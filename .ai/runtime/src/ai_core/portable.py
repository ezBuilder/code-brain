"""Cross-platform helpers (file locking, detached subprocess) for Code Brain.

Unix uses fcntl.flock; Windows uses msvcrt.locking with the same blocking/non-blocking
semantics. Subprocess detachment differs: Unix uses start_new_session, Windows uses
DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP creationflags.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

IS_WINDOWS = os.name == "nt"


def lock_exclusive_blocking(handle: Any) -> None:
    """Acquire an exclusive blocking lock on a file handle. No-op if locking unavailable."""
    if IS_WINDOWS:
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        except (ImportError, OSError):
            pass
        return
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass


def lock_exclusive_nonblocking(handle: Any) -> bool:
    """Try to acquire an exclusive non-blocking lock. Returns True on success.

    On Windows, msvcrt.LK_NBLCK locks 1 byte from the current file position; behavior
    matches fcntl.LOCK_NB for our single-flight use case.
    """
    if IS_WINDOWS:
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (ImportError, OSError):
            return False
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ImportError, BlockingIOError, OSError):
        return False


def unlock(handle: Any) -> None:
    """Release a lock previously acquired via lock_exclusive_*."""
    if IS_WINDOWS:
        try:
            import msvcrt
            try:
                handle.seek(0)
            except Exception:
                pass
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
        return
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def detached_popen_kwargs() -> dict[str, Any]:
    """Return Popen kwargs that fully detach the child from the parent.

    On Unix we use start_new_session=True. On Windows we set DETACHED_PROCESS
    plus CREATE_NEW_PROCESS_GROUP, which is the equivalent posture.
    """
    if IS_WINDOWS:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {
            "creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def hyphen_encode_path(path_str: str) -> str:
    """Encode an absolute path into the form Claude Code uses for ~/.claude/projects/.

    Unix: /Users/foo/proj  -> -Users-foo-proj
    Windows: C:\\Users\\foo\\proj -> -C--Users-foo-proj  (drive colon kept as `:` collapsed
    to `-`, then leading `-` prepended). Matches Claude Code's observed encoding scheme.
    """
    s = str(path_str).replace("\\", "/").replace(":", "")
    return "-" + s.strip("/").replace("/", "-")
