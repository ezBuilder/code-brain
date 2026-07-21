from __future__ import annotations

import heapq
import json
import os
import secrets
import signal
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import is_ci
from .redact import redact_value
from .resource_diag import classify_termination, process_tree_rss_kib, system_memory_snapshot

_SANDBOX_DIR = (".ai", "cache", "sandbox")
_SUMMARY_BUDGET_BYTES = 4096
_FIRST_LINES_DEFAULT = 30
_LAST_LINES_DEFAULT = 5
_STDERR_TAIL_DEFAULT = 10
_FETCH_LINE_CAP = 200
_FETCH_DEFAULT_WINDOW = 100
_FETCH_GREP_CAP = 200
_COMPACT_MAX_LINES = 20
_COMPACT_MAX_BYTES = 1024
_MAX_TIMEOUT_SECONDS = 900


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_CAPTURE_MAX_BYTES = _bounded_env_int(
    "AI_SANDBOX_CAPTURE_MAX_BYTES",
    4_000_000,
    minimum=64_000,
    maximum=64_000_000,
)
_SOURCE_CAPTURE_MAX_BYTES = max(
    _CAPTURE_MAX_BYTES,
    _bounded_env_int(
        "AI_SANDBOX_SOURCE_MAX_BYTES",
        64_000_000,
        minimum=1_000_000,
        maximum=1_000_000_000,
    ),
)
_MONITOR_INTERVAL_SECONDS = 0.10
_RSS_SAMPLE_INTERVAL_SECONDS = 0.50
_TERMINATE_GRACE_SECONDS = 1.0
_EXEC_ID_HEX = frozenset("0123456789abcdef")
_META_MAX_BYTES = _bounded_env_int(
    "AI_SANDBOX_META_MAX_BYTES",
    512_000,
    minimum=16_000,
    maximum=8_000_000,
)
_META_DIAGNOSTICS_LIMIT = _bounded_env_int(
    "AI_SANDBOX_DIAGNOSTICS_MAX_FILES",
    100,
    minimum=1,
    maximum=10_000,
)
_META_CANDIDATE_LIMIT = _bounded_env_int(
    "AI_SANDBOX_DIAGNOSTICS_MAX_CANDIDATES",
    4000,
    minimum=1,
    maximum=200_000,
)
_META_SCAN_MAX_BYTES = _bounded_env_int(
    "AI_SANDBOX_DIAGNOSTICS_MAX_BYTES",
    16_000_000,
    minimum=64_000,
    maximum=1_000_000_000,
)
_META_SCAN_MAX_SECONDS = _bounded_env_float(
    "AI_SANDBOX_DIAGNOSTICS_MAX_SECONDS",
    1.0,
    minimum=0.05,
    maximum=60.0,
)
_META_DIAGNOSTIC_EXAMPLES = 10

# --- Opt-in execution isolation (PRD §12.2.1: real sandbox layer for Stage 2 auto-run) ---
# macOS sandbox profile: deny IP network (egress/ingress) + raw IP sockets + mount changes.
# Pattern is (allow default) + targeted denies — robust vs a fragile (deny default) that would
# need every implicit syscall allowed. File *reads* stay allowed so uv/python/git reach system
# libs/config; AF_UNIX local sockets stay allowed (only AF_INET/AF_INET6 denied).
#
# Verified empirically on macOS 26.5 under this exact profile: outbound TCP connect -> EPERM,
# UDP sendto -> EPERM, and DNS getaddrinfo -> EAI_NONAME (resolver unreachable). So both IP
# egress AND the DNS-name covert channel are blocked (locked by test_isolate_network_blocks_*).
#
# Residual risks NOT covered by this baseline (full closure needs a Linux netns/container
# runtime — out of scope): no filesystem WRITE-jail (Stage 2 mitigates by running in a git
# worktree); AF_UNIX local IPC / osascript-XPC side channels; inherited file descriptors;
# PATH points at real binaries (absolute paths defeat any PATH pinning, so PATH is kept).
# isolate_network and isolate_env are ORTHOGONAL — Stage 2 must enable BOTH (network-deny
# stops exfil; env-scrub + --noprofile/--norc stop secret reads and rc-file repopulation).
_SBPL_NETWORK_ISOLATED = """(version 1)
(allow default)
(deny network*)
(deny system-socket (socket-domain 2))
(deny system-socket (socket-domain 30))
(deny file-write-mount)
(deny file-write-unmount)
"""

# Minimal env allowlist so bash -lc / python / git / uv run without inheriting secrets.
_ENV_ALLOWLIST = (
    "PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "SHELL", "LC_CTYPE", "LC_ALL", "LANG",
)
# Caller-supplied extra env names are rejected (fail-closed) if they look secret-bearing.
_SECRET_PREFIXES = (
    "AWS_", "ANTHROPIC_", "OPENAI_", "AZURE_", "GCP_", "GOOGLE_", "GH_", "GITHUB_", "GITLAB_",
    "NPM_", "PIP_", "PYPI_", "POETRY_", "TWINE_", "DOCKER_", "REGISTRY_", "SLACK_", "DISCORD_",
    "SENTRY_", "DATADOG_", "STRIPE_", "FIREBASE_", "TWILIO_", "SSH_", "GPG_", "KUBE",
)
_SECRET_SUBSTRINGS = (
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "CREDENTIAL",
    "PRIVATE_KEY", "ACCESS_KEY", "BEARER", "SESSION", "OAUTH",
    "URL", "URI", "DSN",  # connection strings often carry inline credentials
)


def _has_sandbox_exec() -> bool:
    """sandbox-exec present? (macOS) — gates fail-closed network isolation."""
    return shutil.which("sandbox-exec") is not None


def _is_secret_var(name: str) -> bool:
    """Heuristic: does this env var name look secret-bearing? (reject from extra allowlist)."""
    up = name.upper()
    if any(up.startswith(p) for p in _SECRET_PREFIXES):
        return True
    return any(s in up for s in _SECRET_SUBSTRINGS)


def _build_clean_env(extra: list[str] | None = None) -> dict[str, str]:
    """Clean child env: mandatory allowlist + caller-vetted extras; excludes secrets.

    Raises ValueError if an extra name looks secret-bearing (fail-closed).
    """
    env: dict[str, str] = {}
    for name in _ENV_ALLOWLIST:
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    for name in extra or ():
        if not isinstance(name, str) or not name or name in _ENV_ALLOWLIST:
            continue
        if _is_secret_var(name):
            raise ValueError(f"extra_env_vars rejects secret-bearing name: {name!r}")
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    return env


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sandbox_dir(root: Path) -> Path:
    return root.joinpath(*_SANDBOX_DIR)


def _validated_sandbox_dir(root: Path, *, create: bool) -> Path:
    """Return the repo-confined sandbox directory without following symlink components."""
    root_resolved = root.resolve()
    current = root_resolved
    for part in _SANDBOX_DIR:
        candidate = current / part
        if candidate.is_symlink():
            raise PermissionError("sandbox path must not contain symlinks")
        if create:
            candidate.mkdir(mode=0o700, exist_ok=True)
        if candidate.exists() and not candidate.is_dir():
            raise PermissionError("sandbox path component is not a directory")
        current = candidate
    resolved = current.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PermissionError("sandbox path must stay under repo root") from exc
    return resolved


def _ensure_dir(root: Path) -> Path:
    return _validated_sandbox_dir(root, create=True)


def _valid_exec_id(exec_id: object) -> bool:
    return (
        isinstance(exec_id, str)
        and len(exec_id) == 16
        and all(char in _EXEC_ID_HEX for char in exec_id)
    )


def _artifact_path(root: Path, exec_id: object, suffix: str) -> Path:
    if not _valid_exec_id(exec_id):
        raise ValueError("invalid exec_id")
    path = _validated_sandbox_dir(root, create=False) / f"{exec_id}{suffix}"
    if path.is_symlink():
        raise PermissionError("sandbox artifact must not be a symlink")
    return path


def _resolve_work_cwd(root: Path, cwd: str | None) -> Path:
    root_resolved = root.resolve()
    raw = Path(cwd).expanduser() if cwd is not None else root_resolved
    candidate = raw if raw.is_absolute() else root_resolved / raw
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("cwd must stay under repo root") from exc
    if not resolved.is_dir():
        raise FileNotFoundError(str(cwd or root))
    return resolved


def _write_secure(path: Path, data: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)


def _trim_first_lines(first_lines: list[str], last_lines: list[str]) -> list[str]:
    """Cap first_lines so first+last combined stays under the summary budget."""
    last_len = sum(len(line) + 1 for line in last_lines)
    budget = max(0, _SUMMARY_BUDGET_BYTES - last_len)
    trimmed: list[str] = []
    used = 0
    for line in first_lines:
        cost = len(line) + 1
        if used + cost > budget:
            break
        trimmed.append(line)
        used += cost
    return trimmed


def _read_meta_bounded(
    path: Path,
    *,
    expected: os.stat_result | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, "open_error"
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None, "not_regular"
        if int(getattr(opened, "st_nlink", 1)) != 1:
            return None, "unsafe_hardlink"
        if expected is not None and (
            int(opened.st_dev) != int(expected.st_dev)
            or int(opened.st_ino) != int(expected.st_ino)
            or int(opened.st_mtime_ns) != int(expected.st_mtime_ns)
            or int(opened.st_size) != int(expected.st_size)
        ):
            return None, "changed_before_read"
        if int(opened.st_size) > _META_MAX_BYTES:
            return None, "metadata_too_large"
        raw = os.read(descriptor, _META_MAX_BYTES + 1)
        if len(raw) > _META_MAX_BYTES:
            return None, "metadata_too_large"
        final = os.fstat(descriptor)
        if (
            int(final.st_dev) != int(opened.st_dev)
            or int(final.st_ino) != int(opened.st_ino)
            or int(final.st_mtime_ns) != int(opened.st_mtime_ns)
            or int(final.st_size) != int(opened.st_size)
        ):
            return None, "changed_during_read"
    finally:
        os.close(descriptor)
    try:
        loaded = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(loaded, dict):
        return None, "not_object"
    return loaded, None


def _read_meta(path: Path) -> dict[str, Any] | None:
    loaded, _reason = _read_meta_bounded(path)
    return loaded


def _collect_latest_meta_candidates(
    sandbox_dir: Path,
    *,
    limit: int,
    started: float,
) -> tuple[list[tuple[Path, os.stat_result]], dict[str, Any]]:
    deadline = started + float(_META_SCAN_MAX_SECONDS)
    candidate_limit = max(1, int(_META_CANDIDATE_LIMIT))
    heap: list[tuple[int, str, Path, os.stat_result]] = []
    skip_counts: dict[str, int] = {}
    discovered = 0
    complete = True

    def skip(reason: str, count: int = 1) -> None:
        nonlocal complete
        skip_counts[reason] = skip_counts.get(reason, 0) + max(1, int(count))
        complete = False

    try:
        with os.scandir(sandbox_dir) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    skip("discovery_time_limit")
                    break
                if not entry.name.endswith(".meta.json"):
                    continue
                discovered += 1
                try:
                    state = entry.stat(follow_symlinks=False)
                except OSError:
                    skip("stat_error")
                    continue
                if stat.S_ISLNK(state.st_mode):
                    skip("unsafe_symlink")
                    continue
                if not stat.S_ISREG(state.st_mode):
                    skip("not_regular")
                    continue
                if int(getattr(state, "st_nlink", 1)) != 1:
                    skip("unsafe_hardlink")
                    continue
                if int(state.st_size) > _META_MAX_BYTES:
                    skip("metadata_too_large")
                    continue
                path = Path(entry.path)
                item = (int(state.st_mtime_ns), entry.name, path, state)
                if len(heap) < candidate_limit:
                    heapq.heappush(heap, item)
                elif item[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, item)
                    skip("candidate_limit")
                else:
                    skip("candidate_limit")
    except OSError:
        skip("directory_scan_error")

    selected = [(item[2], item[3]) for item in sorted(heap, reverse=True)[: max(0, int(limit))]]
    return selected, {
        "complete": complete,
        "discovered": discovered,
        "selected": len(selected),
        "skip_counts": dict(sorted(skip_counts.items())),
        "deadline": deadline,
    }


def _maybe_audit(root: Path, payload: dict[str, Any]) -> None:
    if is_ci():
        return
    try:
        from .memory import append_event

        append_event(root, payload)
    except Exception:
        # Audit append must never break execute path.
        pass


def _capture_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _read_bounded_capture(path: Path, *, budget: int) -> tuple[str, int, bool]:
    source_bytes = _capture_size(path)
    if source_bytes <= 0 or budget <= 0:
        return "", source_bytes, source_bytes > 0
    try:
        with path.open("rb") as handle:
            if source_bytes <= budget:
                raw = handle.read(budget)
                return raw.decode("utf-8", errors="replace"), source_bytes, False
            marker = f"\n...[capture truncated: {source_bytes - budget} bytes omitted]...\n".encode("utf-8")
            usable = max(0, budget - len(marker))
            head_size = usable * 3 // 4
            tail_size = usable - head_size
            head = handle.read(head_size)
            if tail_size:
                handle.seek(max(0, source_bytes - tail_size))
                tail = handle.read(tail_size)
            else:
                tail = b""
            raw = head + marker + tail
            return raw.decode("utf-8", errors="replace"), source_bytes, True
    except OSError:
        return "", source_bytes, source_bytes > 0


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def execute(
    root: Path,
    *,
    command: list[str] | str,
    cwd: str | None = None,
    timeout: int = 30,
    isolate_network: bool = False,
    isolate_env: bool = False,
    extra_env_vars: list[str] | None = None,
) -> dict[str, Any]:
    """Run a command and return a redacted, size-capped summary.

    Isolation is opt-in (PRD §12.2.1); all isolation params default off so existing callers
    are unaffected (non-destructive):
      - isolate_network: wrap with macOS `sandbox-exec` to deny IP network egress/ingress and
        raw IP sockets. FAIL-CLOSED: if sandbox-exec is missing, returns an error instead of
        running unisolated.
      - isolate_env: replace the child environment with a minimal allowlist (no inherited
        secrets). extra_env_vars adds caller-vetted names; secret-looking names are rejected.
    Filesystem write-jail is not in this baseline (documented residual risk; Stage 2 also runs
    inside a git worktree).
    """
    # Accept either an argv list or a shell string. String form runs under `bash -lc`
    # so that heredocs, quoting and pipes work without re-escaping into a JSON array.
    if isinstance(command, str):
        if not command.strip():
            return redact_value({"ok": False, "reason": "empty_command"})
        # Under env isolation skip login/rc files (--noprofile --norc) so a user's
        # ~/.bash_profile/.bashrc cannot re-export and repopulate scrubbed secrets.
        bash_flags = ["--noprofile", "--norc", "-c"] if isolate_env else ["-lc"]
        cmd_argv = ["bash", *bash_flags, command]
    else:
        if not command:
            return redact_value({"ok": False, "reason": "empty_command"})
        cmd_argv = list(command)

    try:
        bounded_timeout = int(timeout)
    except (TypeError, ValueError):
        return redact_value({"ok": False, "reason": "invalid_timeout"})
    if bounded_timeout < 1 or bounded_timeout > _MAX_TIMEOUT_SECONDS:
        return redact_value({"ok": False, "reason": "invalid_timeout"})

    try:
        work_cwd_path = _resolve_work_cwd(root, cwd)
    except ValueError:
        return redact_value({"ok": False, "reason": "cwd_outside_root"})
    except FileNotFoundError:
        return redact_value({"ok": False, "reason": "cwd_not_found"})

    try:
        sandbox_dir = _ensure_dir(root)
    except (OSError, PermissionError):
        return redact_value({"ok": False, "reason": "invalid_sandbox_path"})

    exec_id = secrets.token_hex(8)
    work_cwd = str(work_cwd_path)

    # Opt-in isolation. Defaults off → identical behavior to before for current callers.
    run_argv = cmd_argv
    if isolate_network:
        if not _has_sandbox_exec():
            # Fail-closed: caller requested isolation we cannot provide.
            return redact_value({"ok": False, "reason": "sandbox_exec_unavailable", "exec_id": exec_id})
        run_argv = ["sandbox-exec", "-p", _SBPL_NETWORK_ISOLATED, *cmd_argv]

    child_env: dict[str, str] | None = None
    if isolate_env:
        try:
            child_env = _build_clean_env(extra_env_vars)
        except ValueError as exc:
            return redact_value(
                {"ok": False, "reason": "invalid_extra_env_vars", "error": str(exc), "exec_id": exec_id}
            )

    stdout_fd, stdout_name = tempfile.mkstemp(prefix=f".{exec_id}.", suffix=".stdout.tmp", dir=sandbox_dir)
    stderr_fd, stderr_name = tempfile.mkstemp(prefix=f".{exec_id}.", suffix=".stderr.tmp", dir=sandbox_dir)
    stdout_capture = Path(stdout_name)
    stderr_capture = Path(stderr_name)
    before_memory = system_memory_snapshot()
    started = time.monotonic()
    timed_out = False
    output_limit_exceeded = False
    returncode: int | None = None
    peak_rss_kib: int | None = None
    start_error: str | None = None
    try:
        with os.fdopen(stdout_fd, "wb") as stdout_handle, os.fdopen(stderr_fd, "wb") as stderr_handle:
            stdout_fd = stderr_fd = -1
            try:
                proc = subprocess.Popen(
                    run_argv,
                    cwd=work_cwd,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    env=child_env,
                    start_new_session=os.name != "nt",
                )
            except FileNotFoundError:
                start_error = "command_not_found"
            except OSError:
                start_error = "command_start_failed"

            if start_error is None:
                deadline = started + bounded_timeout
                next_rss_sample = started
                while True:
                    now = time.monotonic()
                    if now >= next_rss_sample:
                        observed_rss = process_tree_rss_kib(proc.pid)
                        if observed_rss is not None:
                            peak_rss_kib = max(peak_rss_kib or 0, observed_rss)
                        next_rss_sample = now + _RSS_SAMPLE_INTERVAL_SECONDS
                    if _capture_size(stdout_capture) + _capture_size(stderr_capture) > _SOURCE_CAPTURE_MAX_BYTES:
                        output_limit_exceeded = True
                        _terminate_process(proc)
                        returncode = proc.poll()
                        break
                    remaining = deadline - now
                    if remaining <= 0:
                        timed_out = True
                        _terminate_process(proc)
                        returncode = proc.poll()
                        break
                    try:
                        returncode = proc.wait(timeout=min(_MONITOR_INTERVAL_SECONDS, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
    finally:
        if stdout_fd >= 0:
            os.close(stdout_fd)
        if stderr_fd >= 0:
            os.close(stderr_fd)

    if start_error is not None:
        stdout_capture.unlink(missing_ok=True)
        stderr_capture.unlink(missing_ok=True)
        return redact_value({"ok": False, "reason": start_error, "exec_id": exec_id})

    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    after_memory = system_memory_snapshot()
    stdout_source_bytes = _capture_size(stdout_capture)
    stderr_source_bytes = _capture_size(stderr_capture)
    if stdout_source_bytes + stderr_source_bytes > _SOURCE_CAPTURE_MAX_BYTES:
        output_limit_exceeded = True
    capture_budget = min(_CAPTURE_MAX_BYTES, _SOURCE_CAPTURE_MAX_BYTES)
    if stdout_source_bytes == 0:
        stderr_budget = capture_budget
    elif stderr_source_bytes == 0:
        stderr_budget = 0
    else:
        stderr_budget = min(stderr_source_bytes, max(256_000, capture_budget // 3), capture_budget)
    stdout_budget = max(0, capture_budget - stderr_budget)
    stdout, _stdout_bytes, stdout_truncated = _read_bounded_capture(stdout_capture, budget=stdout_budget)
    stderr, _stderr_bytes, stderr_truncated = _read_bounded_capture(stderr_capture, budget=stderr_budget)
    stdout_capture.unlink(missing_ok=True)
    stderr_capture.unlink(missing_ok=True)
    combined = stdout
    if stderr:
        combined = f"{stdout}{stderr}" if stdout.endswith("\n") or not stdout else f"{stdout}\n{stderr}"

    redacted_combined = redact_value(combined)
    redacted_stderr = redact_value(stderr)
    termination = classify_termination(
        returncode=returncode,
        timed_out=timed_out,
        before=before_memory,
        after=after_memory,
        peak_rss_kib=peak_rss_kib,
        stderr=stderr,
    )
    if output_limit_exceeded:
        termination = {
            "classification": "output_limit_exceeded",
            "confidence": "high",
            "returncode": returncode,
            "signal": termination.get("signal"),
            "signal_number": termination.get("signal_number"),
            "shell_mapped": termination.get("shell_mapped", False),
            "evidence": [
                f"source_total_bytes={stdout_source_bytes + stderr_source_bytes}",
                f"source_capture_max_bytes={_SOURCE_CAPTURE_MAX_BYTES}",
            ],
            "recommendations": [
                "reduce command verbosity or redirect expected bulk output to a bounded project artifact",
            ],
        }

    out_path = sandbox_dir / f"{exec_id}.txt"
    try:
        _write_secure(out_path, redacted_combined)
    except (FileExistsError, OSError):
        return redact_value({"ok": False, "reason": "artifact_write_failed", "exec_id": exec_id})

    all_lines = redacted_combined.splitlines()
    total_lines = len(all_lines)
    total_bytes = len(redacted_combined.encode("utf-8"))
    stderr_bytes = len(redacted_stderr.encode("utf-8"))

    first_lines = all_lines[:_FIRST_LINES_DEFAULT]
    last_lines = all_lines[-_LAST_LINES_DEFAULT:] if total_lines > _FIRST_LINES_DEFAULT else []

    stderr_tail: list[str] = []
    if redacted_stderr:
        stderr_lines = redacted_stderr.splitlines()
        stderr_tail = stderr_lines[-_STDERR_TAIL_DEFAULT:]

    first_lines = _trim_first_lines(first_lines, last_lines)

    created_at = _now_iso()

    meta = {
        "exec_id": exec_id,
        "command": redact_value(list(cmd_argv)),
        "cwd": work_cwd,
        "exit_code": returncode,
        "command_ok": returncode == 0 and not timed_out and not output_limit_exceeded,
        "total_bytes": total_bytes,
        "source_total_bytes": stdout_source_bytes + stderr_source_bytes,
        "total_lines": total_lines,
        "created_at": created_at,
        "stderr_bytes": stderr_bytes,
        "isolate_network": isolate_network,
        "isolate_env": isolate_env,
        "elapsed_ms": elapsed_ms,
        "peak_rss_kib": peak_rss_kib,
        "output_truncated": stdout_truncated or stderr_truncated,
        "termination": termination,
        "memory_before": before_memory,
        "memory_after": after_memory,
    }
    meta_path = sandbox_dir / f"{exec_id}.meta.json"
    try:
        _write_secure(meta_path, json.dumps(meta, ensure_ascii=False, sort_keys=True))
    except (FileExistsError, OSError):
        out_path.unlink(missing_ok=True)
        return redact_value({"ok": False, "reason": "artifact_write_failed", "exec_id": exec_id})

    # Compact mode: when the full output is short, return it raw and skip the
    # first_lines/last_lines split. Saves model-side tokens for small results.
    is_compact = total_lines <= _COMPACT_MAX_LINES and total_bytes <= _COMPACT_MAX_BYTES
    summary: dict[str, Any] = {
        "ok": not timed_out and not output_limit_exceeded,
        "exec_id": exec_id,
        "exit_code": returncode,
        "command_ok": returncode == 0 and not timed_out and not output_limit_exceeded,
        "total_bytes": total_bytes,
        "source_total_bytes": stdout_source_bytes + stderr_source_bytes,
        "total_lines": total_lines,
        "stderr_tail": stderr_tail,
        "created_at": created_at,
        "isolate_network": isolate_network,
        "isolate_env": isolate_env,
        "elapsed_ms": elapsed_ms,
        "peak_rss_kib": peak_rss_kib,
        "output_truncated": stdout_truncated or stderr_truncated,
        "termination": termination,
    }
    if timed_out:
        summary["reason"] = "timeout"
    elif output_limit_exceeded:
        summary["reason"] = "output_limit_exceeded"
    if is_compact:
        summary["output"] = redacted_combined
    else:
        summary["first_lines"] = first_lines
        summary["last_lines"] = last_lines

    _maybe_audit(
        root,
        {
            "hook": "sandbox.execute",
            "exec_id": exec_id,
            "exit_code": returncode,
            "termination": termination["classification"],
            "peak_rss_kib": peak_rss_kib,
            "total_bytes": total_bytes,
        },
    )

    return redact_value(summary)


_READ_OUTPUT_CAP_BYTES = 4_000_000  # bound read_output memory (defensive; metric output is tiny)


def read_output(root: Path, exec_id: str) -> str | None:
    """Full redacted combined output of a prior execute() (by exec_id), or None.

    For callers (e.g. the Stage 2 metric loop) that need the complete output for pattern
    extraction rather than the size-capped summary execute() returns. exec_id is validated
    as 16 hex chars (the token_hex(8) format) so it cannot traverse paths; the read is capped
    at _READ_OUTPUT_CAP_BYTES to bound memory.
    """
    try:
        out_path = _artifact_path(root, exec_id, ".txt")
        with out_path.open("rb") as handle:
            return handle.read(_READ_OUTPUT_CAP_BYTES).decode("utf-8", errors="replace")
    except (OSError, PermissionError, ValueError):
        return None


def fetch(
    root: Path,
    *,
    exec_id: str,
    line_start: int = 1,
    line_end: int | None = None,
    grep_pattern: str | None = None,
) -> dict[str, Any]:
    if not _valid_exec_id(exec_id):
        return redact_value({"ok": False, "reason": "invalid_exec_id"})
    try:
        out_path = _artifact_path(root, exec_id, ".txt")
    except PermissionError:
        return redact_value({"ok": False, "reason": "invalid_artifact", "exec_id": exec_id})
    except (OSError, ValueError):
        return redact_value({"ok": False, "reason": "invalid_sandbox_path", "exec_id": exec_id})
    if not out_path.exists():
        return redact_value({"ok": False, "reason": "not_found", "exec_id": exec_id})

    try:
        with out_path.open("rb") as handle:
            raw = handle.read(_READ_OUTPUT_CAP_BYTES + 1)
    except OSError:
        return redact_value({"ok": False, "reason": "not_found", "exec_id": exec_id})
    truncated = len(raw) > _READ_OUTPUT_CAP_BYTES
    text = raw[:_READ_OUTPUT_CAP_BYTES].decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    total_lines = len(all_lines)

    if grep_pattern is not None:
        needle = grep_pattern.lower()
        matched: list[dict[str, Any]] = []
        for idx, line in enumerate(all_lines, start=1):
            if needle in line.lower():
                matched.append({"lineno": idx, "text": line})
                if len(matched) >= _FETCH_GREP_CAP:
                    break
        result = {
            "ok": True,
            "exec_id": exec_id,
            "total_lines": total_lines,
            "matched_lines": matched,
            "pattern": grep_pattern,
            "truncated": truncated,
        }
        return redact_value(result)

    if line_start < 1:
        line_start = 1
    if line_end is None:
        line_end = line_start + _FETCH_DEFAULT_WINDOW - 1
    if line_end < line_start:
        line_end = line_start

    start_idx = line_start - 1
    end_idx = line_end
    sliced = all_lines[start_idx:end_idx]
    if len(sliced) > _FETCH_LINE_CAP:
        sliced = sliced[:_FETCH_LINE_CAP]
        line_end = line_start + len(sliced) - 1

    lines_payload = [{"lineno": line_start + offset, "text": line} for offset, line in enumerate(sliced)]

    result = {
        "ok": True,
        "exec_id": exec_id,
        "total_lines": total_lines,
        "line_start": line_start,
        "line_end": line_end,
        "lines": lines_payload,
        "truncated": truncated,
    }
    return redact_value(result)


def _execution_diagnostics_empty(*, ok: bool, reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "bounded": True,
        "complete": ok,
        "partial": not ok,
        "metas_discovered": 0,
        "metas_scanned": 0,
        "bytes_scanned": 0,
        "command_failures": 0,
        "max_peak_rss_kib": 0,
        "classifications": {},
        "killed_9": {
            "sigkill_total": 0,
            "oom_confirmed": 0,
            "memory_limit": 0,
            "host_memory_pressure": 0,
            "external_execution_limit": 0,
        },
        "skip_counts": {},
        "examples": [],
        "policy": {
            "max_meta_bytes": int(_META_MAX_BYTES),
            "max_files": int(_META_DIAGNOSTICS_LIMIT),
            "max_candidates": int(_META_CANDIDATE_LIMIT),
            "max_scan_bytes": int(_META_SCAN_MAX_BYTES),
            "max_scan_seconds": float(_META_SCAN_MAX_SECONDS),
        },
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def execution_diagnostics(root: Path) -> dict[str, Any]:
    """Return a bounded summary of recent sandbox executions and SIGKILL evidence.

    The cache can contain years of command metadata. Discovery uses a fixed-size
    newest-first heap; metadata reads have per-file, aggregate-byte and wall-clock
    limits. A partial scan is explicit but remains usable for release diagnostics.
    """
    started = time.monotonic()
    try:
        sandbox_dir = _validated_sandbox_dir(root, create=False)
    except (OSError, PermissionError):
        return redact_value(_execution_diagnostics_empty(ok=False, reason="invalid_sandbox_path"))
    if not sandbox_dir.exists():
        return redact_value(_execution_diagnostics_empty(ok=True))

    candidates, scan = _collect_latest_meta_candidates(
        sandbox_dir,
        limit=int(_META_DIAGNOSTICS_LIMIT),
        started=started,
    )
    skip_counts = dict(scan["skip_counts"])
    classifications: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    bytes_scanned = 0
    metas_scanned = 0
    command_failures = 0
    max_peak_rss_kib = 0
    sigkill_total = 0
    oom_confirmed = 0
    memory_limit = 0
    host_memory_pressure = 0
    external_execution_limit = 0
    complete = bool(scan["complete"])

    def skip(reason: str) -> None:
        nonlocal complete
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
        complete = False

    for meta_path, expected in candidates:
        if time.monotonic() >= float(scan["deadline"]):
            skip("read_time_limit")
            break
        size = max(0, int(expected.st_size))
        if bytes_scanned + size > int(_META_SCAN_MAX_BYTES):
            skip("aggregate_byte_limit")
            break
        meta, _reason = _read_meta_bounded(meta_path, expected=expected)
        if meta is None:
            skip(reason or "metadata_read_error")
            continue
        bytes_scanned += size
        metas_scanned += 1

        termination = meta.get("termination")
        if not isinstance(termination, dict):
            termination = {}
        classification = str(termination.get("classification") or "unknown")
        classifications[classification] = classifications.get(classification, 0) + 1
        command_ok = meta.get("command_ok") is True
        if not command_ok:
            command_failures += 1
        peak = meta.get("peak_rss_kib")
        if isinstance(peak, int) and peak > max_peak_rss_kib:
            max_peak_rss_kib = peak

        signal_name = termination.get("signal")
        if signal_name == "SIGKILL":
            sigkill_total += 1
            if classification == "cgroup_oom_kill_confirmed":
                oom_confirmed += 1
            elif classification in {"cgroup_memory_limit_confirmed", "cgroup_memory_limit_likely"}:
                memory_limit += 1
            elif classification == "host_memory_pressure_likely":
                host_memory_pressure += 1
            elif classification == "external_sigkill_or_execution_limit":
                external_execution_limit += 1

        if not command_ok and len(examples) < _META_DIAGNOSTIC_EXAMPLES:
            examples.append(
                {
                    "exec_id": meta.get("exec_id"),
                    "created_at": meta.get("created_at"),
                    "exit_code": meta.get("exit_code"),
                    "classification": classification,
                    "signal": signal_name,
                    "peak_rss_kib": peak if isinstance(peak, int) else None,
                    "source_total_bytes": meta.get("source_total_bytes"),
                }
            )

    integrity_reasons = {
        "stat_error",
        "unsafe_symlink",
        "not_regular",
        "unsafe_hardlink",
        "metadata_too_large",
        "directory_scan_error",
        "open_error",
        "changed_before_read",
        "changed_during_read",
        "invalid_json",
        "not_object",
        "metadata_read_error",
    }
    integrity_failures = sum(skip_counts.get(reason, 0) for reason in integrity_reasons)
    result = {
        "ok": integrity_failures == 0,
        "bounded": True,
        "complete": complete,
        "partial": not complete,
        "metas_discovered": int(scan["discovered"]),
        "metas_scanned": metas_scanned,
        "bytes_scanned": bytes_scanned,
        "command_failures": command_failures,
        "max_peak_rss_kib": max_peak_rss_kib,
        "classifications": dict(sorted(classifications.items())),
        "killed_9": {
            "sigkill_total": sigkill_total,
            "oom_confirmed": oom_confirmed,
            "memory_limit": memory_limit,
            "host_memory_pressure": host_memory_pressure,
            "external_execution_limit": external_execution_limit,
        },
        "skip_counts": dict(sorted(skip_counts.items())),
        "examples": examples,
        "policy": {
            "max_meta_bytes": int(_META_MAX_BYTES),
            "max_files": int(_META_DIAGNOSTICS_LIMIT),
            "max_candidates": int(_META_CANDIDATE_LIMIT),
            "max_scan_bytes": int(_META_SCAN_MAX_BYTES),
            "max_scan_seconds": float(_META_SCAN_MAX_SECONDS),
        },
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
    }
    return redact_value(result)


def list_executions(root: Path, *, limit: int = 20) -> dict[str, Any]:
    try:
        sandbox_dir = _validated_sandbox_dir(root, create=False)
    except (OSError, PermissionError):
        return redact_value({"ok": False, "reason": "invalid_sandbox_path", "count": 0, "items": []})
    if not sandbox_dir.exists():
        return redact_value({"ok": True, "count": 0, "items": []})

    bounded_limit = max(0, min(int(limit), int(_META_DIAGNOSTICS_LIMIT)))
    candidates, scan = _collect_latest_meta_candidates(
        sandbox_dir,
        limit=bounded_limit,
        started=time.monotonic(),
    )
    metas: list[dict[str, Any]] = []
    for meta_path, expected in candidates:
        meta, reason = _read_meta_bounded(meta_path, expected=expected)
        if meta is None:
            continue
        metas.append(meta)

    metas.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    metas = metas[:bounded_limit]

    items = []
    for meta in metas:
        command = meta.get("command") or []
        if isinstance(command, list):
            command_summary = " ".join(str(part) for part in command)[:80]
        else:
            command_summary = str(command)[:80]
        items.append(
            {
                "exec_id": meta.get("exec_id"),
                "exit_code": meta.get("exit_code"),
                "total_bytes": meta.get("total_bytes"),
                "total_lines": meta.get("total_lines"),
                "created_at": meta.get("created_at"),
                "command_summary": command_summary,
            }
        )

    return redact_value(
        {
            "ok": True,
            "count": len(items),
            "items": items,
            "complete": bool(scan["complete"]),
            "skip_counts": scan["skip_counts"],
        }
    )


def prune(root: Path, *, older_than_seconds: int = 86400) -> dict[str, Any]:
    try:
        sandbox_dir = _validated_sandbox_dir(root, create=False)
    except (OSError, PermissionError):
        return redact_value({"ok": False, "reason": "invalid_sandbox_path", "removed_count": 0, "kept_count": 0})
    if not sandbox_dir.exists():
        return redact_value({"ok": True, "removed_count": 0, "kept_count": 0})

    now = datetime.now(timezone.utc)
    threshold = float(older_than_seconds)
    removed = 0
    kept = 0

    for meta_path in list(sandbox_dir.glob("*.meta.json")):
        meta = _read_meta(meta_path)
        if meta is None:
            kept += 1
            continue
        created_at = meta.get("created_at")
        age_seconds: float | None = None
        if isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_seconds = (now - created_dt).total_seconds()
            except ValueError:
                age_seconds = None
        if age_seconds is None:
            try:
                mtime = meta_path.stat().st_mtime
                age_seconds = now.timestamp() - mtime
            except OSError:
                age_seconds = None

        if age_seconds is not None and age_seconds >= threshold:
            exec_id = meta.get("exec_id") or meta_path.stem.replace(".meta", "")
            if not _valid_exec_id(exec_id):
                kept += 1
                continue
            try:
                txt_path = _artifact_path(root, exec_id, ".txt")
            except (OSError, PermissionError, ValueError):
                kept += 1
                continue
            try:
                meta_path.unlink()
            except OSError:
                pass
            try:
                if txt_path.exists():
                    txt_path.unlink()
            except OSError:
                pass
            removed += 1
        else:
            kept += 1

    return redact_value({"ok": True, "removed_count": removed, "kept_count": kept})
