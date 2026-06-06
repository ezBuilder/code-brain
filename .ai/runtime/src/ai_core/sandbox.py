from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import is_ci
from .redact import redact_value

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


def _ensure_dir(root: Path) -> Path:
    sandbox = _sandbox_dir(root)
    sandbox.mkdir(parents=True, exist_ok=True)
    return sandbox


def _write_secure(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


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


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _maybe_audit(root: Path, payload: dict[str, Any]) -> None:
    if is_ci():
        return
    try:
        from .memory import append_event

        append_event(root, payload)
    except Exception:
        # Audit append must never break execute path.
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

    exec_id = secrets.token_hex(8)
    work_cwd = cwd if cwd is not None else str(root)

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

    try:
        completed = subprocess.run(
            run_argv,
            cwd=work_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return redact_value({"ok": False, "reason": "timeout", "exec_id": exec_id})
    except FileNotFoundError:
        return redact_value({"ok": False, "reason": "command_not_found", "exec_id": exec_id})

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = stdout
    if stderr:
        combined = f"{stdout}{stderr}" if stdout.endswith("\n") or not stdout else f"{stdout}\n{stderr}"

    redacted_combined = redact_value(combined)
    redacted_stderr = redact_value(stderr)

    sandbox_dir = _ensure_dir(root)
    out_path = sandbox_dir / f"{exec_id}.txt"
    _write_secure(out_path, redacted_combined)

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
        "command": list(cmd_argv),
        "cwd": work_cwd,
        "exit_code": completed.returncode,
        "total_bytes": total_bytes,
        "total_lines": total_lines,
        "created_at": created_at,
        "stderr_bytes": stderr_bytes,
        "isolate_network": isolate_network,
        "isolate_env": isolate_env,
    }
    meta_path = sandbox_dir / f"{exec_id}.meta.json"
    _write_secure(meta_path, json.dumps(meta, ensure_ascii=False, sort_keys=True))

    # Compact mode: when the full output is short, return it raw and skip the
    # first_lines/last_lines split. Saves model-side tokens for small results.
    is_compact = total_lines <= _COMPACT_MAX_LINES and total_bytes <= _COMPACT_MAX_BYTES
    summary: dict[str, Any] = {
        "ok": True,
        "exec_id": exec_id,
        "exit_code": completed.returncode,
        "total_bytes": total_bytes,
        "total_lines": total_lines,
        "stderr_tail": stderr_tail,
        "created_at": created_at,
    }
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
            "exit_code": completed.returncode,
            "total_bytes": total_bytes,
        },
    )

    return redact_value(summary)


def fetch(
    root: Path,
    *,
    exec_id: str,
    line_start: int = 1,
    line_end: int | None = None,
    grep_pattern: str | None = None,
) -> dict[str, Any]:
    sandbox_dir = _sandbox_dir(root)
    out_path = sandbox_dir / f"{exec_id}.txt"
    if not out_path.exists():
        return redact_value({"ok": False, "reason": "not_found", "exec_id": exec_id})

    text = out_path.read_text(encoding="utf-8")
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
    }
    return redact_value(result)


def list_executions(root: Path, *, limit: int = 20) -> dict[str, Any]:
    sandbox_dir = _sandbox_dir(root)
    if not sandbox_dir.exists():
        return redact_value({"ok": True, "count": 0, "items": []})

    metas: list[dict[str, Any]] = []
    for meta_path in sandbox_dir.glob("*.meta.json"):
        meta = _read_meta(meta_path)
        if meta is None:
            continue
        metas.append(meta)

    metas.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    metas = metas[: max(0, int(limit))]

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

    return redact_value({"ok": True, "count": len(items), "items": items})


def prune(root: Path, *, older_than_seconds: int = 86400) -> dict[str, Any]:
    sandbox_dir = _sandbox_dir(root)
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
            txt_path = sandbox_dir / f"{exec_id}.txt"
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
