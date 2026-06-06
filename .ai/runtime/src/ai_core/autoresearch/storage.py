"""Path helpers + lazy directory creation for the autoresearch data root.

Data root: <project>/.ai/autoresearch/ (reuses the .ai/ git-sync model — git is
SSOT for raw/ + wiki/). Derived artifacts (index/*.db, .state/, .locks/) are
git-ignored. manifest.jsonl lives under index/ so that raw/ content stays
immutable (PRD §12.2.10).
"""
from __future__ import annotations

from pathlib import Path

DATA_DIRNAME = "autoresearch"

RAW = "raw"
WIKI = "wiki"
WIKI_SUBDIRS = ("summaries", "entities", "concepts")
INDEX = "index"
STATE = ".state"
LOCKS = ".locks"
SCHEMA = "schema"

MANIFEST_NAME = "manifest.jsonl"   # under index/ (append/derived layer)
LOG_NAME = "log.md"                # under wiki/
FTS_DB_NAME = "fts.db"             # under index/


def data_root(project_root: Path) -> Path:
    return project_root / ".ai" / DATA_DIRNAME


def ensure_tree(root: Path) -> Path:
    """Create the autoresearch directory tree if missing. Idempotent."""
    (root / RAW).mkdir(parents=True, exist_ok=True)
    for sub in WIKI_SUBDIRS:
        (root / WIKI / sub).mkdir(parents=True, exist_ok=True)
    for d in (INDEX, STATE, LOCKS, SCHEMA):
        (root / d).mkdir(parents=True, exist_ok=True)
    return root


def manifest_path(root: Path) -> Path:
    return root / INDEX / MANIFEST_NAME


def fts_db_path(root: Path) -> Path:
    return root / INDEX / FTS_DB_NAME


def log_path(root: Path) -> Path:
    return root / WIKI / LOG_NAME


def locks_dir(root: Path) -> Path:
    return root / LOCKS


def wiki_root(root: Path) -> Path:
    return root / WIKI


def raw_dir(root: Path) -> Path:
    return root / RAW
