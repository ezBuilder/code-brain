from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WorkerAlreadyRunning(RuntimeError):
    exit_code = 75


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def lock_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "run" / "worker.lock"


def queue_lock_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "run" / "queue.lock"


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def lock_status(root: Path) -> dict[str, Any]:
    path = lock_path(root)
    rel_path = path.relative_to(root).as_posix()
    hostname_local = socket.gethostname()
    try:
        record = read_lock(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "locked": True,
            "stale": True,
            "cross_host": False,
            "reason": "corrupt",
            "path": rel_path,
            "hostname_local": hostname_local,
            "error": str(exc),
        }
    if not record:
        return {
            "ok": True,
            "locked": False,
            "stale": False,
            "cross_host": False,
            "reason": "no_lock",
            "path": rel_path,
            "hostname_local": hostname_local,
            "error": None,
        }
    pid = int(record.get("pid", 0) or 0)
    hostname = record.get("hostname")
    if not pid or not hostname:
        return {
            "ok": False,
            "locked": True,
            "stale": True,
            "cross_host": False,
            "reason": "corrupt",
            "path": rel_path,
            "pid": pid,
            "owner": record.get("owner"),
            "hostname": hostname,
            "hostname_local": hostname_local,
            "acquired_at": record.get("acquired_at"),
            "error": "missing pid or hostname",
        }
    cross_host = hostname != hostname_local
    alive = True if cross_host else pid_is_alive(pid)
    reason = "cross_host" if cross_host else ("healthy" if alive else "stale_dead_pid")
    return {
        "ok": bool(alive),
        "locked": True,
        "stale": not alive,
        "cross_host": cross_host,
        "reason": reason,
        "path": rel_path,
        "pid": pid,
        "owner": record.get("owner"),
        "hostname": hostname,
        "hostname_local": hostname_local,
        "acquired_at": record.get("acquired_at"),
        "error": None,
    }


def clear_worker_lock(root: Path, *, force: bool = False, reason: str = "operator") -> dict[str, Any]:
    status = lock_status(root)
    if not status.get("locked"):
        return {"ok": True, "action": "no_op", "reason": status.get("reason", "no_lock"), "force": force, "lock": status}
    if status.get("cross_host"):
        payload = {"reason": "cross_host", "force": force, "pid": status.get("pid"), "hostname": status.get("hostname")}
        append_worker_lock_audit(root, action="worker.lock_refused", payload=payload)
        return {
            "ok": False,
            "action": "refused",
            "reason": "cross_host",
            "hint": "clear this lock on the host that owns it",
            "force": force,
            "lock": status,
        }
    stale_or_corrupt = bool(status.get("stale") or status.get("reason") == "corrupt")
    if stale_or_corrupt:
        lock_path(root).unlink(missing_ok=True)
        action = "worker.lock_clear"
        payload = {"reason": status.get("reason"), "requested_reason": reason, "force": force, "pid": status.get("pid")}
        append_worker_lock_audit(root, action=action, payload=payload)
        return {"ok": True, "action": "cleared", "reason": status.get("reason"), "force": force, "lock": status}
    if not force:
        payload = {"reason": "live_local", "force": False, "pid": status.get("pid"), "hostname": status.get("hostname")}
        append_worker_lock_audit(root, action="worker.lock_refused", payload=payload)
        return {
            "ok": False,
            "action": "refused",
            "reason": "live_local",
            "hint": f"rerun with --force only after stopping pid {status.get('pid')}",
            "force": False,
            "lock": status,
        }
    lock_path(root).unlink(missing_ok=True)
    payload = {"reason": "live_local", "requested_reason": reason, "force": True, "pid": status.get("pid")}
    append_worker_lock_audit(root, action="worker.lock_force_clear", payload=payload)
    return {"ok": True, "action": "force_cleared", "reason": "live_local", "force": True, "lock": status}


def append_worker_lock_audit(root: Path, *, action: str, payload: dict[str, Any]) -> None:
    from ai_core.memory import append_audit

    append_audit(root, action=action, category="worker", payload=payload)


@dataclass
class WorkerLock:
    root: Path
    owner: str = "worker"
    pid: int = os.getpid()

    @property
    def path(self) -> Path:
        return lock_path(self.root)

    def acquire(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": self.pid,
            "owner": self.owner,
            "hostname": socket.gethostname(),
            "acquired_at": now_iso(),
        }
        encoded = json.dumps(record, sort_keys=True) + "\n"
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                status = lock_status(self.root)
                if status.get("stale"):
                    self.path.unlink(missing_ok=True)
                    continue
                raise WorkerAlreadyRunning(f"worker already running: pid={status.get('pid')}")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
            return {"ok": True, "path": self.path.relative_to(self.root).as_posix(), **record}

    def release(self) -> dict[str, Any]:
        record = read_lock(self.path)
        if record and int(record.get("pid", 0) or 0) == self.pid:
            self.path.unlink(missing_ok=True)
            return {"ok": True, "released": True}
        return {"ok": True, "released": False}

    def __enter__(self) -> "WorkerLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


def acquire_worker_lock(root: Path, *, owner: str = "worker") -> WorkerLock:
    lock = WorkerLock(root=root, owner=owner)
    lock.acquire()
    return lock


@contextmanager
def queue_lock(root: Path):
    from ..portable import IS_WINDOWS

    path = queue_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if IS_WINDOWS:
            try:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            except (ImportError, OSError):
                pass
        else:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
        yield
    finally:
        try:
            if IS_WINDOWS:
                try:
                    import msvcrt
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                    except OSError:
                        pass
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except (ImportError, OSError):
                    pass
            else:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
        finally:
            os.close(fd)
