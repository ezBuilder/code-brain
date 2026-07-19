from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

from .policy import is_ci
from .private_write import atomic_write_private_text, read_root_confined_text

CACHE_SCHEMA = 2


class GitBaselineUnavailable(RuntimeError):
    """Raised when the authoritative tracked-file baseline cannot be read."""


class TrackedPathList(list[Path]):
    def __init__(self, paths: list[Path], *, source: str) -> None:
        super().__init__(paths)
        self.source = source


FILESYSTEM_BASELINE_SKIP_DIRS = {
    ".git",
    ".chatgpt2codex",
    ".venv",
    "node_modules",
    ".next",
    ".nuxt",
    ".output",
    "dist",
    "build",
    "coverage",
    "logs",
    ".playwright-mcp",
    ".dart_tool",
    "source-maps",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
}

FILESYSTEM_BASELINE_SKIP_PREFIXES = (
    ".ai/cache/",
    ".ai/memory/",
    ".ai/runtime/.venv/",
    ".chatgpt2codex/",
)


def _is_worktree_entry(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _path_state(path: Path) -> dict[str, object] | None:
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


def _git_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.lower().startswith("gitdir:"):
        return None
    raw = line.split(":", 1)[1].strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else (root / path).resolve()


def git_index_fingerprint(root: Path) -> str:
    root = Path(root).resolve()
    dot_git = root / ".git"
    git_dir = _git_dir(root)
    payload = {
        "dot_git": _path_state(dot_git),
        "index": _path_state(git_dir / "index") if git_dir is not None else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "tracked-files.json"


def _cache_parent_confined(root: Path, path: Path) -> bool:
    try:
        path.parent.resolve().relative_to(root.resolve())
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


def _load(root: Path, fingerprint: str) -> TrackedPathList | None:
    path = _trusted_cache_file(root)
    if path is None:
        return None
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=10_000_000,
            require_private=True,
        )
        payload = json.loads(text)
        if payload.get("schema") != CACHE_SCHEMA:
            return None
        if payload.get("git_index_fingerprint") != fingerprint:
            return None
        rels = payload.get("paths")
        if not isinstance(rels, list):
            return None
        paths: list[Path] = []
        for rel in rels:
            if not isinstance(rel, str):
                return None
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                return None
            path = root / rel_path
            if _is_worktree_entry(path):
                paths.append(path)
        return TrackedPathList(sorted(paths), source="cache")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write(root: Path, fingerprint: str, rels: list[str]) -> None:
    path = _cache_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _cache_parent_confined(root, path):
            return
        atomic_write_private_text(
            path,
            json.dumps(
                {
                    "schema": CACHE_SCHEMA,
                    "git_index_fingerprint": fingerprint,
                    "paths": sorted(rels),
                },
                sort_keys=True,
            ),
            root=root,
        )
    except OSError:
        pass


def _filesystem_rel_ignored(rel: Path) -> bool:
    rel_posix = rel.as_posix().rstrip("/") + "/"
    if any(part in FILESYSTEM_BASELINE_SKIP_DIRS for part in rel.parts):
        return True
    return any(rel_posix.startswith(prefix) for prefix in FILESYSTEM_BASELINE_SKIP_PREFIXES)


def _filesystem_baseline(root: Path) -> TrackedPathList:
    paths: list[Path] = []
    for current, dir_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            current_rel = current_path.relative_to(root)
        except ValueError:
            continue
        kept_dirs: list[str] = []
        for name in dir_names:
            child = current_path / name
            child_rel = current_rel / name
            if _filesystem_rel_ignored(child_rel):
                continue
            try:
                mode = child.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISLNK(mode):
                paths.append(child)
            elif stat.S_ISDIR(mode):
                kept_dirs.append(name)
        dir_names[:] = kept_dirs
        for name in file_names:
            child = current_path / name
            child_rel = current_rel / name
            if not _filesystem_rel_ignored(child_rel) and _is_worktree_entry(child):
                paths.append(child)
    return TrackedPathList(sorted(paths), source="filesystem")


def tracked_files(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
) -> TrackedPathList:
    root = Path(root)
    dot_git = root / ".git"
    if not dot_git.exists() and not dot_git.is_symlink():
        return _filesystem_baseline(root)
    before = git_index_fingerprint(root)
    if use_cache:
        cached = _load(root, before)
        if cached is not None:
            return cached
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        # A repository marker exists, so replacing the failed tracked baseline
        # with a recursive walk would silently mix untracked local state into a
        # security decision while still presenting it as a tracked scan.
        raise GitBaselineUnavailable("git ls-files baseline unavailable") from exc
    rels = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    after = git_index_fingerprint(root)
    if update_cache and not is_ci() and before == after:
        _write(root, after, rels)
    return TrackedPathList(
        sorted(path for rel in rels if _is_worktree_entry(path := root / rel)),
        source="git",
    )
