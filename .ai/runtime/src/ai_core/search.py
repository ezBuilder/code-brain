from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .config import load_config
from .redact import redact_value

SCHEMA_VERSION = 3
SKIP_DIRS = {
    ".git",
    ".venv",
    ".playwright-cli",
    ".playwright-mcp",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "cache",
    "node_modules",
    "site-packages",
    "vendor",
    ".next",
    ".nuxt",
    ".output",
    ".dart_tool",
    ".gradle",
    "Pods",
    "DerivedData",
    "dist",
    "build",
    "coverage",
    "logs",
    "generated",
}
MAX_TEXT_BYTES = 100_000
SKIP_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "Podfile.lock",
    "Gemfile.lock",
}
SKIP_SUFFIXES = {
    ".map",
    ".min.js",
    ".snap",
}
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".dart",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".css",
    ".scss",
    ".html",
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


def init_schema(conn: sqlite3.Connection, *, migrate_legacy: bool = False) -> None:
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma busy_timeout=5000")
    current_version = int(conn.execute("pragma user_version").fetchone()[0])
    existing_chunk_columns = [
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("pragma table_info(chunks)").fetchall()
    ]
    legacy_schema = "content" in existing_chunk_columns or (
        existing_chunk_columns and "summary" not in existing_chunk_columns
    )
    needs_migration = (current_version and current_version < SCHEMA_VERSION) or legacy_schema
    if needs_migration and not migrate_legacy:
        raise RuntimeError("legacy search index schema; run ai index rebuild")
    if needs_migration:
        drop_schema(conn)
    create_schema(conn)


def drop_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        drop table if exists chunks_fts;
        drop table if exists chunk_meta;
        drop table if exists summaries;
        drop table if exists provenance;
        drop table if exists embeddings_vec0;
        drop table if exists chunks;
        """
    )


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists chunks (
          id integer primary key,
          path text not null,
          sha256 text not null,
          summary text not null,
          updated_at text default current_timestamp
        );
        create virtual table if not exists chunks_fts using fts5(path, content, content='', tokenize="porter unicode61 remove_diacritics 2");
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
    conn.execute(f"pragma user_version={SCHEMA_VERSION}")


def rebuild(root: Path) -> dict[str, Any]:
    with connect(root) as conn:
        init_schema(conn, migrate_legacy=True)
        conn.execute("begin immediate")
        drop_schema(conn)
        create_schema(conn)
        indexed = 0
        for path in iter_text_files(root):
            rel = path.relative_to(root).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            redacted = redact_value(content)
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            summary = summarize(redacted)
            cursor = conn.execute("insert into chunks(path, sha256, summary) values (?, ?, ?)", (rel, digest, summary))
            chunk_id = int(cursor.lastrowid)
            conn.execute("insert into chunks_fts(rowid, path, content) values (?, ?, ?)", (chunk_id, rel, redacted))
            conn.execute(
                "insert into chunk_meta(chunk_id, kind, bytes, line_count) values (?, ?, ?, ?)",
                (chunk_id, "file", len(redacted.encode("utf-8")), redacted.count("\n") + 1),
            )
            conn.execute(
                "insert into summaries(path, summary) values (?, ?)",
                (rel, summary),
            )
            conn.execute(
                "insert into provenance(path, processor, model_hash, prompt_version, chunker_version, confidence) values (?, ?, ?, ?, ?, ?)",
                (rel, "code-brain-local", None, "extractive-v1", "1", 1.0),
            )
            conn.execute("insert into embeddings_vec0(chunk_id) values (?)", (chunk_id,))
            indexed += 1
        conn.commit()
        conn.execute("vacuum")
    return {"ok": True, "db_path": db_path(root).relative_to(root).as_posix(), "indexed": indexed}


def query(root: Path, text: str, *, limit: int = 5) -> dict[str, Any]:
    retriever = configured_retriever(root)
    if retriever != "bm25":
        raise RuntimeError(f"search retriever '{retriever}' is not implemented; use retriever: bm25")
    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            select c.path, c.sha256, c.summary, p.processor,
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
                "snippet": snippet_from_file(root, row["path"], text, fallback=row["summary"], expected_sha=row["sha256"]),
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


def observability(root: Path, *, query_text: str | None = None, limit: int = 5) -> dict[str, Any]:
    path = db_path(root)
    payload: dict[str, Any] = {
        "ok": True,
        "db_path": path.relative_to(root).as_posix(),
        "exists": path.exists(),
        "retriever": configured_retriever(root),
        "schema_version": None,
        "sqlite_bytes": 0,
        "sqlite_wal_bytes": 0,
        "indexed_files": 0,
        "indexed_bytes": 0,
        "summary_bytes": 0,
        "embeddings": {"enabled": False, "disabled_rows": 0},
    }
    if not path.exists():
        payload["ok"] = False
        payload["reason"] = "missing_index"
        return payload
    payload["sqlite_bytes"] = path.stat().st_size
    wal = path.with_name(path.name + "-wal")
    payload["sqlite_wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    with connect(root) as conn:
        init_schema(conn)
        payload["schema_version"] = int(conn.execute("pragma user_version").fetchone()[0])
        row = conn.execute(
            """
            select count(*) as indexed_files,
                   coalesce(sum(m.bytes), 0) as indexed_bytes,
                   coalesce(sum(length(c.summary)), 0) as summary_bytes
            from chunks c
            left join chunk_meta m on m.chunk_id = c.id
            """
        ).fetchone()
        payload["indexed_files"] = int(row["indexed_files"])
        payload["indexed_bytes"] = int(row["indexed_bytes"])
        payload["summary_bytes"] = int(row["summary_bytes"])
        disabled = conn.execute("select count(*) as count from embeddings_vec0").fetchone()
        payload["embeddings"] = {"enabled": False, "disabled_rows": int(disabled["count"])}
    if query_text:
        pack = context_pack(root, query_text, limit=limit)
        result_paths = [item["path"] for item in pack["results"]]
        matched_bytes = indexed_bytes_for_paths(root, result_paths)
        context_bytes = len(pack.get("additionalContext", "").encode("utf-8"))
        payload["query"] = {
            "text": query_text,
            "limit": limit,
            "result_count": len(result_paths),
            "result_paths": result_paths,
            "matched_indexed_bytes": matched_bytes,
            "context_bytes": context_bytes,
            "context_to_matched_bytes_ratio": round(context_bytes / matched_bytes, 4) if matched_bytes else None,
            "stale_results": [
                item["path"] for item in pack["results"] if str(item.get("snippet", "")).startswith("[stale index:")
            ],
            "additionalContext": pack.get("additionalContext", ""),
        }
    return payload


def indexed_bytes_for_paths(root: Path, paths: list[str]) -> int:
    if not paths:
        return 0
    placeholders = ",".join("?" for _ in paths)
    with connect(root) as conn:
        init_schema(conn)
        row = conn.execute(
            f"""
            select coalesce(sum(m.bytes), 0) as bytes
            from chunks c
            join chunk_meta m on m.chunk_id = c.id
            where c.path in ({placeholders})
            """,
            paths,
        ).fetchone()
    return int(row["bytes"])


def configured_retriever(root: Path) -> str:
    config = load_config(root)
    search_config = config.get("search", {})
    if not isinstance(search_config, dict):
        raise ValueError("search config must be a mapping")
    retriever = search_config.get("retriever", "bm25")
    if not isinstance(retriever, str):
        raise ValueError("search.retriever must be a string")
    return retriever


def iter_text_files(root: Path):
    for path in candidate_files(root):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in SKIP_NAMES:
            continue
        if any(path.name.endswith(suffix) for suffix in SKIP_SUFFIXES):
            continue
        if rel.as_posix() == ".ai/cache/code.sqlite":
            continue
        if path.stat().st_size > MAX_TEXT_BYTES:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {"AGENTS.md", "CLAUDE.md"}:
            yield path


def candidate_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(path for path in root.rglob("*") if path.is_file())
    rels = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    return sorted(path for rel in rels if (path := root / rel).is_file())


def summarize(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return ""


def snippet_from_file(root: Path, rel_path: str, query_text: str, *, fallback: str, expected_sha: str | None = None) -> str:
    path = root / rel_path
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"[stale index: source unavailable; run ai index rebuild] {fallback}"
    redacted = str(redact_value(content))
    if expected_sha:
        current_sha = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
        if current_sha != expected_sha:
            return f"[stale index: source changed; run ai index rebuild] {fallback}"
    terms = [term.casefold() for term in query_text.split() if term.strip()]
    lowered = redacted.casefold()
    hit_at = -1
    for term in terms:
        hit_at = lowered.find(term)
        if hit_at >= 0:
            break
    if hit_at < 0:
        return summarize(redacted)
    start = max(0, hit_at - 120)
    end = min(len(redacted), hit_at + 240)
    snippet = redacted[start:end].replace("\n", "\\n")
    if start > 0:
        snippet = "..." + snippet
    if end < len(redacted):
        snippet += "..."
    return snippet[:420]


def escape_fts_query(text: str) -> str:
    terms = [term.replace('"', "") for term in text.split() if term.strip()]
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)
