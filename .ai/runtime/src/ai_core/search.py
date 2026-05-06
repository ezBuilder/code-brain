from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from .redact import redact_value

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "cache"}
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".txt",
    ".sh",
    ".ps1",
}


def db_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "code.sqlite"


def connect(root: Path) -> sqlite3.Connection:
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma busy_timeout=5000")
    conn.executescript(
        """
        create table if not exists chunks (
          id integer primary key,
          path text not null,
          sha256 text not null,
          content text not null,
          updated_at text default current_timestamp
        );
        create virtual table if not exists chunks_fts using fts5(path, content, content='chunks', content_rowid='id');
        create table if not exists chunk_meta (
          chunk_id integer primary key,
          kind text not null default 'file',
          bytes integer not null,
          line_count integer not null
        );
        create table if not exists summaries (
          path text primary key,
          summary text not null,
          updated_at text default current_timestamp
        );
        create table if not exists provenance (
          path text primary key,
          processor text not null,
          model_hash text,
          prompt_version text,
          chunker_version text not null,
          confidence real not null
        );
        create table if not exists embeddings_vec0 (
          chunk_id integer primary key,
          disabled_reason text not null default 'embeddings_default_off'
        );
        """
    )


def rebuild(root: Path) -> dict[str, Any]:
    with connect(root) as conn:
        init_schema(conn)
        conn.execute("begin immediate")
        conn.execute("delete from chunks_fts")
        conn.execute("delete from chunk_meta")
        conn.execute("delete from summaries")
        conn.execute("delete from provenance")
        conn.execute("delete from embeddings_vec0")
        conn.execute("delete from chunks")
        indexed = 0
        for path in iter_text_files(root):
            rel = path.relative_to(root).as_posix()
            content = path.read_text(encoding="utf-8")
            redacted = redact_value(content)
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            cursor = conn.execute("insert into chunks(path, sha256, content) values (?, ?, ?)", (rel, digest, redacted))
            chunk_id = int(cursor.lastrowid)
            conn.execute("insert into chunks_fts(rowid, path, content) values (?, ?, ?)", (chunk_id, rel, redacted))
            conn.execute(
                "insert into chunk_meta(chunk_id, kind, bytes, line_count) values (?, ?, ?, ?)",
                (chunk_id, "file", len(redacted.encode("utf-8")), redacted.count("\n") + 1),
            )
            conn.execute(
                "insert into summaries(path, summary) values (?, ?)",
                (rel, summarize(redacted)),
            )
            conn.execute(
                "insert into provenance(path, processor, model_hash, prompt_version, chunker_version, confidence) values (?, ?, ?, ?, ?, ?)",
                (rel, "code-brain-local", None, "extractive-v1", "1", 1.0),
            )
            conn.execute("insert into embeddings_vec0(chunk_id) values (?)", (chunk_id,))
            indexed += 1
        conn.commit()
    return {"ok": True, "db_path": db_path(root).relative_to(root).as_posix(), "indexed": indexed}


def query(root: Path, text: str, *, limit: int = 5) -> dict[str, Any]:
    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            select c.path, snippet(chunks_fts, 1, '[', ']', '...', 12) as snippet, p.processor,
                   p.model_hash, p.prompt_version, p.chunker_version, p.confidence
            from chunks_fts
            join chunks c on c.id = chunks_fts.rowid
            join provenance p on p.path = c.path
            where chunks_fts match ?
            order by rank
            limit ?
            """,
            (escape_fts_query(text), limit),
        ).fetchall()
    return {
        "ok": True,
        "query": text,
        "results": [
            {
                "path": row["path"],
                "snippet": row["snippet"],
                "provenance": {
                    "processor": row["processor"],
                    "model_hash": row["model_hash"],
                    "prompt_version": row["prompt_version"],
                    "chunker_version": row["chunker_version"],
                    "confidence": row["confidence"],
                },
            }
            for row in rows
        ],
    }


def context_pack(root: Path, text: str, *, limit: int = 5) -> dict[str, Any]:
    payload = query(root, text, limit=limit)
    payload["additionalContext"] = "\n".join(f"- {item['path']}: {item['snippet']}" for item in payload["results"])
    return payload


def iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.as_posix() == ".ai/cache/code.sqlite":
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {"AGENTS.md", "CLAUDE.md"}:
            yield path


def summarize(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return ""


def escape_fts_query(text: str) -> str:
    terms = [term.replace('"', "") for term in text.split() if term.strip()]
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)

