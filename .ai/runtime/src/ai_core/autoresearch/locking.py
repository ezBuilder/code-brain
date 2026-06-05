"""Global ingest serialization lock (single-user assumption — PRD §12.2.11).

O_EXCL atomic acquire + PID/mtime stale detection (TTL). A single global lock is
the simplest correct primitive for the single-user+agent model (§1.3); per-page
locks are a Stage 1+ optimization. The server orchestrator holds this lock while
committing; agents write to staging only and never hold the lock themselves.
"""
from __future__ import annotations

import contextlib
import errno
import os
import time
from pathlib import Path

from . import storage

STALE_TTL_S = 300  # 5 min


class LockBusy(RuntimeError):
    pass


def _lock_file(root: Path, name: str) -> Path:
    return storage.locks_dir(root) / f"{name}.lock"


def _is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    if age < STALE_TTL_S:
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").split(",", 1)[0])
    except (ValueError, OSError):
        return True  # unreadable + old → reclaim
    try:
        os.kill(pid, 0)
        return False  # owner still alive
    except OSError:
        return True   # owner gone → reclaim


@contextlib.contextmanager
def ingest_lock(root: Path, name: str = "ingest", timeout_s: float = 30.0, poll_s: float = 0.1):
    """Acquire the global ingest lock or raise LockBusy after timeout_s."""
    storage.ensure_tree(root)
    path = _lock_file(root, name)
    deadline = time.time() + timeout_s
    payload = f"{os.getpid()},{time.time()}".encode("utf-8")
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            if _is_stale(path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue
            if time.time() >= deadline:
                raise LockBusy(f"ingest lock busy: {path}")
            time.sleep(poll_s)
    try:
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
