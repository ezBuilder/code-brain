from __future__ import annotations

import hashlib
import json
import marshal
import os
import stat
from pathlib import Path
from typing import Iterable

from .policy import is_ci
from .private_write import atomic_write_private_text, read_root_confined_text
from .redact import SECRET_MATCHER_VERSION, SECRET_PATTERNS, contains_secret

SCAN_STATE_SCHEMA = 3


def _matcher_implementation_digest() -> str:
    try:
        source_path = Path(__import__("ai_core.redact", fromlist=["__file__"]).__file__ or "")
        if source_path.is_file() and not source_path.is_symlink():
            return hashlib.sha256(source_path.read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError):
        pass
    fallback = marshal.dumps(contains_secret.__code__)
    return hashlib.sha256(fallback).hexdigest()


def _matcher_fingerprint() -> str:
    payload = {
        "version": SECRET_MATCHER_VERSION,
        "implementation_sha256": _matcher_implementation_digest(),
        "patterns": [
            {"pattern": pattern.pattern, "flags": int(pattern.flags)}
            for pattern in SECRET_PATTERNS
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _path_state(path: Path) -> dict[str, object] | None:
    try:
        link_state = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(link_state.st_mode):
        try:
            link_target: str | None = os.readlink(path)
        except OSError:
            return None
        kind = "symlink"
        target_state = None
    else:
        if not stat.S_ISREG(link_state.st_mode):
            return None
        kind = "regular"
        link_target = None
        target_state = link_state
    return {
        "kind": kind,
        "link_target": link_target,
        "link": [
            int(link_state.st_dev),
            int(link_state.st_ino),
            int(link_state.st_mode),
            int(link_state.st_size),
            int(link_state.st_mtime_ns),
            int(getattr(link_state, "st_ctime_ns", int(link_state.st_ctime * 1_000_000_000))),
        ],
        "target": (
            [
                int(target_state.st_dev),
                int(target_state.st_ino),
                int(target_state.st_mode),
                int(target_state.st_size),
                int(target_state.st_mtime_ns),
                int(getattr(target_state, "st_ctime_ns", int(target_state.st_ctime * 1_000_000_000))),
            ]
            if target_state is not None
            else None
        ),
    }


def _cache_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "scan-state.json"


def _cache_parent_confined(root: Path, path: Path) -> bool:
    try:
        resolved_root = root.resolve()
        path.parent.resolve().relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return True


def _trusted_cache_file(root: Path) -> Path | None:
    path = _cache_path(root)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if not _cache_parent_confined(root, path):
            return None
        state = path.stat()
        if os.name != "nt" and stat.S_IMODE(state.st_mode) & 0o077:
            return None
        if hasattr(os, "geteuid") and state.st_uid != os.geteuid():
            return None
    except OSError:
        return None
    return path


def _load(root: Path) -> dict[str, dict[str, object]]:
    path = _trusted_cache_file(root)
    if path is None:
        return {}
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=10_000_000,
            require_private=True,
        )
        payload = json.loads(text)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if payload.get("schema") != SCAN_STATE_SCHEMA:
        return {}
    if payload.get("matcher_fingerprint") != _matcher_fingerprint():
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for rel, entry in entries.items():
        if isinstance(rel, str) and isinstance(entry, dict):
            result[rel] = entry
    return result


def _write(root: Path, entries: dict[str, dict[str, object]]) -> None:
    if is_ci():
        return
    path = _cache_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _cache_parent_confined(root, path):
            return
        atomic_write_private_text(
            path,
            json.dumps(
                {
                    "schema": SCAN_STATE_SCHEMA,
                    "matcher_fingerprint": _matcher_fingerprint(),
                    "entries": entries,
                },
                sort_keys=True,
            ),
            root=root,
        )
    except OSError:
        pass


def _scan_stable(path: Path) -> tuple[bool, dict[str, object] | None, str]:
    for _attempt in range(2):
        before = _path_state(path)
        if before is None:
            return False, None, "unreadable"
        try:
            text = (
                str(before.get("link_target") or "")
                if before.get("kind") == "symlink"
                else path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            return False, None, "unreadable"
        hit = contains_secret(text)
        after = _path_state(path)
        if after is not None and after == before:
            return hit, after, "stable"
    # A file changing during both attempts is conservatively surfaced instead
    # of trusting a potentially stale negative result.
    return True, None, "unstable"


def scan_paths(
    root: Path,
    paths: Iterable[Path],
    *,
    incremental: bool,
    update_state: bool,
) -> list[str]:
    return list(
        scan_paths_report(
            root,
            paths,
            incremental=incremental,
            update_state=update_state,
        )["hits"]
    )


def scan_paths_report(
    root: Path,
    paths: Iterable[Path],
    *,
    incremental: bool,
    update_state: bool,
) -> dict[str, object]:
    root = Path(root)
    previous = _load(root) if incremental else {}
    next_entries: dict[str, dict[str, object]] = {}
    hits: list[str] = []
    reused = 0
    rescanned = 0
    unreadable = 0
    unstable = 0
    ordered_paths = sorted(paths)
    for path in ordered_paths:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        current = _path_state(path)
        cached = previous.get(rel)
        if (
            incremental
            and current is not None
            and isinstance(cached, dict)
            and cached.get("state") == current
            and isinstance(cached.get("hit"), bool)
        ):
            hit = bool(cached["hit"])
            stable_state = current
            reused += 1
        else:
            hit, stable_state, status = _scan_stable(path)
            rescanned += 1
            if status == "unreadable":
                unreadable += 1
            elif status == "unstable":
                unstable += 1
        if hit:
            hits.append(rel)
        if stable_state is not None:
            next_entries[rel] = {"state": stable_state, "hit": hit}
    if update_state:
        _write(root, next_entries)
    return {
        "hits": sorted(hits),
        "mode": "incremental" if incremental else "full",
        "total": len(ordered_paths),
        "reused": reused,
        "rescanned": rescanned,
        "unreadable": unreadable,
        "unstable": unstable,
    }
