from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .private_write import (
    atomic_write_private_text,
    private_file_lock,
    read_root_confined_text,
)
from .redact import redact_value

DEFAULT_TTL_SECONDS = 900
REGISTRY_MAX_RECORDS = 100
REGISTRY_MAX_BYTES = 1_000_000


def registry_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "child-processes.jsonl"


def registry_lock_path(root: Path) -> Path:
    return root / ".ai" / "cache" / ".child-processes.lock"


def register_child(root: Path, *, pid: int, kind: str, command: list[str]) -> None:
    if pid <= 0:
        return
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pid": int(pid),
        "kind": str(kind)[:64],
        "command": redact_value([str(part)[:240] for part in command[:12]]),
        "created_at": time.time(),
        "identity": _process_identity(pid),
    }
    try:
        with private_file_lock(registry_lock_path(root), root=root):
            rows: list[dict[str, Any]] = []
            try:
                text, _state = read_root_confined_text(
                    path,
                    root=root,
                    max_bytes=REGISTRY_MAX_BYTES,
                    require_private=True,
                )
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    try:
                        prior = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(prior, dict):
                        rows.append(redact_value(prior))
            except (OSError, UnicodeDecodeError):
                rows = []
            rows = rows[-(REGISTRY_MAX_RECORDS - 1) :]
            rows.append(record)
            atomic_write_private_text(
                path,
                "".join(
                    json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                    for row in rows
                ),
                root=root,
            )
    except OSError:
        return


def cleanup_children(root: Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    try:
        with private_file_lock(registry_lock_path(root), root=root):
            return _cleanup_children_locked(root, ttl_seconds=ttl_seconds)
    except OSError:
        return {"ok": False, "reason": "registry_lock_unavailable"}


def _cleanup_children_locked(root: Path, *, ttl_seconds: int) -> dict[str, Any]:
    path = registry_path(root)
    now = time.time()
    checked = killed = alive = reused = unverified = malformed = 0
    kept: list[dict[str, Any]] = []
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=1_000_000,
            require_private=True,
        )
        lines = text.splitlines()
    except FileNotFoundError:
        return {"ok": True, "checked": 0, "killed": 0, "alive": 0}
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "reason": "registry_unreadable"}
    for line in lines[-200:]:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        try:
            pid = int(record.get("pid") or 0)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if pid <= 0:
            malformed += 1
            continue
        checked += 1
        if not _pid_alive(pid):
            continue
        registered_identity = str(record.get("identity") or "")
        current_identity = _process_identity(pid)
        if not registered_identity or not current_identity:
            unverified += 1
            alive += 1
            kept.append(record)
            continue
        if current_identity != registered_identity:
            reused += 1
            continue
        try:
            created_at = float(record.get("created_at") or now)
        except (TypeError, ValueError):
            malformed += 1
            continue
        age = now - created_at
        if age >= ttl_seconds:
            if _terminate_if_identity_matches(pid, registered_identity):
                killed += 1
                continue
        alive += 1
        kept.append(record)
    try:
        atomic_write_private_text(
            path,
            "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in kept[-REGISTRY_MAX_RECORDS:]
            ),
            root=root,
        )
    except OSError:
        pass
    return {
        "ok": True,
        "checked": checked,
        "killed": killed,
        "alive": alive,
        "reused": reused,
        "unverified": unverified,
        "malformed": malformed,
    }


def _process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        text = proc_stat.read_text(encoding="utf-8")
        close_paren = text.rfind(")")
        fields = text[close_paren + 2 :].split()
        start_ticks = fields[19]
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            boot_id = "unknown-boot"
        raw = f"proc:{boot_id}:{start_ticks}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except (OSError, IndexError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "uid=", "-p", str(pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        return None
    return hashlib.sha256(("ps:" + raw).encode("utf-8")).hexdigest()


def _terminate_if_identity_matches(pid: int, expected_identity: str) -> bool:
    if not expected_identity:
        return False
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if callable(pidfd_open) and callable(pidfd_send_signal):
        try:
            pidfd = pidfd_open(pid, 0)
        except (OSError, TypeError):
            return False
        try:
            # The descriptor is already bound to one process instance. Verify
            # that instance still matches the registration before signaling it.
            if _process_identity(pid) != expected_identity:
                return False
            pidfd_send_signal(pidfd, signal.SIGTERM)
            return True
        except (OSError, ProcessLookupError, PermissionError):
            return False
        finally:
            try:
                os.close(pidfd)
            except OSError:
                pass
    if _process_identity(pid) != expected_identity:
        return False
    return _terminate(pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
