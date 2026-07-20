from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat as stat_module
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import load_config
from .policy import is_ci
from .private_write import (
    atomic_write_private_bytes,
    atomic_write_private_text,
    ensure_root_confined_directory,
    ensure_root_confined_private_regular_file,
    private_file_try_lock,
    read_root_confined_text,
    validate_root_confined_directory,
    validate_root_confined_regular_file,
)
from .redact import redact_value

SCHEMA_VERSION = 8
CANDIDATE_CACHE_SCHEMA = 3
CANDIDATE_CACHE_MAX_AGE_SECONDS = 60.0
import os as _os
try:
    SNIPPET_MAX_BYTES = max(80, min(2048, int(_os.environ.get("AI_SNIPPET_MAX_BYTES", "240"))))
except (ValueError, TypeError):
    SNIPPET_MAX_BYTES = 240
SEARCH_QUERY_MAX_CHARS = 4096
SEARCH_QUERY_MAX_TERMS = 128
SEARCH_QUERY_ECHO_MAX_CHARS = 512
SEARCH_RESULT_DEFAULT = 5
SEARCH_RESULT_MAX = 100
SEARCH_DENSE_CANDIDATE_MAX = SEARCH_RESULT_MAX * 8
RG_OUTPUT_MAX_BYTES = 256 * 1024
RG_OUTPUT_MAX_EVENTS = 512
RG_TIMEOUT_SECONDS = 10.0
GIT_CANDIDATE_MAX_BYTES = 16 * 1024 * 1024
GIT_CANDIDATE_MAX_PATHS = 200_000
GIT_CANDIDATE_TIMEOUT_SECONDS = 10.0
FILESYSTEM_CANDIDATE_MAX_VISITED = 1_000_000
FILESYSTEM_CANDIDATE_TIMEOUT_SECONDS = 10.0
# Path prefixes excluded from FTS5 indexing. These are runtime-accumulating
# operational logs / caches whose content changes on every CLI invocation,
# so indexing them creates a perpetual `index_freshness` staleness loop.
SKIP_PATH_PREFIXES = (
    ".ai/memory/",
    ".ai/cache/",
    ".ai/skills/",
    ".ai/precall_rules/",
    ".ai/agents_catalog/",
    ".codebrain/",
)
SKIP_DIRS = {
    ".git",
    ".chatgpt2codex",
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
MTIME_STALE_GRACE_SECONDS = 2.0
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


_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _prepare_index_storage(root: Path) -> Path:
    """Prepare private confined SQLite main/sidecar files without following links."""
    path = db_path(root)
    ensure_root_confined_directory(path.parent, root=root, mode=0o700)
    ensure_root_confined_private_regular_file(
        path,
        root=root,
        replace_unsafe=True,
    )
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        ensure_root_confined_private_regular_file(
            Path(str(path) + suffix),
            root=root,
            replace_unsafe=True,
        )
    return path


def connect(root: Path) -> sqlite3.Connection:
    path = _prepare_index_storage(root)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _connection_scope(root: Path):
    """Commit/rollback like sqlite's context manager and always close."""
    conn = connect(root)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _is_corrupt_index_error(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        base_code = code & 0xFF
        if base_code in {
            int(getattr(sqlite3, "SQLITE_CORRUPT", 11)),
            int(getattr(sqlite3, "SQLITE_NOTADB", 26)),
        }:
            return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "file is not a database",
            "database disk image is malformed",
            "malformed database schema",
            "database corruption",
        )
    )


def _reset_index_storage(root: Path) -> None:
    path = db_path(root)
    ensure_root_confined_directory(path.parent, root=root, mode=0o700)
    for target in (path, *(Path(str(path) + suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES)):
        atomic_write_private_bytes(target, b"", root=root)


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
        drop table if exists code_symbols;
        drop table if exists code_calls;
        drop table if exists file_state;
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
        create virtual table if not exists chunks_fts using fts5(path, content, content='', contentless_delete=1, tokenize="porter unicode61 remove_diacritics 2");
        create table if not exists chunk_meta (
          chunk_id integer primary key,
          kind text not null default 'file',
          bytes integer not null,
          line_count integer not null,
          qualname text,
          start_line integer,
          end_line integer
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
        create table if not exists file_state (
          path text primary key,
          size integer not null,
          mtime_ns integer not null,
          ctime_ns integer not null,
          sha256 text not null
        );
        create table if not exists embeddings_vec0 (
          chunk_id integer primary key,
          disabled_reason text not null default 'embeddings_default_off',
          vector blob,
          model_name text,
          vector_dim integer,
          created_at text
        );
        create index if not exists embeddings_vec0_model_idx on embeddings_vec0(model_name);
        create table if not exists code_symbols (
          id integer primary key,
          path text not null,
          qualname text not null,
          kind text not null,
          lineno integer not null,
          end_lineno integer not null,
          parent text,
          lang text not null default 'python'
        );
        create index if not exists code_symbols_path_idx on code_symbols(path);
        create index if not exists code_symbols_qualname_idx on code_symbols(qualname);
        create index if not exists code_symbols_lang_idx on code_symbols(lang);
        create table if not exists code_calls (
          id integer primary key,
          path text not null,
          caller text not null,
          callee text not null,
          lineno integer not null,
          lang text not null default 'python'
        );
        create index if not exists code_calls_callee_idx on code_calls(callee);
        create index if not exists code_calls_caller_idx on code_calls(caller);
        create index if not exists code_calls_lang_idx on code_calls(lang);
        """
    )
    conn.execute(f"pragma user_version={SCHEMA_VERSION}")


def rebuild(
    root: Path,
    *,
    single_flight: bool = False,
    incremental: bool = False,
    paths: set[str] | None = None,
) -> dict[str, Any]:
    def execute() -> dict[str, Any]:
        try:
            return (
                _rebuild_incremental_inner(root, paths=paths)
                if incremental
                else _rebuild_inner(root)
            )
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt_index_error(exc):
                raise
            try:
                _reset_index_storage(root)
                result = _rebuild_inner(root)
            except (OSError, sqlite3.DatabaseError) as retry_exc:
                return {
                    "ok": False,
                    "reason": "corrupt_index_recovery_failed",
                    "detail": type(retry_exc).__name__,
                    "db_path": db_path(root).relative_to(root).as_posix(),
                }
            result["recovered_corrupt_index"] = True
            return result

    if single_flight:
        lock_path = root / ".ai" / "cache" / ".rebuild.lock"
        entered_lock = False
        try:
            with private_file_try_lock(lock_path, root=root) as acquired:
                entered_lock = True
                if not acquired:
                    return {
                        "ok": True,
                        "skipped": "another rebuild in progress",
                        "db_path": db_path(root).relative_to(root).as_posix(),
                    }
                return execute()
        except OSError:
            return {
                "ok": False,
                "reason": (
                    "index_storage_unavailable"
                    if entered_lock
                    else "rebuild_lock_unavailable"
                ),
                "db_path": db_path(root).relative_to(root).as_posix(),
            }
    return execute()


def _rebuild_incremental_inner(root: Path, *, paths: set[str] | None = None) -> dict[str, Any]:
    """Re-index only files whose redacted-content sha256 has changed.

    Drops chunks for deleted files; updates chunks for changed files; leaves
    unchanged files untouched. Codegraph + embedding row are rebuilt for the
    changed set too (drop + insert) so they never diverge from the FTS row.

    Schema v8 enables FTS5 contentless_delete, so changed/deleted files can
    remove just their own FTS rows. When ``paths`` is provided, only those
    worktree-relative paths are considered; otherwise the whole text-file set
    is scanned for drift/deletions.

    If the schema is out of date or empty, falls back to full rebuild.
    """
    db_p = db_path(root)
    if not db_p.exists():
        return _rebuild_inner(root)

    with _connection_scope(root) as conn:
        try:
            init_schema(conn, migrate_legacy=False)
        except RuntimeError:
            # legacy schema → caller must do a full rebuild
            return _rebuild_inner(root)
        existing = {
            row["path"]: (int(row["id"]), str(row["sha256"]))
            for row in conn.execute(
                """
                select c.id, c.path, c.sha256
                from chunks c
                join chunk_meta m on m.chunk_id = c.id
                where m.kind = 'file'
                """
            ).fetchall()
        }
        if not existing:
            return _rebuild_inner(root)

        conn.execute("begin immediate")
        seen: set[str] = set()
        changed = 0
        added = 0
        unchanged = 0
        candidate_paths = (
            _target_text_files(root, paths)
            if paths is not None
            else (
                (path.relative_to(root).as_posix(), path)
                for path in iter_text_files(root, use_cache=False, update_cache=True)
            )
        )
        for rel, path in candidate_paths:
            loaded = _read_indexable_text(root, path)
            if loaded is None:
                continue
            content, source_state = loaded
            seen.add(rel)
            redacted = redact_value(content)
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            existing_pair = existing.get(rel)
            if existing_pair is not None and existing_pair[1] == digest:
                _upsert_file_state(conn, rel, path, digest, state=source_state)
                unchanged += 1
                continue
            # Need to (re)write this file: drop dependent rows, including the
            # row-level FTS entries for the file and its function chunks.
            if existing_pair is not None:
                _delete_chunk_rows(conn, existing_pair[0])
                # summaries/provenance/codegraph have path-keyed UNIQUE rows; clear them too
                conn.execute("delete from summaries where path = ?", (rel,))
                conn.execute("delete from provenance where path = ?", (rel,))
                conn.execute("delete from code_symbols where path = ?", (rel,))
                conn.execute("delete from code_calls where path = ?", (rel,))
                # Also delete function chunks for this file (they have path like "file.ext:qualname")
                # Applies to any supported language that has function-level chunks
                chunk_ids = conn.execute(
                    "select id from chunks where path like ?", (f"{rel}:%",)
                ).fetchall()
                for (cid,) in chunk_ids:
                    _delete_chunk_rows(conn, cid)
            summary = summarize(redacted)
            cursor = conn.execute(
                "insert into chunks(path, sha256, summary) values (?, ?, ?)",
                (rel, digest, summary),
            )
            chunk_id = int(cursor.lastrowid)
            conn.execute(
                "insert into chunks_fts(rowid, path, content) values (?, ?, ?)",
                (chunk_id, rel, redacted),
            )
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
            _insert_chunk_embedding(conn, chunk_id, redacted, root)
            _insert_codegraph_for_path(conn, rel, redacted, path)
            # For supported languages, also insert function/class level chunks (hybrid chunking)
            _insert_function_chunks(conn, rel, content, chunk_id, root=root)
            _upsert_file_state(conn, rel, path, digest, state=source_state)
            if existing_pair is not None:
                changed += 1
            else:
                added += 1
        # Cleanup deleted files. In targeted mode, only target paths are allowed
        # to delete rows; full mode compares against the complete seen set.
        deleted = 0
        delete_candidates = paths if paths is not None else set(existing)
        for rel in sorted(delete_candidates):
            existing_pair = existing.get(rel)
            if existing_pair is None or rel in seen:
                continue
            cid, _digest = existing_pair
            if paths is not None:
                target = root / rel
                if _is_indexable_text_file(root, target):
                    continue
            if rel not in seen:
                _delete_chunk_rows(conn, cid)
                conn.execute("delete from summaries where path = ?", (rel,))
                conn.execute("delete from provenance where path = ?", (rel,))
                conn.execute("delete from code_symbols where path = ?", (rel,))
                conn.execute("delete from code_calls where path = ?", (rel,))
                conn.execute("delete from file_state where path = ?", (rel,))
                # Also delete function chunks for this file
                chunk_ids = conn.execute(
                    "select id from chunks where path like ?", (f"{rel}:%",)
                ).fetchall()
                for (func_cid,) in chunk_ids:
                    _delete_chunk_rows(conn, func_cid)
                deleted += 1
        conn.commit()
    if changed or added or deleted:
        _mark_index_generation(root)
    return {
        "ok": True,
        "db_path": db_p.relative_to(root).as_posix(),
        "incremental": True,
        "unchanged": unchanged,
        "changed": changed,
        "added": added,
        "deleted": deleted,
        "indexed": unchanged + changed + added,
        "targeted": paths is not None,
    }


def _delete_chunk_rows(conn: sqlite3.Connection, chunk_id: int) -> None:
    """Delete a chunk and its dependent rows, including its FTS row."""
    conn.execute("delete from chunks_fts where rowid = ?", (chunk_id,))
    conn.execute("delete from chunk_meta where chunk_id = ?", (chunk_id,))
    conn.execute("delete from embeddings_vec0 where chunk_id = ?", (chunk_id,))
    conn.execute("delete from chunks where id = ?", (chunk_id,))


def _rebuild_inner(root: Path) -> dict[str, Any]:
    with _connection_scope(root) as conn:
        init_schema(conn, migrate_legacy=True)
        conn.execute("begin immediate")
        drop_schema(conn)
        create_schema(conn)
        indexed = 0
        for path in iter_text_files(root, use_cache=False, update_cache=True):
            rel = path.relative_to(root).as_posix()
            loaded = _read_indexable_text(root, path)
            if loaded is None:
                continue
            content, source_state = loaded
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
            _insert_chunk_embedding(conn, chunk_id, redacted, root)
            _insert_codegraph_for_path(conn, rel, redacted, path)
            # For supported languages, also insert function/class level chunks (hybrid chunking)
            _insert_function_chunks(conn, rel, content, chunk_id, root=root)
            _upsert_file_state(conn, rel, path, digest, state=source_state)
            indexed += 1
        conn.commit()
        conn.execute("vacuum")
    _mark_index_generation(root)
    return {"ok": True, "db_path": db_path(root).relative_to(root).as_posix(), "indexed": indexed}


def _mark_index_generation(root: Path) -> None:
    marker = root / ".ai" / "cache" / "code-index-generation"
    atomic_write_private_text(marker, str(time.time_ns()) + "\n", root=root)


def _file_stat_values(
    path: Path,
    stat_result: os.stat_result | None = None,
) -> tuple[int, int, int]:
    stat_result = stat_result or path.stat()
    return (
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(getattr(stat_result, "st_ctime_ns", int(stat_result.st_ctime * 1_000_000_000))),
    )


def _upsert_file_state(
    conn: sqlite3.Connection,
    rel: str,
    path: Path,
    digest: str,
    *,
    state: os.stat_result | None = None,
) -> None:
    size, mtime_ns, ctime_ns = _file_stat_values(path, state)
    conn.execute(
        """
        insert into file_state(path, size, mtime_ns, ctime_ns, sha256)
        values (?, ?, ?, ?, ?)
        on conflict(path) do update set
          size = excluded.size,
          mtime_ns = excluded.mtime_ns,
          ctime_ns = excluded.ctime_ns,
          sha256 = excluded.sha256
        """,
        (rel, size, mtime_ns, ctime_ns, digest),
    )


def _codegraph_enabled() -> bool:
    raw = os.environ.get("AI_SEARCH_CODEGRAPH", "1")
    return str(raw).strip().lower() not in {"0", "off", "false", "no"}


def _cast_chunk_active(path: str, root: Path | None) -> bool:
    """Whether cAST chunking should run for ``path``.

    True when EITHER the opt-in env flag (``AI_AST_CHUNK``) is truthy OR a
    persisted self-validation verdict (cast_eval) enabled cAST for this repo —
    so a passing eval auto-enables cAST without the env flag. Both checks are
    fail-soft; with env unset and no verdict this returns False and the existing
    chunking path stays byte-identical.
    """
    try:
        from .ast_chunker import ast_chunk_enabled

        if ast_chunk_enabled():
            return True
    except Exception:
        pass
    if root is None:
        return False
    try:
        from . import cast_eval

        return bool(cast_eval.verdict(root))
    except Exception:
        return False


def _insert_function_chunks(
    conn: sqlite3.Connection, path: str, source_text: str, file_chunk_id: int,
    *, root: Path | None = None,
) -> None:
    """Insert function/class level chunks for a source file (hybrid chunking).

    Supports Python, JS/TS, Go, Rust via language-specific extraction.

    For each function/class extracted from the source:
      1. Create a new chunk row with the symbol text
      2. Insert into chunks_fts for full-text search
      3. Update chunk_meta with symbol metadata (qualname, start/end lines, kind)

    This preserves file-level chunks while adding function-level chunks.
    Best-effort: silently skips if parsing fails or ast-grep is unavailable.
    """
    # Determine language from file extension
    lang = None
    if path.endswith(".py"):
        lang = "py"
    elif path.endswith((".js", ".jsx")):
        lang = "js"
    elif path.endswith((".ts", ".tsx")):
        lang = "ts"
    elif path.endswith(".go"):
        lang = "go"
    elif path.endswith(".rs"):
        lang = "rs"
    else:
        return

    # cAST AST-aware chunking (Python pilot). Default OFF: when AI_AST_CHUNK is
    # unset/falsy AND no cast_eval verdict has enabled it, ast_chunks stays None
    # and the existing path runs byte-identically. A truthy env flag OR a passing
    # self-validation verdict (cast_eval.verdict) enables it for .py files; an
    # empty list (parse failure) falls back to the default chunker.
    func_chunks: list[dict[str, Any]] | None = None
    ast_chunks: list[dict[str, Any]] | None = None
    if lang == "py" and _cast_chunk_active(path, root):
        try:
            from .ast_chunker import chunk_python

            ast_chunks = chunk_python(source_text)
        except Exception:
            ast_chunks = None
    if ast_chunks is not None:
        func_chunks = _adapt_ast_chunks(path, ast_chunks)
    if not func_chunks:
        func_chunks = _function_chunks_for_lang(path, source_text, lang)
    if not func_chunks:
        return

    for func in func_chunks:
        qualname = func["qualname"]
        chunk_text = func["text"]
        start_line = func["start_line"]
        end_line = func["end_line"]
        kind = func["kind"]

        # Create a unique summary for the function chunk
        summary = f"{kind} {qualname} ({start_line}-{end_line})"

        # Compute sha256 of chunk text (for dedup purposes, though we allow multiples per file)
        chunk_digest = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()

        try:
            # Insert chunk row with a fake "path" to make it searchable
            # Use a canonical path like "path.py:qualname" for clarity
            chunk_path = f"{path}:{qualname}"
            cursor = conn.execute(
                "insert into chunks(path, sha256, summary) values (?, ?, ?)",
                (chunk_path, chunk_digest, summary),
            )
            chunk_id = int(cursor.lastrowid)

            # Insert into FTS index
            conn.execute(
                "insert into chunks_fts(rowid, path, content) values (?, ?, ?)",
                (chunk_id, chunk_path, chunk_text),
            )

            # Insert into chunk_meta with function metadata
            conn.execute(
                "insert into chunk_meta(chunk_id, kind, bytes, line_count, qualname, start_line, end_line) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    kind,
                    len(chunk_text.encode("utf-8")),
                    chunk_text.count("\n") + 1,
                    qualname,
                    start_line,
                    end_line,
                ),
            )
        except sqlite3.IntegrityError:
            # Duplicate or constraint violation; skip this chunk
            pass


def _insert_function_chunks_for_python(
    conn: sqlite3.Connection, path: str, source_text: str, file_chunk_id: int
) -> None:
    """Deprecated: use _insert_function_chunks() instead.

    Kept for backward compatibility; simply delegates to the new function.
    """
    _insert_function_chunks(conn, path, source_text, file_chunk_id)


def _insert_codegraph_for_path(conn: sqlite3.Connection, rel: str, redacted_text: str, abs_path: Path) -> None:
    """Insert function/class symbols + call edges for Python and multi-language source files.

    Default ON (AI_SEARCH_CODEGRAPH=1). Supports Python, JS, TS, Go, Rust via ast-grep.
    Skips unsupported files and any file the AST parser rejects (best-effort indexer behavior).
    """
    if not _codegraph_enabled():
        return

    # Detect language from file extension
    lang = None
    extract_symbols_func = None
    extract_calls_func = None

    if rel.endswith(".py"):
        lang = "python"
        try:
            from .codegraph import extract_symbols, extract_calls
            extract_symbols_func = extract_symbols
            extract_calls_func = extract_calls
        except Exception:
            return
    elif rel.endswith((".js", ".jsx")):
        lang = "javascript"
        try:
            from .astgrep_integration import extract_symbols_js, extract_calls_js
            extract_symbols_func = extract_symbols_js
            extract_calls_func = extract_calls_js
        except Exception:
            return
    elif rel.endswith((".ts", ".tsx")):
        lang = "typescript"
        try:
            from .astgrep_integration import extract_symbols_ts, extract_calls_ts
            extract_symbols_func = extract_symbols_ts
            extract_calls_func = extract_calls_ts
        except Exception:
            return
    elif rel.endswith(".go"):
        lang = "go"
        try:
            from .astgrep_integration import extract_symbols_go, extract_calls_go
            extract_symbols_func = extract_symbols_go
            extract_calls_func = extract_calls_go
        except Exception:
            return
    elif rel.endswith(".rs"):
        lang = "rust"
        try:
            from .astgrep_integration import extract_symbols_rs, extract_calls_rs
            extract_symbols_func = extract_symbols_rs
            extract_calls_func = extract_calls_rs
        except Exception:
            return
    else:
        # Unsupported language
        return

    try:
        if lang == "python":
            # Python AST extraction works with source text
            syms = extract_symbols_func(redacted_text, path=rel)
            for s in syms:
                conn.execute(
                    "insert into code_symbols(path, qualname, kind, lineno, end_lineno, parent, lang) "
                    "values (?, ?, ?, ?, ?, ?, ?)",
                    (s.path, s.qualname, s.kind, s.lineno, s.end_lineno, s.parent, lang),
                )
            calls = extract_calls_func(redacted_text, path=rel)
            for c in calls:
                conn.execute(
                    "insert into code_calls(path, caller, callee, lineno, lang) values (?, ?, ?, ?, ?)",
                    (c.path, c.caller, c.callee, c.lineno, lang),
                )
        else:
            # ast-grep requires a file path. Parse a private temporary copy of
            # the already-redacted trusted descriptor content, never the
            # repository path, so path replacement cannot change the input.
            suffix = abs_path.suffix or ".txt"
            with tempfile.TemporaryDirectory(prefix="code-brain-codegraph-") as temp_dir:
                trusted_path = Path(temp_dir) / f"source{suffix}"
                trusted_path.write_text(redacted_text, encoding="utf-8")
                syms = extract_symbols_func(str(trusted_path))
                for s in syms:
                    conn.execute(
                        "insert into code_symbols(path, qualname, kind, lineno, end_lineno, lang) "
                        "values (?, ?, ?, ?, ?, ?)",
                        (rel, s.get("qualname"), s.get("kind"), s.get("lineno"), s.get("end_lineno"), lang),
                    )
                calls = extract_calls_func(str(trusted_path))
                for c in calls:
                    conn.execute(
                        "insert into code_calls(path, caller, callee, lineno, lang) values (?, ?, ?, ?, ?)",
                        (rel, "<module>", c.get("callee"), c.get("lineno"), lang),
                    )
    except Exception:
        # Indexer must continue even if one file misbehaves.
        return


def _insert_chunk_embedding(conn: sqlite3.Connection, chunk_id: int, text: str, root: Path) -> None:
    """Insert embeddings_vec0 row. When AI_SEARCH_DENSE enabled + model present,
    compute the dense vector inline and store as BLOB. Otherwise insert a
    placeholder row (vector=NULL) to preserve foreign-key-style 1:1 with chunks."""
    from . import embedding as _emb
    from datetime import datetime, timezone

    if _emb.is_active_for(root):
        vec = _emb.embed(text[:4096], root)  # cap input to limit memory/CPU
        if vec is not None:
            import struct
            payload = struct.pack(f"<{len(vec)}f", *vec)
            conn.execute(
                "insert into embeddings_vec0(chunk_id, disabled_reason, vector, model_name, vector_dim, created_at) "
                "values (?, ?, ?, ?, ?, ?)",
                (chunk_id, "active", payload, _emb.MODEL_NAME, len(vec),
                 datetime.now(timezone.utc).isoformat()),
            )
            return
    # default / fallback path — placeholder row, no vector
    conn.execute("insert into embeddings_vec0(chunk_id) values (?)", (chunk_id,))


def _bm25_weights() -> tuple[float, float]:
    """Read FTS5 BM25 column weights from env, with safe defaults.

    Environment variables:
    - AI_SEARCH_BM25_PATH_WEIGHT: weight for path column (default 2.0)
    - AI_SEARCH_BM25_CONTENT_WEIGHT: weight for content column (default 1.0)
    """
    def _read(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return default
        if val != val:  # NaN
            return default
        return val

    return (
        _read("AI_SEARCH_BM25_PATH_WEIGHT", 2.0),
        _read("AI_SEARCH_BM25_CONTENT_WEIGHT", 1.0),
    )


def _function_chunks_for_python(path: str, source_text: str) -> list[dict[str, Any]]:
    """Extract function/class level text chunks from Python source.

    Returns list of dicts with keys:
      - symbol_name: short name (e.g., "my_func")
      - qualname: full qualified name (e.g., "module.MyClass.method")
      - start_line: 1-indexed start line
      - end_line: 1-indexed end line (inclusive)
      - text: source text of the chunk
      - kind: 'function', 'async_function', 'method', 'async_method', or 'class'

    Returns [] if Python parsing fails (best-effort indexer behavior).
    """
    try:
        from .codegraph import extract_symbols
    except Exception:
        return []

    try:
        symbols = extract_symbols(source_text, path=path)
    except Exception:
        return []

    if not symbols:
        return []

    lines = source_text.split("\n")
    chunks = []

    for sym in symbols:
        start_line = sym.lineno
        end_line = sym.end_lineno
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            continue

        # Extract lines (0-indexed slicing for 1-indexed line numbers)
        chunk_lines = lines[start_line - 1 : end_line]
        chunk_text = "\n".join(chunk_lines)

        chunks.append({
            "symbol_name": sym.qualname.split(".")[-1],  # short name
            "qualname": sym.qualname,
            "start_line": start_line,
            "end_line": end_line,
            "text": chunk_text,
            "kind": sym.kind,
        })

    return chunks


def _adapt_ast_chunks(path: str, ast_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt cAST chunker output to the function-chunk shape used by the indexer.

    The ast_chunker yields ``{text, start_line, end_line}``; the indexer's
    insert path expects ``{qualname, text, start_line, end_line, kind}``. A
    synthetic, deterministic qualname (``file::cast:<start>-<end>``) keeps the
    function-chunk path unique per file without colliding with symbol-derived
    chunks. Opt-in only; never reached when AI_AST_CHUNK is unset.
    """
    adapted: list[dict[str, Any]] = []
    for ch in ast_chunks:
        start_line = ch.get("start_line")
        end_line = ch.get("end_line")
        text = ch.get("text")
        if not isinstance(start_line, int) or not isinstance(end_line, int) or not isinstance(text, str):
            continue
        qualname = f"cast:{start_line}-{end_line}"
        adapted.append({
            "symbol_name": qualname,
            "qualname": qualname,
            "start_line": start_line,
            "end_line": end_line,
            "text": text,
            "kind": "cast_chunk",
        })
    return adapted


def _function_chunks_for_lang(path: str, source_text: str, lang: str) -> list[dict[str, Any]]:
    """Extract function/class level text chunks from source in a given language.

    Supports: "py", "js"/"jsx", "ts"/"tsx", "go", "rs".

    Returns list of dicts with keys:
      - symbol_name: short name
      - qualname: full qualified name
      - start_line: 1-indexed start line
      - end_line: 1-indexed end line (inclusive)
      - text: source text of the chunk
      - kind: 'function', 'class', or language-specific variant

    Returns [] if parsing fails or ast-grep is unavailable (best-effort indexer behavior).
    """
    if lang == "py":
        return _function_chunks_for_python(path, source_text)

    # Multi-language extraction via ast-grep integration
    if lang not in {"js", "jsx", "ts", "tsx", "go", "rs"}:
        return []

    try:
        from . import astgrep_integration as ast_mod
    except Exception:
        return []

    # Select the appropriate extract_symbols function
    extract_func = None
    if lang in {"js", "jsx"}:
        extract_func = ast_mod.extract_symbols_js
    elif lang in {"ts", "tsx"}:
        extract_func = ast_mod.extract_symbols_ts
    elif lang == "go":
        extract_func = ast_mod.extract_symbols_go
    elif lang == "rs":
        extract_func = ast_mod.extract_symbols_rs

    if extract_func is None:
        return []

    try:
        suffix = Path(path).suffix or f".{lang}"
        with tempfile.TemporaryDirectory(prefix="code-brain-chunks-") as temp_dir:
            trusted_path = Path(temp_dir) / f"source{suffix}"
            trusted_path.write_text(source_text, encoding="utf-8")
            symbols = extract_func(str(trusted_path))
    except Exception:
        return []

    if not symbols:
        return []

    lines = source_text.split("\n")
    chunks = []

    for sym in symbols:
        start_line = sym.get("lineno")
        end_line = sym.get("end_lineno")
        qualname = sym.get("qualname", "")
        kind = sym.get("kind", "function")

        if not qualname or not isinstance(start_line, int) or not isinstance(end_line, int):
            continue
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            continue

        # Extract lines (0-indexed slicing for 1-indexed line numbers)
        chunk_lines = lines[start_line - 1 : end_line]
        chunk_text = "\n".join(chunk_lines)

        chunks.append({
            "symbol_name": qualname.split(".")[-1] if "." in qualname else qualname,
            "qualname": qualname,
            "start_line": start_line,
            "end_line": end_line,
            "text": chunk_text,
            "kind": kind,
        })

    return chunks


def _compute_rrf_k(indexed_chunk_count: int) -> int:
    """Compute RRF k dynamically based on corpus size.

    For corpus size N (indexed chunks), compute:
      k = clamp(round(60 * log2(max(N, 16)) / log2(1024)), 30, 120)

    This scales k with corpus growth: small corpus (16 chunks) → k≈30,
    1024 chunks → k≈60, larger corpus scales toward k≤120.

    Environment variable AI_SEARCH_RRF_K (if set) overrides dynamic k.
    """
    import math

    # Check for env override first
    raw = os.environ.get("AI_SEARCH_RRF_K")
    if raw is not None and str(raw).strip() != "":
        try:
            val = int(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass

    # Dynamic k: scale with log of corpus size
    N = max(indexed_chunk_count, 16)
    # Reference: at N=1024 (baseline), k=60
    # scaling factor: log2(N) / log2(1024) to normalize growth
    base_k = 60.0
    log_scale = math.log2(N) / math.log2(1024)
    computed_k = base_k * log_scale
    k = max(30, min(120, round(computed_k)))
    return k


def _looks_like_code_symbol(q: str) -> bool:
    """Heuristic: does the query look like a code symbol / path / ticket?"""
    if not isinstance(q, str) or not q.strip():
        return False
    if re.search(r"[a-z][A-Z]", q):
        return True
    if "_" in q and " " not in q.strip():
        return True
    if "/" in q or q.endswith((".py", ".ts", ".tsx", ".js", ".rs", ".go", ".md")):
        return True
    if re.match(r"^[A-Z]+-?\d+", q):
        return True
    return False


def retrieval_policy_for_query(query_text: str, index_state: dict[str, Any]) -> str:
    """Choose a lightweight retrieval policy label without performing retrieval."""
    q = (query_text or "").strip()
    indexed_files = int(index_state.get("indexed_files") or 0)
    if not q or indexed_files <= 0:
        return "none"

    symbol_count = int(index_state.get("symbol_count") or 0)
    call_edge_count = int(index_state.get("call_edge_count") or 0)
    graph_available = symbol_count > 0 or call_edge_count > 0
    if not graph_available:
        return "bm25"

    q_lower = q.casefold()
    graph_intent = bool(
        re.search(r"\b(callers?|callees?|call-?graph|symbol|definition|references?)\b", q_lower)
    )
    if graph_intent:
        return "graph"
    if _looks_like_code_symbol(q):
        return "hybrid"
    return "bm25"


def _index_state_from_conn(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute("select count(*) as count from chunks").fetchone()
    sym = conn.execute("select count(*) as count from code_symbols").fetchone()
    calls = conn.execute("select count(*) as count from code_calls").fetchone()
    return {
        "indexed_chunks": int(row["count"]),  # used for dynamic RRF_K computation
        "indexed_files": int(row["count"]),
        "symbol_count": int(sym["count"]),
        "call_edge_count": int(calls["count"]),
    }


def _rg_fallback_enabled() -> bool:
    raw = os.environ.get("AI_SEARCH_RG_FALLBACK", "1")
    return str(raw).strip().lower() not in {"0", "off", "false", "no"}


def _query_rejection_reason(text: str) -> str | None:
    if not text:
        return "empty_query"
    if "\x00" in text:
        return "invalid_query_control_character"
    if len(text) > SEARCH_QUERY_MAX_CHARS:
        return "query_too_long"
    if len(re.findall(r"\w+", text, flags=re.UNICODE)) > SEARCH_QUERY_MAX_TERMS:
        return "query_too_many_terms"
    return None


def normalize_result_limit(
    value: Any,
    *,
    default: int = SEARCH_RESULT_DEFAULT,
    allow_zero: bool = False,
) -> int:
    """Coerce caller-provided result limits to a small deterministic range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default)
    if allow_zero and parsed <= 0:
        return 0
    return max(1, min(SEARCH_RESULT_MAX, parsed))


def _safe_query_echo(text: str) -> str:
    redacted = str(redact_value(text)).replace("\x00", "")
    if len(redacted) <= SEARCH_QUERY_ECHO_MAX_CHARS:
        return redacted
    return redacted[: SEARCH_QUERY_ECHO_MAX_CHARS - 1] + "…"


def _rejected_query_payload(text: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "query": _safe_query_echo(text),
        "retrieval_policy": "none",
        "recommended_retrieval_policy": "none",
        "results": [],
        "rg_fallback": False,
        "dense_rerank": False,
        "auto_refresh": {"checked": False, "rebuilt": False, "reason": "query_rejected"},
    }


def _redacted_rg_preview(root: Path, rel_path: str, lineno: int, preview: str) -> str:
    """Redact one rg preview after multiline secret blocks were skipped in rg."""
    raw_preview = preview.strip()
    direct = str(redact_value(raw_preview))
    if direct != raw_preview:
        return direct
    if "PRIVATE KEY-----" in raw_preview and (
        "-----BEGIN " in raw_preview or "-----END " in raw_preview
    ):
        return "[REDACTED]"
    return raw_preview


def _pcre2_literal(value: str) -> str:
    """Quote arbitrary text for PCRE2, including embedded ``\\E`` sequences."""
    return r"\Q" + value.replace(r"\E", r"\E\\E\Q") + r"\E"


def _rg_safe_pattern(query_text: str) -> str:
    private_key_block = (
        r"(?s:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)(*SKIP)(*F)|"
    )
    return private_key_block + _pcre2_literal(query_text)


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort terminate one isolated subprocess group and reap its leader."""
    if proc.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except OSError:
                pass
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=0.25)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _run_process_lines_bounded(
    command: list[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    max_events: int,
    delimiter: bytes = b"\n",
    cwd: Path | None = None,
    allowed_returncodes: set[int] | None = None,
    require_complete: bool = False,
) -> list[str]:
    """Run a command with bounded stdout records and fail-soft cleanup."""
    separator = bytes(delimiter)
    if not separator:
        return []
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if creation_flag:
            popen_kwargs["creationflags"] = creation_flag
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            bufsize=0,
            cwd=cwd,
            **popen_kwargs,
        )
    except (OSError, ValueError):
        return []
    if proc.stdout is None:
        _terminate_process_group(proc)
        return []

    lines: list[bytes] = []
    state = {"overflow": False, "failed": False}
    byte_limit = max(1, int(max_output_bytes))
    event_limit = max(1, int(max_events))

    def read_stdout() -> None:
        pending = bytearray()
        total = 0
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), min(65536, byte_limit + 1))
                if not chunk:
                    break
                remaining = byte_limit - total
                if remaining <= 0:
                    state["overflow"] = True
                    break
                if len(chunk) > remaining:
                    pending.extend(chunk[:remaining])
                    total = byte_limit
                    state["overflow"] = True
                else:
                    pending.extend(chunk)
                    total += len(chunk)
                while True:
                    boundary = pending.find(separator)
                    if boundary < 0:
                        break
                    if len(lines) >= event_limit:
                        state["overflow"] = True
                        break
                    lines.append(bytes(pending[:boundary]).rstrip(b"\r"))
                    del pending[: boundary + len(separator)]
                if state["overflow"]:
                    break
            if pending and len(lines) < event_limit and not state["overflow"]:
                lines.append(bytes(pending).rstrip(b"\r"))
            elif pending and len(lines) >= event_limit:
                state["overflow"] = True
        except OSError:
            state["failed"] = True

    reader = threading.Thread(target=read_stdout, name="code-brain-process-reader", daemon=True)
    reader.start()
    reader.join(timeout=max(0.01, float(timeout_seconds)))
    timed_out = reader.is_alive()
    if timed_out or state["overflow"]:
        _terminate_process_group(proc)
        reader.join(timeout=1.0)
    else:
        try:
            proc.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError):
            _terminate_process_group(proc)
    try:
        proc.stdout.close()
    except OSError:
        pass
    if timed_out or state["failed"] or reader.is_alive():
        return []
    if require_complete and state["overflow"]:
        return []
    if allowed_returncodes is not None and proc.returncode not in allowed_returncodes:
        return []
    return [line.decode("utf-8", errors="replace") for line in lines]


def _rg_result_path(root: Path, raw_path: str) -> tuple[Path, str] | None:
    """Normalize one rg path lexically without following repository links."""
    root_abs = Path(os.path.abspath(root))
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root_abs / candidate
    candidate_abs = Path(os.path.abspath(candidate))
    try:
        rel = candidate_abs.relative_to(root_abs)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return candidate_abs, rel.as_posix()


def _rg_fallback(root: Path, query_text: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Run ripgrep as an exact-match fallback. Returns [] if rg missing or fails."""
    limit = normalize_result_limit(limit, default=10, allow_zero=True)
    if limit == 0:
        return []
    if not _rg_fallback_enabled():
        return []
    rg_bin = shutil.which("rg")
    if not rg_bin:
        return []
    q = (query_text or "").strip()
    if _query_rejection_reason(q) is not None:
        return []
    raw_events = _run_process_lines_bounded(
        [
            rg_bin,
            "--json",
            "--pcre2",
            "--multiline",
            "--smart-case",
            "--max-count",
            "3",
            "--max-columns",
            "200",
            "--max-filesize",
            "10M",
            "--",
            _rg_safe_pattern(q),
            str(root),
        ],
        timeout_seconds=RG_TIMEOUT_SECONDS,
        max_output_bytes=RG_OUTPUT_MAX_BYTES,
        max_events=max(64, min(RG_OUTPUT_MAX_EVENTS, max(1, int(limit)) * 12 + 8)),
    )
    if not raw_events:
        return []
    results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    path_allowed: dict[str, bool] = {}
    policy_root = Path(os.path.abspath(root))
    for idx, raw_event in enumerate(raw_events):
        if not raw_event:
            continue
        try:
            event = json.loads(raw_event)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "match":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        path_data = data.get("path")
        lines_data = data.get("lines")
        raw_path = path_data.get("text") if isinstance(path_data, dict) else None
        preview = lines_data.get("text") if isinstance(lines_data, dict) else None
        lineno_value = data.get("line_number")
        if not isinstance(raw_path, str) or not isinstance(preview, str):
            continue
        try:
            lineno = int(lineno_value)
        except (TypeError, ValueError):
            continue
        normalized = _rg_result_path(policy_root, raw_path)
        if normalized is None:
            continue
        candidate_path, rel_path = normalized
        allowed = path_allowed.get(rel_path)
        if allowed is None:
            allowed = _is_indexable_text_file(policy_root, candidate_path)
            path_allowed[rel_path] = allowed
        if not allowed:
            continue
        if rel_path in seen_paths:
            continue
        seen_paths.add(rel_path)
        preview_clean = _redacted_rg_preview(root, rel_path, lineno, preview)
        if len(preview_clean) > SNIPPET_MAX_BYTES:
            preview_clean = preview_clean[:SNIPPET_MAX_BYTES]
        results.append({
            "path": rel_path,
            "snippet": f"L{lineno}: {preview_clean}",
            "line": lineno,
            "content": preview_clean,
            "rank": -0.0001 * (idx + 1),
            "source": "rg",
            "provenance": {
                "processor": "ripgrep-fallback",
                "model_hash": None,
                "prompt_version": None,
                "chunker_version": "rg-1",
                "confidence": 0.5,
            },
        })
        if len(results) >= limit:
            break
    return results


def _record_evidence_candidates(root: Path, *, query_text: str, results: list[dict[str, Any]], source: str) -> None:
    if is_ci():
        return
    try:
        from .evidence import append_candidate_results
        append_candidate_results(root, query=query_text, results=results, source=source)
    except Exception:
        pass


def _result_scope(path: str, summary: str | None) -> str:
    """Compact contextual header so a snippet is self-sufficient (Anthropic Contextual Retrieval).

    Purely derived from existing fields (no extra query, no schema change): the module path and,
    for function chunks whose path encodes ``file::symbol``, the enclosing symbol.
    """
    p = str(path or "")
    if "::" in p:
        file_part, _, symbol = p.partition("::")
        return f"{file_part} › {symbol}"[:160]
    head = (str(summary or "").strip().splitlines() or [""])[0]
    return (f"{p} — {head}"[:160]) if head else p[:160]


def query(root: Path, text: str, *, limit: int = 5, evidence_source: str | None = None) -> dict[str, Any]:
    text = str(text or "").strip()
    limit = normalize_result_limit(limit)
    rejection_reason = _query_rejection_reason(text)
    if rejection_reason is not None:
        return _rejected_query_payload(text, rejection_reason)
    auto_refresh = _auto_refresh_if_stale(root)
    retriever = configured_retriever(root)
    if retriever != "bm25":
        raise RuntimeError(f"search retriever '{retriever}' is not implemented; use retriever: bm25")
    path_weight, content_weight = _bm25_weights()
    # Pull a wider candidate pool when dense rerank is enabled (per Codex report's
    # "lexical 100-500 → dense 20-100 → rerank 5-20" recipe — sized to corpus).
    try:
        from . import embedding as _emb
        dense_active = _emb.is_active_for(root)
    except Exception:
        dense_active = False
    candidate_limit = (
        min(SEARCH_DENSE_CANDIDATE_MAX, max(limit * 8, 40))
        if dense_active
        else limit
    )

    def load_index_rows():
        with _connection_scope(root) as conn:
            init_schema(conn)
            state = _index_state_from_conn(conn)
            loaded_rows = conn.execute(
                """
                select c.id, c.path, c.sha256, c.summary, p.processor,
                       p.model_hash, p.prompt_version, p.chunker_version, p.confidence
                from chunks_fts
                join chunks c on c.id = chunks_fts.rowid
                join provenance p on p.path = c.path
                where chunks_fts match ?
                order by bm25(chunks_fts, ?, ?)
                limit ?
                """,
                (escape_fts_query(text), path_weight, content_weight, candidate_limit),
            ).fetchall()
            loaded_vectors: dict[int, list[float]] = {}
            if dense_active and loaded_rows:
                chunk_ids = [int(row["id"]) for row in loaded_rows]
                placeholders = ",".join("?" * len(chunk_ids))
                vec_rows = conn.execute(
                    f"select chunk_id, vector from embeddings_vec0 "
                    f"where chunk_id in ({placeholders}) and vector is not null",
                    chunk_ids,
                ).fetchall()
                import struct as _struct

                for vector_row in vec_rows:
                    blob = vector_row["vector"]
                    if not blob:
                        continue
                    try:
                        floats = list(_struct.unpack(f"<{len(blob)//4}f", blob))
                        loaded_vectors[int(vector_row["chunk_id"])] = floats
                    except Exception:
                        continue
            return state, loaded_rows, loaded_vectors

    empty_state = {
        "indexed_chunks": 0,
        "indexed_files": 0,
        "symbol_count": 0,
        "call_edge_count": 0,
    }
    try:
        index_state, rows, vectors_by_id = load_index_rows()
    except sqlite3.Error as exc:
        index_state, rows, vectors_by_id = empty_state, [], {}
        if _is_corrupt_index_error(exc):
            recovery = rebuild(root, single_flight=True)
            if recovery.get("ok") and not recovery.get("skipped"):
                try:
                    index_state, rows, vectors_by_id = load_index_rows()
                    auto_refresh = {
                        "enabled": True,
                        "rebuilt": True,
                        "reason": "corrupt_index",
                        "result": recovery,
                    }
                except sqlite3.Error:
                    auto_refresh = {
                        "enabled": True,
                        "rebuilt": False,
                        "reason": "corrupt_index_recovery_failed",
                    }
            else:
                auto_refresh = {
                    "enabled": True,
                    "rebuilt": False,
                    "reason": "corrupt_index_recovery_deferred",
                    "result": recovery,
                }
        else:
            auto_refresh = {
                "enabled": bool(auto_refresh.get("enabled", True)),
                "rebuilt": False,
                "reason": "index_unavailable",
            }
    recommended_policy = retrieval_policy_for_query(text, index_state)
    fts_results: list[dict[str, Any]] = []
    for row in rows:
        fts_results.append({
            "id": int(row["id"]),
            "path": row["path"],
            "scope": _result_scope(row["path"], row["summary"]),
            "snippet": snippet_from_file(root, row["path"], text, fallback=row["summary"], expected_sha=row["sha256"]),
            "provenance": {
                "processor": row["processor"],
                "model_hash": row["model_hash"],
                "prompt_version": row["prompt_version"],
                "chunker_version": row["chunker_version"],
                "confidence": row["confidence"],
            },
        })

    dense_used = False
    if dense_active and vectors_by_id:
        try:
            qvec = _emb.embed(text, root)
        except Exception:
            qvec = None
        if qvec is not None:
            # Cosine sim — both vectors are L2-normalized in embed_batch.
            def _cos(a: list[float], b: list[float]) -> float:
                if len(a) != len(b):
                    return 0.0
                return sum(x * y for x, y in zip(a, b))
            # RRF: combine BM25 rank (already sorted) with dense rank.
            bm25_rank = {r["id"]: idx for idx, r in enumerate(fts_results)}
            dense_scores = []
            for r in fts_results:
                vec = vectors_by_id.get(r["id"])
                if vec is None:
                    dense_scores.append((r["id"], -1.0))
                else:
                    dense_scores.append((r["id"], _cos(qvec, vec)))
            dense_scores.sort(key=lambda x: -x[1])
            dense_rank = {cid: idx for idx, (cid, _s) in enumerate(dense_scores)}
            # RRF k: dynamic or env override (AI_SEARCH_RRF_K)
            rrf_k = _compute_rrf_k(index_state.get("indexed_chunks", 16))
            combined = []
            for r in fts_results:
                cid = r["id"]
                bm_r = bm25_rank.get(cid, candidate_limit)
                dn_r = dense_rank.get(cid, candidate_limit)
                fused = 1.0 / (rrf_k + bm_r + 1) + 1.0 / (rrf_k + dn_r + 1)
                row_copy = dict(r)
                row_copy["_rrf"] = fused
                combined.append(row_copy)
            combined.sort(key=lambda x: -x["_rrf"])
            fts_results = combined
            dense_used = True
    # Apply cross-encoder reranker if active (post-RRF, pre-limit).
    try:
        from . import reranker as _rr
        if _rr.is_active_for(root):
            reranked = _rr.rerank(text, fts_results, root, top_k=limit)
            if reranked is not None:
                fts_results = reranked
    except Exception:
        pass
    # Strip internal fields before returning.
    for r in fts_results:
        r.pop("id", None)
        r.pop("_rrf", None)
    fallback_used = False
    if _rg_fallback_enabled() and (not fts_results or _looks_like_code_symbol(text)):
        rg_hits = _rg_fallback(root, text, limit=limit)
        if rg_hits:
            existing_paths = {item["path"] for item in fts_results}
            for hit in rg_hits:
                if hit["path"] in existing_paths:
                    continue
                fts_results.append({
                    "path": hit["path"],
                    "snippet": hit["snippet"],
                    "provenance": hit["provenance"],
                })
                existing_paths.add(hit["path"])
                if len(fts_results) >= limit:
                    break
            fallback_used = True
    actual_policy = "bm25"
    if dense_used:
        actual_policy += "+dense"
    if fallback_used:
        actual_policy += "+rg"
    payload = {
        "ok": True,
        "query": _safe_query_echo(text),
        "retrieval_policy": actual_policy,
        "recommended_retrieval_policy": recommended_policy,
        "results": fts_results[:limit],
        "rg_fallback": fallback_used,
        "dense_rerank": dense_used,
        "auto_refresh": auto_refresh,
    }
    if evidence_source:
        _record_evidence_candidates(root, query_text=text, results=payload["results"], source=evidence_source)
    return payload


def context_pack(root: Path, text: str, *, limit: int = 5, mode: str = "balanced") -> dict[str, Any]:
    from .context_budget import apply as apply_context_budget

    limit = normalize_result_limit(limit)
    payload = query(root, text, limit=limit, evidence_source="context_pack")
    payload.update(apply_context_budget(payload["results"], mode=mode, limit=limit))
    return payload


def _auto_refresh_enabled() -> bool:
    raw = os.environ.get("AI_SEARCH_AUTO_REFRESH", "1")
    return str(raw).strip().lower() not in {"0", "off", "false", "no"}


def _auto_refresh_if_stale(root: Path) -> dict[str, Any]:
    if not _auto_refresh_enabled():
        return {"enabled": False, "rebuilt": False, "reason": "disabled"}
    if is_ci():
        return {"enabled": False, "rebuilt": False, "reason": "ci_read_only"}
    dirty_paths = _git_dirty_paths(root)
    if dirty_paths:
        dirty_status = index_hash_status(root, paths=dirty_paths)
        changed_dirty = set(dirty_status.get("changed_paths") or [])
        if changed_dirty:
            result = rebuild(root, single_flight=True, incremental=True, paths=changed_dirty)
            return {
                "enabled": True,
                "rebuilt": True,
                "reason": "dirty_hash_mismatch",
                "path_count": len(changed_dirty),
                "result": result,
            }
    db = db_path(root)
    if not db.exists():
        result = rebuild(root, single_flight=True, incremental=True)
        return {"enabled": True, "rebuilt": True, "reason": "missing", "result": result}
    try:
        source_mtime = max((state.st_mtime for _path, state in iter_text_file_states(root)), default=0.0)
        db_mtime = db.stat().st_mtime
    except OSError as exc:
        return {"enabled": True, "rebuilt": False, "reason": f"stat_error:{exc}"}
    if source_mtime >= db_mtime or 0 <= db_mtime - source_mtime <= MTIME_STALE_GRACE_SECONDS:
        hash_status = index_hash_status(root, use_metadata=True, refresh_metadata=True, use_candidate_cache=True)
        changed_paths = set(hash_status.get("changed_paths") or [])
        if changed_paths:
            result = rebuild(root, single_flight=True, incremental=True, paths=changed_paths)
            return {
                "enabled": True,
                "rebuilt": True,
                "reason": "hash_mismatch",
                "path_count": len(changed_paths),
                "result": result,
            }
        if not hash_status.get("ok") and hash_status.get("reason") not in {"current"}:
            result = rebuild(root, single_flight=True, incremental=True)
            return {
                "enabled": True,
                "rebuilt": True,
                "reason": str(hash_status.get("reason") or "hash_check_failed"),
                "result": result,
            }
    return {"enabled": True, "rebuilt": False, "reason": "current"}


def index_hash_status(
    root: Path,
    *,
    paths: set[str] | None = None,
    use_metadata: bool = False,
    refresh_metadata: bool = False,
    use_candidate_cache: bool = False,
) -> dict[str, Any]:
    db = db_path(root)
    if not db.exists():
        return {
            "ok": False,
            "reason": "missing",
            "changed_paths": [],
            "indexed_files": 0,
        }
    try:
        with _connection_scope(root) as conn:
            init_schema(conn)
            indexed = {
                str(row["path"]): (
                    str(row["sha256"]),
                    (
                        int(row["size"]),
                        int(row["mtime_ns"]),
                        int(row["ctime_ns"]),
                    )
                    if row["size"] is not None
                    else None,
                )
                for row in conn.execute(
                    """
                    select c.path, c.sha256, s.size, s.mtime_ns, s.ctime_ns
                    from chunks c
                    join chunk_meta m on m.chunk_id = c.id
                    left join file_state s on s.path = c.path
                    where m.kind = 'file'
                    """
                ).fetchall()
            }
    except RuntimeError as exc:
        detail = str(exc)
        reason = (
            "legacy_schema"
            if "legacy" in detail and "index schema" in detail
            else "unreadable"
        )
        return {
            "ok": False,
            "reason": reason,
            "detail": detail,
            "changed_paths": [],
            "indexed_files": 0,
        }
    except (OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "reason": "unreadable",
            "detail": f"index unreadable: {exc}",
            "changed_paths": [],
            "indexed_files": 0,
        }
    if not indexed:
        return {
            "ok": False,
            "reason": "empty",
            "changed_paths": [],
            "indexed_files": 0,
        }

    changed: set[str] = set()
    seen: set[str] = set()
    metadata_updates: list[tuple[str, Path, str, os.stat_result]] = []
    if paths is None:
        candidates = [
            (path.relative_to(root).as_posix(), path, state)
            for path, state in iter_text_file_states(
                root,
                use_cache=use_candidate_cache,
                update_cache=True,
            )
        ]
    else:
        candidates = []
        for rel in sorted(paths):
            path = root / rel
            state = _indexable_text_stat(root, path)
            if state is not None:
                candidates.append((rel, path, state))
            elif rel in indexed:
                changed.add(rel)

    for rel, path, stat_result in candidates:
        seen.add(rel)
        indexed_entry = indexed.get(rel)
        if indexed_entry is None:
            changed.add(rel)
            continue
        expected, indexed_metadata = indexed_entry
        if use_metadata and indexed_metadata is not None:
            try:
                current_metadata = _file_stat_values(path, stat_result)
            except OSError:
                changed.add(rel)
                continue
            if current_metadata == indexed_metadata:
                continue
        loaded = _read_indexable_text(root, path)
        if loaded is None:
            changed.add(rel)
            continue
        content, source_state = loaded
        redacted = str(redact_value(content))
        digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
        if digest != expected:
            changed.add(rel)
        elif use_metadata and refresh_metadata:
            metadata_updates.append((rel, path, digest, source_state))
    if paths is None:
        changed.update(set(indexed) - seen)
    if metadata_updates:
        try:
            with _connection_scope(root) as conn:
                init_schema(conn)
                for rel, path, digest, source_state in metadata_updates:
                    _upsert_file_state(conn, rel, path, digest, state=source_state)
                conn.commit()
        except (OSError, sqlite3.Error, RuntimeError):
            pass
    ordered = sorted(changed)
    return {
        "ok": not ordered,
        "reason": "current" if not ordered else "hash_mismatch",
        "changed_paths": ordered,
        "indexed_files": len(indexed),
    }


def _changed_index_paths_by_hash(root: Path) -> set[str]:
    """Backward-compatible internal wrapper for callers/tests using the old helper."""
    return set(index_hash_status(root).get("changed_paths") or [])


def _git_dirty_paths(root: Path) -> set[str]:
    try:
        result = subprocess.run(
            # Tracked drift is cheap to enumerate. New untracked source files
            # are detected by the mtime-triggered full hash status below.
            # Including every untracked managed Code Brain file here forced
            # ~300 hash candidates on every query in a freshly installed repo.
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return set()
    paths: set[str] = set()
    parts = [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0") if item]
    idx = 0
    while idx < len(parts):
        item = parts[idx]
        status = item[:2]
        rel = item[3:] if len(item) > 3 else ""
        if rel:
            paths.add(rel)
        if status.startswith(("R", "C")) or len(status) > 1 and status[1] in {"R", "C"}:
            idx += 1
            if idx < len(parts):
                old_rel = parts[idx]
                if old_rel:
                    paths.add(old_rel)
        idx += 1
    return paths


def observability(root: Path, *, query_text: str | None = None, limit: int = 5) -> dict[str, Any]:
    limit = normalize_result_limit(limit)
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
    with _connection_scope(root) as conn:
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
            "auto_refresh": pack.get("auto_refresh"),
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
    with _connection_scope(root) as conn:
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


def iter_text_file_states(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
):
    for path in candidate_files(root, use_cache=use_cache, update_cache=update_cache):
        stat_result = _indexable_text_stat(root, path)
        if stat_result is not None:
            yield path, stat_result


def iter_text_files(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
):
    for path, _stat_result in iter_text_file_states(
        root,
        use_cache=use_cache,
        update_cache=update_cache,
    ):
        yield path


def _is_indexable_text_file(root: Path, path: Path) -> bool:
    return _indexable_text_stat(root, path) is not None


def _indexable_text_stat(root: Path, path: Path) -> os.stat_result | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    rel_posix = rel.as_posix()
    if any(rel_posix.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
        return None
    if any(part in SKIP_DIRS for part in rel.parts):
        return None
    if path.name in SKIP_NAMES:
        return None
    if any(path.name.endswith(suffix) for suffix in SKIP_SUFFIXES):
        return None
    if path.suffix not in TEXT_SUFFIXES and path.name not in {"AGENTS.md", "CLAUDE.md"}:
        return None
    try:
        return validate_root_confined_regular_file(
            path,
            root=root,
            max_bytes=MAX_TEXT_BYTES,
        )
    except OSError:
        return None


def _read_indexable_text(root: Path, path: Path) -> tuple[str, os.stat_result] | None:
    """Read one source file from the same confined descriptor used for trust checks."""
    if _indexable_text_stat(root, path) is None:
        return None
    try:
        return read_root_confined_text(
            path,
            root=root,
            max_bytes=MAX_TEXT_BYTES,
            require_private=False,
        )
    except (OSError, UnicodeDecodeError):
        return None


def _target_text_files(root: Path, rel_paths: set[str]):
    for rel in sorted(rel_paths):
        path = root / rel
        if _is_indexable_text_file(root, path):
            yield rel, path


def _candidate_cache_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "candidate-files.json"


def _candidate_policy_fingerprint() -> str:
    payload = {
        "skip_dirs": sorted(SKIP_DIRS),
        "skip_path_prefixes": list(SKIP_PATH_PREFIXES),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_rel_allowed(rel: str) -> bool:
    rel_path = Path(rel)
    if rel_path.is_absolute() or not rel_path.parts or ".." in rel_path.parts:
        return False
    rel_posix = rel_path.as_posix()
    if any(part in SKIP_DIRS for part in rel_path.parts):
        return False
    if any(rel_posix.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
        return False
    return True


def _path_signature(path: Path) -> list[int] | None:
    try:
        state = path.stat()
    except OSError:
        return None
    return [
        int(state.st_size),
        int(state.st_mtime_ns),
        int(getattr(state, "st_ctime_ns", int(state.st_ctime * 1_000_000_000))),
    ]


def _git_dir_path(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not line.lower().startswith(prefix):
        return None
    value = line[len(prefix):].strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def _candidate_cache_tree_state(root: Path) -> tuple[dict[str, list[int]], dict[str, list[int] | None]]:
    directories: dict[str, list[int]] = {}
    ignore_files: dict[str, list[int] | None] = {}
    for current, dir_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            current_rel = current_path.relative_to(root)
        except ValueError:
            continue
        kept: list[str] = []
        for name in dir_names:
            child_rel = (current_rel / name).as_posix()
            child_prefix = child_rel.rstrip("/") + "/"
            if name == ".git" or name in SKIP_DIRS:
                continue
            if any(child_prefix.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
                continue
            kept.append(name)
        dir_names[:] = kept
        try:
            state = current_path.stat()
            directories[current_rel.as_posix() or "."] = [
                int(state.st_mtime_ns),
                int(getattr(state, "st_ctime_ns", int(state.st_ctime * 1_000_000_000))),
            ]
        except OSError:
            continue
        if ".gitignore" in file_names:
            ignore_path = current_path / ".gitignore"
            ignore_files[ignore_path.relative_to(root).as_posix()] = _path_signature(ignore_path)
    git_dir = _git_dir_path(root)
    dot_git = root / ".git"
    ignore_files[".git"] = _path_signature(dot_git)
    if git_dir is not None:
        for name, path in (
            ("git-index", git_dir / "index"),
            ("git-info-exclude", git_dir / "info" / "exclude"),
        ):
            ignore_files[name] = _path_signature(path)
    return directories, ignore_files


def _candidate_cache_load(root: Path) -> list[Path] | None:
    cache_path = _candidate_cache_path(root)
    try:
        text, _state = read_root_confined_text(
            cache_path,
            root=root,
            max_bytes=10_000_000,
            require_private=True,
        )
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return None
        created = float(payload.get("created_at_unix", 0))
        age = time.time() - created
        if age < 0 or age > CANDIDATE_CACHE_MAX_AGE_SECONDS:
            return None
        if payload.get("schema") != CANDIDATE_CACHE_SCHEMA:
            return None
        if payload.get("policy_fingerprint") != _candidate_policy_fingerprint():
            return None
        cached_dirs = payload.get("directories")
        cached_ignores = payload.get("ignore_files")
        rel_paths = payload.get("paths")
        if not isinstance(cached_dirs, dict) or not isinstance(cached_ignores, dict) or not isinstance(rel_paths, list):
            return None
        for rel, expected_state in cached_dirs.items():
            if (
                not isinstance(rel, str)
                or not isinstance(expected_state, list)
                or len(expected_state) != 2
                or not all(isinstance(item, int) for item in expected_state)
            ):
                return None
            path = root if rel == "." else root / rel
            if rel == ".":
                state = root.lstat()
                if stat_module.S_ISLNK(state.st_mode) or not stat_module.S_ISDIR(state.st_mode):
                    return None
            else:
                state = validate_root_confined_directory(
                    path,
                    root=root,
                    require_safe_permissions=True,
                )
            current_state = [
                int(state.st_mtime_ns),
                int(getattr(state, "st_ctime_ns", int(state.st_ctime * 1_000_000_000))),
            ]
            if current_state != expected_state:
                return None
        _dirs, current_ignores = _candidate_cache_tree_state(root)
        if current_ignores != cached_ignores:
            return None
        paths: list[Path] = []
        for rel in rel_paths:
            if not isinstance(rel, str):
                return None
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                return None
            paths.append(root / rel_path)
        return sorted(paths)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _candidate_cache_write(root: Path, rels: list[str]) -> None:
    cache_path = _candidate_cache_path(root)
    try:
        ensure_root_confined_directory(cache_path.parent, root=root, mode=0o700)
        directories, ignore_files = _candidate_cache_tree_state(root)
        payload = {
            "schema": CANDIDATE_CACHE_SCHEMA,
            "policy_fingerprint": _candidate_policy_fingerprint(),
            "created_at_unix": time.time(),
            "directories": directories,
            "ignore_files": ignore_files,
            "paths": sorted(rels),
        }
        atomic_write_private_text(
            cache_path,
            json.dumps(payload, sort_keys=True),
            root=root,
        )
    except OSError:
        pass


def _filesystem_candidate_files(
    root: Path,
    *,
    max_paths: int | None = None,
    max_visited: int | None = None,
    timeout_seconds: float | None = None,
) -> list[Path]:
    """Bounded no-follow fallback when Git candidate discovery is unavailable."""
    root = Path(root)
    path_limit = max(0, int(GIT_CANDIDATE_MAX_PATHS if max_paths is None else max_paths))
    visit_limit = max(
        0,
        int(FILESYSTEM_CANDIDATE_MAX_VISITED if max_visited is None else max_visited),
    )
    if path_limit == 0 or visit_limit == 0:
        return []
    duration = (
        FILESYSTEM_CANDIDATE_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    deadline = time.monotonic() + max(0.01, float(duration))
    visited = 0
    found: list[Path] = []
    for current, dir_names, file_names in os.walk(root, topdown=True, followlinks=False):
        if time.monotonic() >= deadline or visited >= visit_limit:
            break
        current_path = Path(current)
        try:
            current_rel = current_path.relative_to(root)
        except ValueError:
            dir_names[:] = []
            continue
        kept_dirs: list[str] = []
        for name in dir_names:
            visited += 1
            if visited > visit_limit or time.monotonic() >= deadline:
                break
            child_rel = current_rel / name
            child_posix = child_rel.as_posix().rstrip("/") + "/"
            if name == ".git" or name in SKIP_DIRS:
                continue
            if any(child_posix.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
                continue
            try:
                state = (current_path / name).lstat()
            except OSError:
                continue
            if stat_module.S_ISDIR(state.st_mode) and not stat_module.S_ISLNK(state.st_mode):
                kept_dirs.append(name)
        dir_names[:] = kept_dirs
        if visited > visit_limit or time.monotonic() >= deadline:
            break
        for name in file_names:
            visited += 1
            if visited > visit_limit or time.monotonic() >= deadline:
                return sorted(found)
            path = current_path / name
            try:
                rel = path.relative_to(root).as_posix()
                state = path.lstat()
            except (OSError, ValueError):
                continue
            if not _candidate_rel_allowed(rel) or not stat_module.S_ISREG(state.st_mode):
                continue
            found.append(path)
            if len(found) >= path_limit:
                return sorted(found)
    return sorted(found)


def candidate_files(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
) -> list[Path]:
    if use_cache:
        cached = _candidate_cache_load(root)
        if cached is not None:
            return cached
    rels = _run_process_lines_bounded(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        delimiter=b"\0",
        timeout_seconds=GIT_CANDIDATE_TIMEOUT_SECONDS,
        max_output_bytes=GIT_CANDIDATE_MAX_BYTES,
        max_events=GIT_CANDIDATE_MAX_PATHS + 1,
        allowed_returncodes={0},
        require_complete=True,
    )
    if (
        not rels
        or len(rels) > GIT_CANDIDATE_MAX_PATHS
        or any("\ufffd" in rel for rel in rels)
    ):
        return _filesystem_candidate_files(root)
    rels = [rel for rel in rels if _candidate_rel_allowed(rel)]
    if update_cache and not is_ci():
        _candidate_cache_write(root, rels)
    return sorted(root / rel for rel in rels)


def summarize(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return ""


def snippet_from_file(root: Path, rel_path: str, query_text: str, *, fallback: str, expected_sha: str | None = None) -> str:
    path = root / rel_path
    loaded = _read_indexable_text(root, path)
    if loaded is None:
        return f"[stale index: source unavailable; run ai index rebuild] {fallback}"
    content, _source_state = loaded
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
    return snippet[:SNIPPET_MAX_BYTES]


def escape_fts_query(text: str) -> str:
    # Split on punctuation before quoting. Quoting a whitespace token such as
    # `reciprocal/fusion` turns it into an FTS phrase requiring adjacent terms,
    # while natural-language callers intend two independent search terms.
    # ``\w`` is Unicode-aware in Python, so Korean and other non-Latin words are
    # preserved alongside identifiers and numbers.
    terms = re.findall(r"\w+", str(text or ""), flags=re.UNICODE)
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)
