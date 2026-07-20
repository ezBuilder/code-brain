from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import __version__
from .private_write import atomic_write_private_text, read_root_confined_bytes, read_root_confined_text


SOURCE_MAX_BYTES = 1024 * 1024
OUTPUT_MAX_BYTES = 2 * 1024 * 1024


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_tree(path: Path) -> str:
    if not path.exists():
        return sha256_text("")
    parts: list[str] = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            rel = item.relative_to(path).as_posix()
            parts.append(f"{rel}:{hashlib.sha256(item.read_bytes()).hexdigest()}")
    return sha256_text("\n".join(parts))


def build_manifest(root: Path) -> dict[str, Any]:
    from .trust import trust_machines_hash

    root = Path(root)
    trust_hash = trust_machines_hash(root)
    return {
        "schema_version": 1,
        "generator": {"name": "code-brain-runtime", "version": __version__},
        "artifacts": [
            {"path": "AGENTS.md", "source_sha": file_sha(root / ".ai" / "AGENTS.md", root=root), "action": "mirror"},
            {"path": "CLAUDE.md", "source_sha": file_sha(root / ".ai" / "AGENTS.md", root=root), "action": "mirror"},
        ],
        "embedding": {"enabled": False, "model": None, "dim": None, "hash": None},
        "sqlite_vec": {"version": None},
        "summarizer": {"mode": "extractive", "model": None, "version": 1, "prompt_version": None},
        "processor": {"versions": {"runtime": __version__}},
        "chunker": {"version": 1},
        "git": {"root_id_normalized": None, "root_hash": None, "eol_policy": "lf", "attributes_hash": file_sha(root / ".ai" / ".gitattributes", root=root)},
        "trust": {"machines_hash": trust_hash},
    }


def file_sha(path: Path, *, root: Path) -> str | None:
    try:
        data, _state = read_root_confined_bytes(
            path,
            root=root,
            max_bytes=SOURCE_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def agent_contract_text(root: Path) -> str:
    root = Path(root)
    source = root / ".ai" / "AGENTS.md"
    try:
        text, _state = read_root_confined_text(
            source,
            root=root,
            max_bytes=SOURCE_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return "# Code Brain Agent Contract\n\nRepo-local agent contract missing: `.ai/AGENTS.md`.\n"
    return text if text.endswith("\n") else text + "\n"


def _existing_output(root: Path, path: Path) -> tuple[bool, str | None, bool]:
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=OUTPUT_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
        return True, text, False
    except FileNotFoundError:
        return False, None, False
    except (OSError, UnicodeDecodeError):
        return True, None, True


def render(root: Path, *, dry_run: bool = False, no_overwrite: bool = False, manifest_only: bool = False) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    manifest = build_manifest(root)
    contract = agent_contract_text(root)
    manifest_text = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    writes = [
        (root / ".ai" / "generated" / "manifest.json", manifest_text),
        (root / "AGENTS.md", contract),
        (root / "CLAUDE.md", contract),
    ]
    if manifest_only:
        writes = writes[:1]
    planned = []
    inspected: list[tuple[Path, str, bool, bool, bool]] = []
    for path, content in writes:
        if len(content.encode("utf-8")) > OUTPUT_MAX_BYTES:
            raise ValueError("render output exceeds size limit")
        exists, existing, unsafe = _existing_output(root, path)
        changed = unsafe or not exists or existing != content
        item = {"path": path.relative_to(root).as_posix(), "exists": exists, "changed": changed}
        if unsafe:
            item["unsafe"] = True
        planned.append(item)
        inspected.append((path, content, exists, changed, unsafe))

    for path, content, exists, changed, _unsafe in inspected:
        if dry_run or not changed:
            continue
        if no_overwrite and exists:
            raise FileExistsError(f"refusing to overwrite {path.relative_to(root).as_posix()}")
        atomic_write_private_text(path, content, root=root)
    return {"planned": planned, "manifest": manifest}
