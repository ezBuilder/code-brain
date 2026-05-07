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
    try:
        record = read_lock(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "locked": True, "stale": True, "path": path.relative_to(root).as_posix(), "error": str(exc)}
    if not record:
        return {"ok": True, "locked": False, "stale": False, "path": path.relative_to(root).as_posix()}
    pid = int(record.get("pid", 0) or 0)
    alive = pid_is_alive(pid)
    return {
        "ok": alive,
        "locked": True,
        "stale": not alive,
        "path": path.relative_to(root).as_posix(),
        "pid": pid,
        "owner": record.get("owner"),
        "hostname": record.get("hostname"),
        "acquired_at": record.get("acquired_at"),
    }


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
    path = queue_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except ImportError:
                pass
        finally:
            os.close(fd)
