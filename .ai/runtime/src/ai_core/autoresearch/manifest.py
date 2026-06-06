"""Append-only manifest I/O with sha256 idempotency.

Manifest lives under index/ (derived/append layer); raw/ content stays immutable
(PRD §12.2.10). Appends are atomic single-line O_APPEND writes; callers MUST hold
the global ingest lock (see locking.py) so concurrent appends never interleave.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import storage
from .models import RawManifest


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_all(root: Path) -> list[RawManifest]:
    path = storage.manifest_path(root)
    if not path.is_file():
        return []
    out: list[RawManifest] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(RawManifest.from_json(line))
    return out


def find_by_sha(root: Path, sha256: str) -> RawManifest | None:
    path = storage.manifest_path(root)
    if not path.is_file():
        return None
    needle = f'"sha256":"{sha256}"'
    for line in path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            return RawManifest.from_json(line)
    return None


def id_exists(root: Path, source_id: str) -> bool:
    path = storage.manifest_path(root)
    if not path.is_file():
        return False
    needle = f'"id":"{source_id}"'
    for line in path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            return True
    return False


def find_by_id(root: Path, source_id: str) -> RawManifest | None:
    path = storage.manifest_path(root)
    if not path.is_file():
        return None
    needle = f'"id":"{source_id}"'
    for line in path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            return RawManifest.from_json(line)
    return None


def append(root: Path, record: RawManifest) -> bool:
    """Append a manifest record. Idempotent on sha256 — returns False if duplicate.

    Caller must hold the global ingest lock. O_APPEND guarantees the line is not
    interleaved with a concurrent writer's line at the OS level.
    """
    if find_by_sha(root, record.sha256) is not None:
        return False
    storage.ensure_tree(root)
    path = storage.manifest_path(root)
    line = (record.to_json() + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return True
