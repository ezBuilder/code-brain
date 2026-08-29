#!/usr/bin/env python3
"""Persist trust for verified Code Brain Codex hooks.

Codex intentionally invalidates trust when a hook definition changes. This
helper mirrors the Codex hook browser's public app-server flow. Installers may
trust canonical Code Brain entries in a verified managed target by default;
foreign project entries stay untrusted, while custom project configs and user
hook scripts still require an explicit private policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
import threading
from pathlib import Path
from typing import Any

from codex_hook_contract import (
    HookContractError,
    PROJECT_EVENTS,
    project_command as _project_command,
    project_windows_command as _project_windows_command,
    validate_managed_hook_payload,
)


DEFAULT_TIMEOUT_SECONDS = 15.0
# Runtime routers remain byte-identical to the reviewed installer source.
# hooks.json is instead validated against codex_hook_contract.py because its
# optional events and preserved foreign groups legitimately vary by target.
MANAGED_PROJECT_ROUTER_FILES: tuple[str, ...] = (
    ".ai/bin/ai-hook",
    ".ai/bin/ai-hook.ps1",
)


class TrustError(RuntimeError):
    """Raised when trust cannot be updated safely."""


class PolicyError(TrustError):
    """Raised when an explicit private trust policy is invalid or unsafe."""


def _require_private_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        state = path.lstat()
    except OSError as exc:
        raise TrustError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise TrustError(f"{label} must be a regular non-symlink file: {path}")
    if os.name != "nt":
        if hasattr(os, "getuid") and state.st_uid != os.getuid():
            raise TrustError(f"{label} must be owned by the current user: {path}")
        if state.st_mode & 0o022:
            raise TrustError(f"{label} must not be group/world writable: {path}")
    return state


def load_policy(
    path: Path,
    *,
    codex_home: Path,
    allow_missing_entries: bool = False,
) -> dict[str, Any]:
    _require_private_regular_file(path, label="hook trust policy")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustError(f"hook trust policy is invalid: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise TrustError("hook trust policy must be a schema-1 JSON object")

    project_enabled = payload.get("trust_project_code_brain_hooks", False)
    if not isinstance(project_enabled, bool):
        raise TrustError("trust_project_code_brain_hooks must be boolean")

    raw_project_roots = payload.get("trusted_project_roots", [])
    if not isinstance(raw_project_roots, list) or not all(
        isinstance(item, str) for item in raw_project_roots
    ):
        raise TrustError("trusted_project_roots must be a string array")
    trusted_project_roots: list[Path] = []
    for raw in raw_project_roots:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise TrustError(f"trusted project root must be absolute: {raw}")
        # The default policy augments exact managed-target trust. A deleted old
        # workspace must not disable trust for every later upgrade; ignoring an
        # absent entry cannot grant trust, and the policy file remains untouched.
        # Dangling symlinks are still rejected rather than treated as absent.
        if not candidate.exists():
            if candidate.is_symlink() or not allow_missing_entries:
                raise TrustError(f"trusted project root is not a directory: {candidate}")
            continue
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise TrustError(f"trusted project root is not a directory: {resolved}")
        trusted_project_roots.append(resolved)

    raw_user_paths = payload.get("trusted_user_hook_paths", [])
    if not isinstance(raw_user_paths, list) or not all(isinstance(item, str) for item in raw_user_paths):
        raise TrustError("trusted_user_hook_paths must be a string array")
    user_hook_dir = (codex_home / "hooks").resolve()
    trusted_user_paths: list[str] = []
    for raw in raw_user_paths:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise TrustError(f"trusted user hook path must be absolute: {raw}")
        if not candidate.exists():
            if candidate.is_symlink() or not allow_missing_entries:
                raise TrustError(f"trusted user hook is unavailable: {candidate}")
            continue
        _require_private_regular_file(candidate, label="trusted user hook")
        resolved = candidate.resolve()
        if resolved.parent != user_hook_dir:
            raise TrustError(f"trusted user hook must stay directly under {user_hook_dir}")
        trusted_user_paths.append(str(resolved))

    return {
        "trust_project_code_brain_hooks": project_enabled,
        "trusted_project_roots": tuple(trusted_project_roots),
        "trusted_user_hook_paths": frozenset(trusted_user_paths),
        "managed_target_default": False,
    }


def managed_target_policy(cwd: Path) -> dict[str, Any]:
    """Ephemeral least-privilege policy for a user-invoked install target."""
    return {
        "trust_project_code_brain_hooks": True,
        "trusted_project_roots": (cwd,),
        "trusted_user_hook_paths": frozenset(),
        "managed_target_default": True,
    }


def _is_within(candidate: Path, roots: tuple[Path, ...]) -> bool:
    return any(candidate == root or root in candidate.parents for root in roots)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _project_config_root(cwd: Path, *, policy: dict[str, Any]) -> Path:
    """Return the worktree root whose project config Codex actually loads.

    Codex resolves linked Git worktrees through the repository's main
    worktree, so hooks/list reports the main worktree's .codex/hooks.json even
    when cwd is the linked worktree.  Accept that source only when Git itself
    confirms cwd is a repository top-level. Explicit policies must also cover
    the main worktree; an ephemeral managed-target policy may follow this
    Git-proven relationship after validating both worktrees' managed files.
    """
    git = shutil.which("git")
    if not git:
        return cwd
    try:
        top = subprocess.run(
            [git, "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrustError("failed to resolve the target Git worktree") from exc
    if top.returncode != 0:
        return cwd
    top_raw = top.stdout.strip()
    if not top_raw or not _same_path(Path(top_raw), cwd):
        raise TrustError("project directory must be the Git worktree top-level")
    try:
        listed = subprocess.run(
            [git, "-C", str(cwd), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrustError("failed to resolve the main Git worktree") from exc
    if listed.returncode != 0:
        raise TrustError("Git did not report the repository worktrees")
    first = next(
        (line.removeprefix("worktree ") for line in listed.stdout.splitlines() if line.startswith("worktree ")),
        "",
    )
    if not first:
        raise TrustError("Git did not report a main worktree")
    project_root = Path(first).expanduser().resolve()
    if not project_root.is_dir():
        raise TrustError("Git reported an unavailable main worktree")
    if not _is_within(project_root, policy["trusted_project_roots"]) and not policy.get(
        "managed_target_default", False
    ):
        raise TrustError("main Git worktree is outside trusted_project_roots")
    return project_root


def _simple_user_script(command: str, trusted_paths: frozenset[str]) -> bool:
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    if len(tokens) != 1:
        return False
    token = tokens[0]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        token = token[1:-1]
    normalized = os.path.normcase(os.path.normpath(token))
    return normalized in {
        os.path.normcase(os.path.normpath(trusted_path)) for trusted_path in trusted_paths
    }


def eligible_hooks(
    hooks: list[dict[str, Any]],
    *,
    cwd: Path,
    project_config_root: Path,
    codex_home: Path,
    policy: dict[str, Any],
    managed_project_events: frozenset[str],
) -> dict[str, dict[str, Any]]:
    expected_project_source = project_config_root / ".codex" / "hooks.json"
    expected_user_source = codex_home / "hooks.json"
    project_source_ok = False
    if expected_project_source.exists():
        _require_private_regular_file(expected_project_source, label="project Codex hooks")
        project_source_ok = True

    selected: dict[str, dict[str, Any]] = {}
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        key = hook.get("key")
        current_hash = hook.get("currentHash")
        command = hook.get("command")
        source_path_raw = hook.get("sourcePath")
        if not all(isinstance(value, str) and value for value in (key, current_hash, command, source_path_raw)):
            continue
        source_path = Path(source_path_raw)
        source = hook.get("source")

        if (
            source == "project"
            and policy["trust_project_code_brain_hooks"]
            and project_source_ok
            and _same_path(source_path, expected_project_source)
        ):
            wire_event = PROJECT_EVENTS.get(str(hook.get("eventName") or ""))
            if (
                wire_event in managed_project_events
                and command in {_project_command(wire_event), _project_windows_command(wire_event)}
            ):
                selected[key] = hook
            continue

        if (
            source == "user"
            and _same_path(source_path, expected_user_source)
            and _simple_user_script(command, policy["trusted_user_hook_paths"])
        ):
            selected[key] = hook
    return selected


def find_codex() -> str:
    override = os.environ.get("CODEX_BIN")
    candidates = [
        override,
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
        shutil.which("codex"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise TrustError("Codex executable not found")


class AppServer:
    def __init__(self, executable: str, *, timeout: float) -> None:
        self.timeout = timeout
        self.next_id = 1
        self.messages: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        command = [executable, "app-server", "--listen", "stdio://"]
        if os.name == "nt" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *command]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise TrustError("failed to start Codex app-server") from exc
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self.messages.put(message)
        except BaseException as exc:  # pragma: no cover - platform pipe failure
            self.messages.put(exc)
        finally:
            self.messages.put(None)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise TrustError("Codex app-server stopped unexpectedly")
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise TrustError("failed to write to Codex app-server") from exc

    def notify(self, method: str, params: Any = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def request(self, method: str, params: Any) -> Any:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            try:
                message = self.messages.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise TrustError(f"Codex app-server timed out during {method}") from exc
            if message is None:
                raise TrustError(f"Codex app-server closed during {method}")
            if isinstance(message, BaseException):
                raise TrustError(f"Codex app-server reader failed during {method}") from message
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise TrustError(f"Codex app-server rejected {method}")
            return message.get("result")

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover - pathological child
                self.process.kill()
                self.process.wait(timeout=2)

    def __enter__(self) -> "AppServer":
        try:
            result = self.request(
                "initialize",
                {"clientInfo": {"name": "code-brain-hook-trust", "version": "1"}},
            )
            if not isinstance(result, dict):
                raise TrustError("Codex app-server initialization failed")
            self.notify("initialized")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _hooks_for_cwd(result: Any, cwd: Path) -> list[dict[str, Any]]:
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise TrustError("Codex hooks/list returned an invalid response")
    for entry in result["data"]:
        entry_cwd = entry.get("cwd") if isinstance(entry, dict) else None
        if isinstance(entry_cwd, str) and entry_cwd and _same_path(Path(entry_cwd), cwd):
            hooks = entry.get("hooks")
            if isinstance(hooks, list):
                return [hook for hook in hooks if isinstance(hook, dict)]
    raise TrustError(f"Codex hooks/list omitted the requested project: {cwd}")


def _project_trust_key_path(cwd: Path) -> str:
    # Mirrors Codex's own TOML representation: [projects."<abs-path>"] trust_level.
    # config/batchWrite keyPaths use the same dotted/quoted-segment syntax.
    escaped = str(cwd).replace("\\", "\\\\").replace('"', '\\"')
    return f'projects."{escaped}".trust_level'


def _has_project_source_hooks(result: Any, cwd: Path, *, project_config_root: Path) -> bool:
    """Whether Codex's hooks/list surfaced any hook sourced from this cwd's
    own .codex/hooks.json.

    Codex omits a project's hooks from hooks/list entirely when
    projects.<cwd>.trust_level is unset, regardless of individual hash-trust
    state. Presence of at least one "source": "project" entry (of any
    trustStatus) is therefore used as the ground-truth signal that project
    trust bootstrap already succeeded, instead of depending on an assumed
    trust-level field name in the response schema.
    """
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return False
    for entry in result["data"]:
        entry_cwd = entry.get("cwd") if isinstance(entry, dict) else None
        if not (isinstance(entry_cwd, str) and entry_cwd and _same_path(Path(entry_cwd), cwd)):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            return False
        expected_source = project_config_root / ".codex" / "hooks.json"
        return any(
            isinstance(hook, dict)
            and hook.get("source") == "project"
            and isinstance(hook.get("sourcePath"), str)
            and _same_path(Path(hook["sourcePath"]), expected_source)
            for hook in hooks
        )
    return False


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except OSError as exc:
        raise TrustError(f"managed file became unreadable: {path}") from exc
    return digest.hexdigest(), total


def _verify_managed_codex_hook_file(path: Path, *, label: str) -> frozenset[str]:
    _require_private_regular_file(path, label=label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustError(f"{label} is not valid JSON: {path}") from exc
    try:
        return validate_managed_hook_payload(payload)
    except HookContractError as exc:
        raise TrustError(f"{label} violates the managed Code Brain contract: {exc}") from exc


def _verify_managed_project_hook_files(
    cwd: Path, *, source_root: Path, project_config_root: Path
) -> frozenset[str]:
    """Verify immutable routers plus the target-specific semantic manifest.

    The POSIX/PowerShell routers must remain byte-identical to this helper's
    reviewed source. The Codex manifest may differ only through canonical
    SessionEnd/Interrupt version gates and unrelated foreign groups. Every
    Code Brain-owned group is checked exactly, and foreign groups are never
    included in the returned trust-eligible event set.
    """
    for relative in MANAGED_PROJECT_ROUTER_FILES:
        target_path = cwd / relative
        source_path = source_root / relative
        _require_private_regular_file(target_path, label=f"target managed hook file {relative}")
        _require_private_regular_file(source_path, label=f"source managed hook file {relative}")
        if relative == ".ai/bin/ai-hook" and not os.access(target_path, os.X_OK):
            raise TrustError(f"target managed hook file must be executable: {target_path}")
        target_digest, target_size = _sha256_and_size(target_path)
        source_digest, source_size = _sha256_and_size(source_path)
        if target_size != source_size or target_digest != source_digest:
            raise TrustError(
                f"target managed hook file does not match helper source: {relative}"
            )
    target_events = _verify_managed_codex_hook_file(
        cwd / ".codex" / "hooks.json",
        label="target project Codex hooks",
    )
    if not _same_path(project_config_root, cwd):
        return _verify_managed_codex_hook_file(
            project_config_root / ".codex" / "hooks.json",
            label="main worktree project Codex hooks",
        )
    return target_events


def _verify_default_project_config(*, project_config_root: Path, source_root: Path) -> None:
    """Require a managed-only project config before implicit project trust.

    Project trust activates the complete .codex/config.toml layer, including
    MCP commands. The no-policy convenience path therefore accepts only the
    parsed Code Brain source config. Custom config remains available through
    an explicit private policy.
    """
    target = project_config_root / ".codex" / "config.toml"
    source = source_root / ".codex" / "config.toml"
    _require_private_regular_file(target, label="target managed Codex config")
    _require_private_regular_file(source, label="source managed Codex config")
    try:
        target_payload = tomllib.loads(target.read_text(encoding="utf-8"))
        source_payload = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TrustError("managed Codex config is invalid") from exc
    if target_payload != source_payload:
        raise TrustError("default trust requires an unmodified Code Brain project config")


def _ensure_project_trust(
    server: "AppServer",
    *,
    cwd: Path,
    policy: dict[str, Any],
    project_config_root: Path,
    source_root: Path,
) -> None:
    """Bootstrap projects.<cwd>.trust_level=trusted so hooks/list surfaces the
    project's own .codex/hooks.json entries at all.

    Only runs when the selected policy enables project hook trust and cwd is
    inside an allowlisted root (both already enforced by the caller before
    this function is reached). Idempotent: skipped once hooks/list
    already reports at least one project-sourced hook for this exact cwd,
    which is only possible once Codex already considers the project trusted.

    The caller verifies every managed hook file before opening the app-server,
    including when the project was already trusted. This function therefore
    only handles the bootstrap state transition and its live verification.
    """
    if not policy["trust_project_code_brain_hooks"]:
        return
    probe = server.request("hooks/list", {"cwds": [str(cwd)]})
    if _has_project_source_hooks(probe, cwd, project_config_root=project_config_root):
        return
    if any(hook.get("source") == "project" for hook in _hooks_for_cwd(probe, cwd)):
        raise TrustError("Codex reported project hooks from an unexpected source path")
    if policy.get("managed_target_default", False):
        _verify_default_project_config(
            project_config_root=project_config_root,
            source_root=source_root,
        )
    server.request(
        "config/batchWrite",
        {
            "edits": [
                {
                    "keyPath": _project_trust_key_path(cwd),
                    "value": "trusted",
                    "mergeStrategy": "upsert",
                }
            ],
            "reloadUserConfig": True,
        },
    )
    verify = server.request("hooks/list", {"cwds": [str(cwd)]})
    if not _has_project_source_hooks(verify, cwd, project_config_root=project_config_root):
        raise TrustError("Codex did not persist the requested project trust bootstrap")


def trust_hooks(
    *,
    cwd: Path,
    policy_path: Path | None,
    timeout: float,
    managed_target_default: bool = False,
    fallback_managed_target: bool = False,
) -> dict[str, Any]:
    if timeout <= 0:
        raise TrustError("timeout must be greater than zero")
    if not cwd.expanduser().is_dir():
        raise TrustError(f"project directory is unavailable: {cwd}")
    cwd = cwd.resolve()
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()
    if managed_target_default:
        if policy_path is not None:
            raise TrustError("managed target trust cannot be combined with an explicit policy")
        if fallback_managed_target:
            raise TrustError("managed target fallback requires an explicit policy")
        policy = managed_target_policy(cwd)
    else:
        if policy_path is None:
            raise TrustError("an explicit policy or managed target trust is required")
        try:
            policy = load_policy(
                policy_path.resolve(),
                codex_home=codex_home,
                allow_missing_entries=fallback_managed_target,
            )
        except TrustError as exc:
            raise PolicyError(str(exc)) from exc
        if fallback_managed_target and not _is_within(cwd, policy["trusted_project_roots"]):
            approved_user_hooks = policy["trusted_user_hook_paths"]
            policy = managed_target_policy(cwd)
            policy["trusted_user_hook_paths"] = approved_user_hooks
    if not _is_within(cwd, policy["trusted_project_roots"]):
        return {
            "ok": True,
            "eligible": 0,
            "trusted": 0,
            "already_trusted": 0,
            "skipped": "cwd_not_allowlisted",
        }
    source_root = Path(__file__).resolve().parents[1]
    project_config_root = _project_config_root(cwd, policy=policy)
    managed_project_events = _verify_managed_project_hook_files(
        cwd,
        source_root=source_root,
        project_config_root=project_config_root,
    )
    executable = find_codex()

    with AppServer(executable, timeout=timeout) as server:
        # Bootstrap project trust first: without projects.<cwd>.trust_level=trusted,
        # hooks/list omits the project's .codex/hooks.json entries entirely, so any
        # later hash-trust step would silently see zero eligible project hooks.
        # The caller has already verified byte-identical routers and canonical
        # Code Brain-owned manifest entries before any trust write.
        _ensure_project_trust(
            server,
            cwd=cwd,
            policy=policy,
            project_config_root=project_config_root,
            source_root=source_root,
        )

        before = _hooks_for_cwd(server.request("hooks/list", {"cwds": [str(cwd)]}), cwd)
        eligible = eligible_hooks(
            before,
            cwd=cwd,
            project_config_root=project_config_root,
            codex_home=codex_home,
            policy=policy,
            managed_project_events=managed_project_events,
        )
        updates = {
            key: {"trusted_hash": hook["currentHash"]}
            for key, hook in eligible.items()
            if hook.get("trustStatus") in {"untrusted", "modified"}
        }
        if updates:
            server.request(
                "config/batchWrite",
                {
                    "edits": [
                        {
                            "keyPath": "hooks.state",
                            "value": updates,
                            "mergeStrategy": "upsert",
                        }
                    ],
                    "reloadUserConfig": True,
                },
            )
            after = _hooks_for_cwd(server.request("hooks/list", {"cwds": [str(cwd)]}), cwd)
            verified = {str(hook.get("key")): hook for hook in after if isinstance(hook.get("key"), str)}
            for key, expected in updates.items():
                current = verified.get(key, {})
                if current.get("trustStatus") != "trusted" or current.get("currentHash") != expected["trusted_hash"]:
                    raise TrustError("Codex did not persist the requested hook trust update")

    return {
        "ok": True,
        "eligible": len(eligible),
        "trusted": len(updates),
        "already_trusted": len(eligible) - len(updates),
    }


def remove_managed_target_trust(*, cwd: Path, timeout: float) -> dict[str, Any]:
    """Remove only Code Brain project-hook hashes before a managed uninstall.

    Project trust itself is deliberately retained because it may predate Code
    Brain and can still govern preserved foreign hooks. Exact app-server keys
    are selected from the live target before its managed entries disappear;
    global user-hook hashes and foreign project-hook hashes are never touched.
    """

    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise TrustError(f"project directory is unavailable: {cwd}")
    policy = managed_target_policy(cwd)
    project_config_root = _project_config_root(cwd, policy=policy)
    expected_source = project_config_root / ".codex" / "hooks.json"
    executable = find_codex()

    with AppServer(executable, timeout=timeout) as server:
        before = _hooks_for_cwd(server.request("hooks/list", {"cwds": [str(cwd)]}), cwd)
        selected: dict[str, dict[str, Any]] = {}
        for hook in before:
            key = hook.get("key")
            command = hook.get("command")
            source_path_raw = hook.get("sourcePath")
            if not all(isinstance(value, str) and value for value in (key, command, source_path_raw)):
                continue
            wire_event = PROJECT_EVENTS.get(str(hook.get("eventName") or ""))
            if (
                hook.get("source") == "project"
                and wire_event
                and _same_path(Path(source_path_raw), expected_source)
                and command in {_project_command(wire_event), _project_windows_command(wire_event)}
            ):
                selected[key] = hook

        if selected:
            server.request(
                "config/batchWrite",
                {
                    "edits": [
                        {
                            "keyPath": f"hooks.state.{json.dumps(key, ensure_ascii=False)}",
                            "value": None,
                            "mergeStrategy": "replace",
                        }
                        for key in selected
                    ],
                    "reloadUserConfig": True,
                },
            )
            after = _hooks_for_cwd(server.request("hooks/list", {"cwds": [str(cwd)]}), cwd)
            current = {
                str(hook.get("key")): hook
                for hook in after
                if isinstance(hook.get("key"), str)
            }
            if any(current.get(key, {}).get("trustStatus") == "trusted" for key in selected):
                raise TrustError("Codex did not remove the managed project-hook trust state")

    return {"ok": True, "removed": len(selected)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, required=True)
    trust_source = parser.add_mutually_exclusive_group(required=True)
    trust_source.add_argument("--policy", type=Path)
    trust_source.add_argument(
        "--trust-managed-target",
        action="store_true",
        help="trust only an exact Code Brain-managed install target without a persistent policy",
    )
    trust_source.add_argument(
        "--remove-managed-target",
        action="store_true",
        help="remove only exact Code Brain project-hook hashes before uninstall",
    )
    parser.add_argument(
        "--fallback-managed-target",
        action="store_true",
        help="when a policy excludes cwd, use exact managed-target trust while preserving approved user hooks",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.fallback_managed_target and args.policy is None:
        parser.error("--fallback-managed-target requires --policy")
    try:
        if args.remove_managed_target:
            result = remove_managed_target_trust(cwd=args.cwd, timeout=args.timeout)
        else:
            result = trust_hooks(
                cwd=args.cwd,
                policy_path=args.policy,
                timeout=args.timeout,
                managed_target_default=args.trust_managed_target,
                fallback_managed_target=args.fallback_managed_target,
            )
    except TrustError as exc:
        managed_fallback = args.trust_managed_target or args.fallback_managed_target
        if managed_fallback and not isinstance(exc, PolicyError):
            result = {
                "ok": True,
                "eligible": 0,
                "trusted": 0,
                "already_trusted": 0,
                "skipped": "managed_target_not_auto_trusted",
                "reason": str(exc),
            }
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                print(f"codex hook trust: managed target was not eligible; skipped ({exc})")
            return 0
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"codex hook trust failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif args.remove_managed_target:
        print(f"codex hook trust: removed {result['removed']} managed project-hook hash(es)")
    elif result.get("skipped"):
        print(f"codex hook trust: {result['skipped']}; skipped")
    elif result["trusted"]:
        print(f"codex hook trust: trusted {result['trusted']} allowlisted hook(s)")
    else:
        print(f"codex hook trust: {result['already_trusted']} allowlisted hook(s) already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
