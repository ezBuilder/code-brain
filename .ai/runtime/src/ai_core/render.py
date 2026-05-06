from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__


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
    trust_hash = hash_tree(root / ".ai" / "trust" / "machines")
    return {
        "schema_version": 1,
        "generator": {"name": "code-brain-runtime", "version": __version__},
        "artifacts": [
            {"path": "AGENTS.md", "source_sha": file_sha(root / ".ai" / "AGENTS.md"), "action": "shim"},
            {"path": "CLAUDE.md", "source_sha": file_sha(root / ".ai" / "AGENTS.md"), "action": "shim"},
        ],
        "embedding": {"enabled": False, "model": None, "dim": None, "hash": None},
        "sqlite_vec": {"version": None},
        "summarizer": {"mode": "extractive", "model": None, "version": 1, "prompt_version": None},
        "processor": {"versions": {"runtime": __version__}},
        "chunker": {"version": 1},
        "git": {"root_id_normalized": None, "root_hash": None, "eol_policy": "lf", "attributes_hash": file_sha(root / ".ai" / ".gitattributes")},
        "trust": {"machines_hash": trust_hash},
    }


def file_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(root: Path, *, dry_run: bool = False, no_overwrite: bool = False) -> dict[str, Any]:
    manifest = build_manifest(root)
    writes = {
        root / "AGENTS.md": "# AGENTS.md\n\nCanonical agent instructions live in `.ai/AGENTS.md`.\n",
        root / "CLAUDE.md": "# CLAUDE.md\n\nCanonical Claude instructions live in `.ai/AGENTS.md`.\n",
        root / ".ai" / "generated" / "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }
    planned = []
    for path, content in writes.items():
        exists = path.exists()
        changed = (not exists) or path.read_text(encoding="utf-8") != content
        planned.append({"path": path.relative_to(root).as_posix(), "exists": exists, "changed": changed})
        if dry_run or not changed:
            continue
        if no_overwrite and exists:
            raise FileExistsError(f"refusing to overwrite {path.relative_to(root).as_posix()}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {"planned": planned, "manifest": manifest}

