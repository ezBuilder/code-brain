from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

PROOF_SCHEMA = 2
PROOF_MAX_AGE_SECONDS = 3600.0
_COMMAND_NAMES = ("bash", "git", "make", "uv", "sops", "age", "git-lfs")


def _path_state(path: Path) -> dict[str, Any] | None:
    try:
        link_state = path.lstat()
        target_state = path.stat()
    except OSError:
        return None
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    return {
        "resolved": resolved,
        "link": [
            int(link_state.st_mode),
            int(link_state.st_size),
            int(link_state.st_mtime_ns),
            int(getattr(link_state, "st_ctime_ns", int(link_state.st_ctime * 1_000_000_000))),
        ],
        "target": [
            int(target_state.st_mode),
            int(target_state.st_size),
            int(target_state.st_mtime_ns),
            int(getattr(target_state, "st_ctime_ns", int(target_state.st_ctime * 1_000_000_000))),
        ],
    }


def environment_payload(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    command_states: dict[str, Any] = {}
    for name in _COMMAND_NAMES:
        resolved = shutil.which(name)
        command_states[name] = {
            "path": resolved,
            "state": _path_state(Path(resolved)) if resolved else None,
        }
    venv_candidates = [
        root / ".ai" / "runtime" / ".venv" / "Scripts" / "python.exe",
        root / ".ai" / "runtime" / ".venv" / "bin" / "python",
    ]
    encrypted = sorted((root / ".ai" / "secrets").glob("*.enc.y*ml"))
    cache = root / ".ai" / "cache"
    try:
        cache_mode = stat.S_IMODE(cache.stat().st_mode) if cache.exists() else None
    except OSError:
        cache_mode = None
    watched_files = [
        root / ".gitattributes",
        root / ".ai" / ".gitattributes",
        root / ".ai" / "runtime" / "pyproject.toml",
        root / "bootstrap.sh",
        root / "bootstrap-code-brain.sh",
        root / ".ai" / "bin" / "ai.ps1",
    ]
    return {
        "os_name": os.name,
        "environment": {
            "PATH": os.environ.get("PATH", ""),
            "PATHEXT": os.environ.get("PATHEXT", ""),
            "PYTHON": os.environ.get("PYTHON", ""),
            "UV_OFFLINE": os.environ.get("UV_OFFLINE", ""),
        },
        "commands": command_states,
        "venv_python": {
            path.relative_to(root).as_posix(): _path_state(path)
            for path in venv_candidates
        },
        "encrypted_secrets": {
            path.relative_to(root).as_posix(): _path_state(path)
            for path in encrypted
        },
        "watched_files": {
            path.relative_to(root).as_posix(): _path_state(path)
            for path in watched_files
        },
        "cache_mode": cache_mode,
    }


def environment_fingerprint(root: Path) -> str:
    encoded = json.dumps(
        environment_payload(root),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
