from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import stat as stat_module
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .index_control import IndexProgress, IndexScanLimit, policy as index_policy, progress_status
from .policy import is_ci
from .private_write import atomic_write_private_text, read_root_confined_text
from .redact import redact_value

SCHEMA_VERSION = 11
CANDIDATE_CACHE_SCHEMA = 3
CANDIDATE_CACHE_MAX_AGE_SECONDS = 60.0
import os as _os


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(_os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# Hard operational bounds.  The index is derived cache state and must never be
# allowed to consume a disk indefinitely.  Operators can raise the cap for very
# large monorepos, while doctor keeps the effective limit visible.
INDEX_MAX_BYTES = _bounded_env_int(
    "AI_INDEX_MAX_BYTES",
    512_000_000,
    minimum=16_000_000,
    maximum=8_000_000_000,
)
INDEX_CACHE_KIB = _bounded_env_int(
    "AI_INDEX_CACHE_KIB",
    16_384,
    minimum=2_048,
    maximum=262_144,
)
INDEX_VACUUM_FREE_RATIO = 0.20
INDEX_VACUUM_MIN_FREE_PAGES = 256
try:
    SNIPPET_MAX_BYTES = max(80, min(2048, int(_os.environ.get("AI_SNIPPET_MAX_BYTES", "240"))))
except (ValueError, TypeError):
    SNIPPET_MAX_BYTES = 240
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


def connect(root: Path) -> sqlite3.Connection:
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=5000")
    conn.execute(f"pragma cache_size={-max(2048, int(INDEX_CACHE_KIB))}")
    conn.execute("pragma temp_store=FILE")
    conn.execute(f"pragma journal_size_limit={max(1_000_000, min(64_000_000, int(INDEX_MAX_BYTES) // 8))}")
    return conn


def _index_storage_stats(root: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    path = db_path(root)
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    db_bytes = path.stat().st_size if path.exists() else 0
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    shm_bytes = shm.stat().st_size if shm.exists() else 0
    page_count = int(conn.execute("pragma page_count").fetchone()[0])
    free_pages = int(conn.execute("pragma freelist_count").fetchone()[0])
    page_size = int(conn.execute("pragma page_size").fetchone()[0])
    total_bytes = int(db_bytes + wal_bytes + shm_bytes)
    return {
        "db_bytes": int(db_bytes),
        "wal_bytes": int(wal_bytes),
        "shm_bytes": int(shm_bytes),
        "total_bytes": total_bytes,
        "max_bytes": int(INDEX_MAX_BYTES),
        "within_limit": total_bytes <= int(INDEX_MAX_BYTES),
        "page_count": page_count,
        "free_pages": free_pages,
        "page_size": page_size,
        "free_ratio": round(free_pages / page_count, 6) if page_count else 0.0,
    }


def index_storage(root: Path) -> dict[str, Any]:
    path = db_path(root)
    if not path.exists():
        return {
            "exists": False,
            "db_bytes": 0,
            "wal_bytes": 0,
            "shm_bytes": 0,
            "total_bytes": 0,
            "max_bytes": int(INDEX_MAX_BYTES),
            "within_limit": True,
            "page_count": 0,
            "free_pages": 0,
            "page_size": 0,
            "free_ratio": 0.0,
        }
    with connect(root) as conn:
        stats = _index_storage_stats(root, conn)
    stats["exists"] = True
    return stats


def index_control_status(root: Path) -> dict[str, Any]:
    effective_policy = index_policy(root)
    progress = progress_status(root, effective_policy=effective_policy)
    storage = index_storage(root)
    path = db_path(root)
    return {
        "ok": bool(
            effective_policy.get("ok")
            and progress.get("ok")
            and storage.get("within_limit")
        ),
        "db_path": path.relative_to(root).as_posix(),
        "exists": path.exists(),
        "policy": effective_policy,
        "progress": progress,
        "storage": storage,
    }


def _maintain_index_storage(root: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    checkpoint_busy = False
    try:
        checkpoint = conn.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()
        checkpoint_busy = bool(checkpoint and int(checkpoint[0]))
    except sqlite3.Error:
        checkpoint_busy = True
    before = _index_storage_stats(root, conn)
    should_vacuum = (
        before["free_pages"] >= max(0, int(INDEX_VACUUM_MIN_FREE_PAGES))
        and before["free_ratio"] >= max(0.0, float(INDEX_VACUUM_FREE_RATIO))
    ) or not before["within_limit"]
    vacuumed = False
    if should_vacuum and not conn.in_transaction:
        conn.execute("vacuum")
        vacuumed = True
        try:
            conn.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error:
            checkpoint_busy = True
    after = _index_storage_stats(root, conn)
    after.update(
        {
            "vacuumed": vacuumed,
            "checkpoint_busy": checkpoint_busy,
            "reclaimed_bytes": max(0, int(before["total_bytes"]) - int(after["total_bytes"])),
        }
    )
    return after


def init_schema(conn: sqlite3.Connection, *, migrate_legacy: bool = False) -> None:
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma busy_timeout=5000")
    current_version = int(conn.execute("pragma user_version").fetchone()[0])
    existing_chunk_columns = [
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("pragma table_info(chunks)").fetchall()
    ]
    existing_chunk_meta_columns = [
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("pragma table_info(chunk_meta)").fetchall()
    ]
    existing_reference_columns = [
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("pragma table_info(code_references)").fetchall()
    ]
    legacy_schema = "content" in existing_chunk_columns or (
        existing_chunk_columns and "summary" not in existing_chunk_columns
    ) or (
        existing_chunk_columns
        and (
            not existing_chunk_meta_columns
            or "source_path" not in existing_chunk_meta_columns
        )
    ) or (
        existing_reference_columns
        and "target_leaf" not in existing_reference_columns
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
        drop table if exists code_references;
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
          source_path text not null,
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
          lang text not null default 'python',
          lexical_callee text,
          target text,
          resolution text not null default 'lexical',
          confidence real not null default 0.45
        );
        create index if not exists code_calls_callee_idx on code_calls(callee);
        create index if not exists code_calls_caller_idx on code_calls(caller);
        create index if not exists code_calls_target_idx on code_calls(target);
        create index if not exists code_calls_lang_idx on code_calls(lang);
        create table if not exists code_references (
          id integer primary key,
          path text not null,
          scope text not null,
          name text not null,
          lexical_name text not null,
          kind text not null,
          lineno integer not null,
          column integer not null,
          end_lineno integer not null,
          end_column integer not null,
          lang text not null default 'python',
          target text,
          target_leaf text,
          resolution text not null default 'lexical',
          confidence real not null default 0.45
        );
        create index if not exists code_references_path_idx on code_references(path);
        create index if not exists code_references_name_idx on code_references(name);
        create index if not exists code_references_lexical_idx on code_references(lexical_name);
        create index if not exists code_references_target_idx on code_references(target);
        create index if not exists code_references_target_leaf_idx on code_references(target_leaf);
        create index if not exists code_references_scope_idx on code_references(scope);
        create index if not exists code_references_kind_idx on code_references(kind);
        """
    )
    conn.execute(f"pragma user_version={SCHEMA_VERSION}")


def rebuild(
    root: Path,
    *,
    single_flight: bool = False,
    incremental: bool = False,
    paths: set[str] | None = None,
    force: bool = False,
    max_seconds: int | None = None,
) -> dict[str, Any]:
    effective_policy = index_policy(root, max_seconds=max_seconds)
    if effective_policy.get("ok") is not True:
        return {
            "ok": False,
            "error": "INDEX_POLICY_INVALID",
            "errors": effective_policy.get("errors", []),
            "committed": False,
            "complete": False,
            "partial": False,
            "policy": effective_policy,
        }
    if effective_policy.get("enabled") is not True and not force:
        return {
            "ok": False,
            "error": "INDEXING_DISABLED",
            "skipped": "indexing disabled by operator policy",
            "committed": False,
            "complete": False,
            "partial": False,
            "policy": effective_policy,
            "db_path": db_path(root).relative_to(root).as_posix(),
        }

    def run() -> dict[str, Any]:
        progress = IndexProgress(
            root=root,
            operation="incremental" if incremental else "full",
            effective_policy=effective_policy,
        )
        progress.begin()
        try:
            result = (
                _rebuild_incremental_inner(root, paths=paths, progress=progress)
                if incremental
                else _rebuild_inner(root, progress=progress)
            )
        except IndexScanLimit as exc:
            failed = progress.fail("INDEX_SCAN_LIMIT", limit=exc)
            return {
                "ok": False,
                "error": "INDEX_SCAN_LIMIT",
                "limit": {"name": exc.limit, "current": exc.current, "maximum": exc.maximum},
                "committed": False,
                "complete": False,
                "partial": False,
                "policy": effective_policy,
                "progress": failed,
                "db_path": db_path(root).relative_to(root).as_posix(),
            }
        except Exception as exc:
            progress.fail(type(exc).__name__)
            raise
        result["policy"] = effective_policy
        result["committed"] = True
        result["complete"] = True
        result["partial"] = False
        result["progress"] = progress.complete(committed=True)
        return result

    if single_flight:
        from .portable import lock_exclusive_nonblocking, unlock
        lock_path = root / ".ai" / "cache" / ".rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w")
        try:
            if not lock_exclusive_nonblocking(lock_fd):
                lock_fd.close()
                return {
                    "ok": True,
                    "skipped": "another rebuild in progress",
                    "db_path": db_path(root).relative_to(root).as_posix(),
                }
            try:
                return run()
            finally:
                unlock(lock_fd)
        finally:
            try:
                lock_fd.close()
            except Exception:
                pass
    return run()


def _rebuild_incremental_inner(
    root: Path,
    *,
    paths: set[str] | None = None,
    progress: IndexProgress,
) -> dict[str, Any]:
    """Re-index only files whose redacted-content sha256 has changed.

    Drops chunks for deleted files; updates chunks for changed files; leaves
    unchanged files untouched. Codegraph + embedding row are rebuilt for the
    changed set too (drop + insert) so they never diverge from the FTS row.

    Schema v9 enables FTS5 contentless_delete and explicit chunk source paths,
    so changed/deleted files can
    remove just their own FTS rows. When ``paths`` is provided, only those
    worktree-relative paths are considered; otherwise the whole text-file set
    is scanned for drift/deletions.

    If the schema is out of date or empty, falls back to full rebuild.
    """
    db_p = db_path(root)
    if not db_p.exists():
        return _rebuild_inner(root, progress=progress)

    with connect(root) as conn:
        try:
            init_schema(conn, migrate_legacy=False)
        except RuntimeError:
            # legacy schema → caller must do a full rebuild
            return _rebuild_inner(root, progress=progress)
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
            return _rebuild_inner(root, progress=progress)

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
                for path in iter_text_files(
                    root,
                    use_cache=False,
                    update_cache=False,
                    progress=progress,
                )
            )
        )
        for rel, path in candidate_paths:
            seen.add(rel)
            if paths is not None:
                progress.candidate(size=len(rel.encode("utf-8")) + 1, path=rel)
                try:
                    source_size = int(path.stat().st_size)
                except OSError:
                    source_size = 0
                progress.scan(size=source_size, path=rel)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            redacted = redact_value(content)
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            existing_pair = existing.get(rel)
            if existing_pair is not None and existing_pair[1] == digest:
                _upsert_file_state(conn, rel, path, digest)
                unchanged += 1
                progress.indexed()
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
                conn.execute("delete from code_references where path = ?", (rel,))
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
                "insert into chunk_meta(chunk_id, kind, source_path, bytes, line_count) "
                "values (?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    "file",
                    rel,
                    len(redacted.encode("utf-8")),
                    redacted.count("\n") + 1,
                ),
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
            _insert_function_chunks(
                conn,
                rel,
                content,
                chunk_id,
                root=root,
                redacted_source_text=redacted,
            )
            _upsert_file_state(conn, rel, path, digest)
            if existing_pair is not None:
                changed += 1
            else:
                added += 1
            progress.indexed()
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
                conn.execute("delete from code_references where path = ?", (rel,))
                conn.execute("delete from file_state where path = ?", (rel,))
                # Also delete function chunks for this file
                chunk_ids = conn.execute(
                    "select id from chunks where path like ?", (f"{rel}:%",)
                ).fetchall()
                for (func_cid,) in chunk_ids:
                    _delete_chunk_rows(conn, func_cid)
                deleted += 1
        conn.commit()
        storage = _maintain_index_storage(root, conn)
    if changed or added or deleted:
        _mark_index_generation(root)
    result = {
        "ok": bool(storage["within_limit"]),
        "db_path": db_p.relative_to(root).as_posix(),
        "incremental": True,
        "unchanged": unchanged,
        "changed": changed,
        "added": added,
        "deleted": deleted,
        "indexed": unchanged + changed + added,
        "targeted": paths is not None,
        "storage": storage,
    }
    if not storage["within_limit"]:
        result["error"] = "INDEX_SIZE_LIMIT"
    return result


def _delete_chunk_rows(conn: sqlite3.Connection, chunk_id: int) -> None:
    """Delete a chunk and its dependent rows, including its FTS row."""
    row = conn.execute("select path from chunks where id = ?", (chunk_id,)).fetchone()
    conn.execute("delete from chunks_fts where rowid = ?", (chunk_id,))
    conn.execute("delete from chunk_meta where chunk_id = ?", (chunk_id,))
    conn.execute("delete from embeddings_vec0 where chunk_id = ?", (chunk_id,))
    if row is not None:
        conn.execute("delete from provenance where path = ?", (row["path"],))
    conn.execute("delete from chunks where id = ?", (chunk_id,))


def _rebuild_inner(root: Path, *, progress: IndexProgress) -> dict[str, Any]:
    with connect(root) as conn:
        init_schema(conn, migrate_legacy=True)
        conn.execute("begin immediate")
        drop_schema(conn)
        create_schema(conn)
        indexed = 0
        for path in iter_text_files(
            root,
            use_cache=False,
            update_cache=False,
            progress=progress,
        ):
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
                "insert into chunk_meta(chunk_id, kind, source_path, bytes, line_count) "
                "values (?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    "file",
                    rel,
                    len(redacted.encode("utf-8")),
                    redacted.count("\n") + 1,
                ),
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
            _insert_function_chunks(
                conn,
                rel,
                content,
                chunk_id,
                root=root,
                redacted_source_text=redacted,
            )
            _upsert_file_state(conn, rel, path, digest)
            indexed += 1
            progress.indexed()
        conn.commit()
        storage = _maintain_index_storage(root, conn)
    _mark_index_generation(root)
    result: dict[str, Any] = {
        "ok": bool(storage["within_limit"]),
        "db_path": db_path(root).relative_to(root).as_posix(),
        "indexed": indexed,
        "storage": storage,
    }
    if not storage["within_limit"]:
        result["error"] = "INDEX_SIZE_LIMIT"
    return result


def _mark_index_generation(root: Path) -> None:
    marker = root / ".ai" / "cache" / "code-index-generation"
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(str(time.time_ns()) + "\n", encoding="utf-8")
    os.replace(temporary, marker)


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
) -> None:
    size, mtime_ns, ctime_ns = _file_stat_values(path)
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
    *,
    root: Path | None = None,
    redacted_source_text: str | None = None,
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

    # Extract boundaries from original syntax, but persist only the matching
    # slice of the already-redacted source. This keeps parsing robust without
    # allowing raw function bodies to enter FTS or chunk hashes.
    redacted_lines = (
        redacted_source_text if redacted_source_text is not None else str(redact_value(source_text))
    ).split("\n")

    for func in func_chunks:
        qualname = func["qualname"]
        start_line = func["start_line"]
        end_line = func["end_line"]
        kind = func["kind"]
        if (
            not isinstance(start_line, int)
            or not isinstance(end_line, int)
            or start_line < 1
            or end_line < start_line
            or end_line > len(redacted_lines)
        ):
            continue
        chunk_text = "\n".join(redacted_lines[start_line - 1 : end_line])

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
                "insert into chunk_meta(chunk_id, kind, source_path, bytes, line_count, "
                "qualname, start_line, end_line) values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    kind,
                    path,
                    len(chunk_text.encode("utf-8")),
                    chunk_text.count("\n") + 1,
                    qualname,
                    start_line,
                    end_line,
                ),
            )
            conn.execute(
                "insert or replace into provenance(path, processor, model_hash, prompt_version, chunker_version, confidence) "
                "values (?, ?, ?, ?, ?, ?)",
                (chunk_path, "code-brain-local", None, "extractive-v1", "1", 1.0),
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
    extract_references_func = None

    if rel.endswith(".py"):
        lang = "python"
        try:
            from .codegraph import extract_calls, extract_references, extract_symbols
            extract_symbols_func = extract_symbols
            extract_calls_func = extract_calls
            extract_references_func = extract_references
        except Exception:
            return
    elif rel.endswith((".js", ".jsx")):
        lang = "javascript"
        try:
            from .astgrep_integration import extract_symbols_js, extract_calls_js
            extract_symbols_func = lambda src, **kw: extract_symbols_js(str(abs_path))
            extract_calls_func = lambda src, **kw: extract_calls_js(str(abs_path))
        except Exception:
            return
    elif rel.endswith((".ts", ".tsx")):
        lang = "typescript"
        try:
            from .astgrep_integration import extract_symbols_ts, extract_calls_ts
            extract_symbols_func = lambda src, **kw: extract_symbols_ts(str(abs_path))
            extract_calls_func = lambda src, **kw: extract_calls_ts(str(abs_path))
        except Exception:
            return
    elif rel.endswith(".go"):
        lang = "go"
        try:
            from .astgrep_integration import extract_symbols_go, extract_calls_go
            extract_symbols_func = lambda src, **kw: extract_symbols_go(str(abs_path))
            extract_calls_func = lambda src, **kw: extract_calls_go(str(abs_path))
        except Exception:
            return
    elif rel.endswith(".rs"):
        lang = "rust"
        try:
            from .astgrep_integration import extract_symbols_rs, extract_calls_rs
            extract_symbols_func = lambda src, **kw: extract_symbols_rs(str(abs_path))
            extract_calls_func = lambda src, **kw: extract_calls_rs(str(abs_path))
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
                    "insert into code_calls(path, caller, callee, lineno, lang, lexical_callee, target, resolution, confidence) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        c.path,
                        c.caller,
                        c.callee,
                        c.lineno,
                        lang,
                        c.lexical_callee,
                        c.target,
                        c.resolution,
                        c.confidence,
                    ),
                )
            references = extract_references_func(redacted_text, path=rel)
            for reference in references:
                conn.execute(
                    "insert into code_references(path, scope, name, lexical_name, kind, lineno, column, "
                    "end_lineno, end_column, lang, target, target_leaf, resolution, confidence) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reference.path,
                        reference.scope,
                        reference.name,
                        reference.lexical_name,
                        reference.kind,
                        reference.lineno,
                        reference.column,
                        reference.end_lineno,
                        reference.end_column,
                        lang,
                        reference.target,
                        str(reference.target or reference.name).rsplit(".", 1)[-1],
                        reference.resolution,
                        reference.confidence,
                    ),
                )
        else:
            # Multi-language extraction (ast-grep) returns dicts
            syms = extract_symbols_func(redacted_text)
            for s in syms:
                conn.execute(
                    "insert into code_symbols(path, qualname, kind, lineno, end_lineno, lang) "
                    "values (?, ?, ?, ?, ?, ?)",
                    (rel, s.get("qualname"), s.get("kind"), s.get("lineno"), s.get("end_lineno"), lang),
                )
            calls = extract_calls_func(redacted_text)
            for c in calls:
                conn.execute(
                    "insert into code_calls(path, caller, callee, lineno, lang, lexical_callee, target, resolution, confidence) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rel,
                        "<module>",
                        c.get("callee"),
                        c.get("lineno"),
                        lang,
                        c.get("callee"),
                        None,
                        "syntax_fallback",
                        0.4,
                    ),
                )
                callee = str(c.get("callee") or "")
                if callee:
                    line = max(1, int(c.get("lineno") or 1))
                    conn.execute(
                        "insert into code_references(path, scope, name, lexical_name, kind, lineno, column, "
                        "end_lineno, end_column, lang, target, target_leaf, resolution, confidence) "
                        "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            rel,
                            "<module>",
                            callee,
                            callee,
                            "call",
                            line,
                            0,
                            line,
                            0,
                            lang,
                            None,
                            callee.rsplit(".", 1)[-1],
                            "syntax_fallback",
                            0.4,
                        ),
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
        symbols = extract_func(path)
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


def _rg_fallback(root: Path, query_text: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Run ripgrep as an exact-match fallback. Returns [] if rg missing or fails."""
    if not _rg_fallback_enabled():
        return []
    rg_bin = shutil.which("rg")
    if not rg_bin:
        return []
    q = (query_text or "").strip()
    if not q:
        return []
    try:
        proc = subprocess.run(
            [
                rg_bin,
                q,
                str(root),
                "--line-number",
                "--with-filename",
                "--smart-case",
                "--max-count",
                "3",
                "--max-columns",
                "200",
                "--no-heading",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if not proc.stdout:
        return []
    results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    allowed_paths = {path.relative_to(root).as_posix() for path in iter_text_files(root)}
    for idx, line in enumerate(proc.stdout.splitlines()):
        if not line:
            continue
        # Format: path:lineno:content
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        raw_path, lineno_str, preview = parts[0], parts[1], parts[2]
        try:
            lineno = int(lineno_str)
        except ValueError:
            continue
        try:
            abs_path = Path(raw_path).resolve()
            rel_path = abs_path.relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel_path = raw_path
        if rel_path not in allowed_paths:
            continue
        if rel_path in seen_paths:
            continue
        seen_paths.add(rel_path)
        preview_clean = preview.strip()
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
    for function chunks whose path encodes ``file:symbol`` (or legacy
    ``file::symbol``), the enclosing symbol.
    """
    p = str(path or "")
    if "::" in p:
        file_part, _, symbol = p.partition("::")
        return f"{file_part} › {symbol}"[:160]
    function_chunk = re.match(
        r"^(.+\.(?:py|js|jsx|ts|tsx|go|rs)):(.+)$",
        p,
        flags=re.IGNORECASE,
    )
    if function_chunk:
        return f"{function_chunk.group(1)} › {function_chunk.group(2)}"[:160]
    head = (str(summary or "").strip().splitlines() or [""])[0]
    return (f"{p} — {head}"[:160]) if head else p[:160]


def _index_result(root: Path, row: Any, query_text: str) -> dict[str, Any]:
    """Convert an index row into the stable public search-result shape."""
    source_path = str(row["source_path"] or row["path"])
    start_line = row["start_line"]
    end_line = row["end_line"]
    return {
        "id": int(row["id"]),
        "path": row["path"],
        "source_path": source_path,
        "scope": _result_scope(row["path"], row["summary"]),
        "snippet": snippet_from_file(
            root,
            source_path,
            query_text,
            fallback=row["summary"],
            expected_sha=row["sha256"],
            start_line=int(start_line) if start_line is not None else None,
            end_line=int(end_line) if end_line is not None else None,
        ),
        "provenance": {
            "processor": row["processor"] or "code-brain-local",
            "model_hash": row["model_hash"],
            "prompt_version": row["prompt_version"] or "extractive-v1",
            "chunker_version": row["chunker_version"] or "1",
            "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
        },
    }


def query(root: Path, text: str, *, limit: int = 5, evidence_source: str | None = None) -> dict[str, Any]:
    from .retrieval_observation import build as build_retrieval_observation
    from .retrieval_observation import start as start_retrieval_observation

    started_ns = start_retrieval_observation()
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
    output_limit = max(0, int(limit))
    requested_limit = max(1, output_limit)
    candidate_limit = min(max(requested_limit * 8, 40), 500) if dense_active else requested_limit
    query_vector: list[float] | None = None
    if dense_active:
        try:
            query_vector = _emb.embed(text, root)
        except Exception:
            query_vector = None
    dense_rows: list[dict[str, Any]] = []
    dense_metadata: dict[str, Any] = {
        "scope": "none",
        "reason": "inactive" if not dense_active else "embedding_unavailable",
        "partial": False,
    }
    with connect(root) as conn:
        init_schema(conn)
        index_state = _index_state_from_conn(conn)
        recommended_policy = retrieval_policy_for_query(text, index_state)
        rows = conn.execute(
            """
            select c.id, c.path, c.sha256, c.summary,
                   m.kind, m.source_path, m.start_line, m.end_line,
                   p.processor,
                   p.model_hash, p.prompt_version, p.chunker_version, p.confidence
            from chunks_fts
            join chunks c on c.id = chunks_fts.rowid
            join chunk_meta m on m.chunk_id = c.id
            left join provenance p on p.path = c.path
            where chunks_fts match ?
            order by bm25(chunks_fts, ?, ?)
            limit ?
            """,
            (escape_fts_query(text), path_weight, content_weight, candidate_limit),
        ).fetchall()
        if query_vector is not None:
            try:
                from .dense_retrieval import collect as collect_dense_candidates

                dense_rows, dense_metadata = collect_dense_candidates(
                    conn,
                    query_vector,
                    model_name=_emb.MODEL_NAME,
                    bm25_candidate_ids=[int(row["id"]) for row in rows],
                    top_k=candidate_limit,
                )
            except Exception as exc:
                dense_rows = []
                dense_metadata = {
                    "scope": "none",
                    "reason": f"dense_error:{type(exc).__name__}",
                    "partial": True,
                }
    fts_results = [_index_result(root, row, text) for row in rows]
    dense_results: list[dict[str, Any]] = []
    for row in dense_rows:
        converted = _index_result(root, row, text)
        converted["_dense_score"] = float(row.get("_dense_score", 0.0))
        dense_results.append(converted)

    dense_used = bool(dense_results)
    if dense_used:
        # Reciprocal Rank Fusion over independent lexical and semantic lists.
        # Candidates absent from one list receive no artificial score from it.
        rrf_k = _compute_rrf_k(index_state.get("indexed_chunks", 16))
        by_id: dict[int, dict[str, Any]] = {}
        scores: dict[int, float] = {}
        for rank, result in enumerate(fts_results, start=1):
            chunk_id = int(result["id"])
            by_id[chunk_id] = dict(result)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
        for rank, result in enumerate(dense_results, start=1):
            chunk_id = int(result["id"])
            if chunk_id not in by_id:
                by_id[chunk_id] = dict(result)
            else:
                by_id[chunk_id]["_dense_score"] = result.get("_dense_score")
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
        fused_results: list[dict[str, Any]] = []
        for chunk_id, result in by_id.items():
            result["_rrf"] = scores[chunk_id]
            fused_results.append(result)
        fused_results.sort(key=lambda result: (-float(result["_rrf"]), str(result["path"])))
        fts_results = fused_results
    # Apply cross-encoder reranker if active (post-RRF, pre-limit).
    try:
        from . import reranker as _rr
        if _rr.is_active_for(root):
            reranked = _rr.rerank(text, fts_results, root, top_k=requested_limit)
            if reranked is not None:
                fts_results = reranked
    except Exception:
        pass
    # Strip internal fields before returning.
    for r in fts_results:
        r.pop("id", None)
        r.pop("_rrf", None)
        r.pop("_dense_score", None)
    fallback_used = False
    if _rg_fallback_enabled() and (not fts_results or _looks_like_code_symbol(text)):
        rg_hits = _rg_fallback(root, text, limit=requested_limit)
        if rg_hits:
            existing_paths = {item["path"] for item in fts_results}
            for hit in rg_hits:
                if hit["path"] in existing_paths:
                    continue
                fts_results.append({
                    "path": hit["path"],
                    "source_path": hit["path"],
                    "snippet": hit["snippet"],
                    "provenance": hit["provenance"],
                })
                existing_paths.add(hit["path"])
                if len(fts_results) >= requested_limit:
                    break
            fallback_used = True
    actual_policy = "bm25"
    if dense_used:
        if dense_metadata.get("scope") == "all_vectors":
            actual_policy += "+dense-global"
        elif dense_metadata.get("scope") == "bm25_candidates":
            actual_policy += "+dense-shortlist"
        else:
            actual_policy += "+dense"
    if fallback_used:
        actual_policy += "+rg"
    payload = {
        "ok": True,
        "query": text,
        "retrieval_policy": actual_policy,
        "recommended_retrieval_policy": recommended_policy,
        "results": fts_results[:output_limit],
        "rg_fallback": fallback_used,
        "dense_rerank": dense_used,
        "dense_retrieval": dense_metadata,
        "auto_refresh": auto_refresh,
    }
    fallbacks: list[str] = []
    if dense_metadata.get("scope") == "bm25_candidates":
        fallbacks.append("dense-shortlist")
    if fallback_used:
        fallbacks.append("ripgrep")
    payload["retrieval_observation"] = build_retrieval_observation(
        operation="code.search",
        query=text,
        started_ns=started_ns,
        returned=len(payload["results"]),
        candidates=len(fts_results),
        partial=bool(dense_metadata.get("partial")) or len(fts_results) > output_limit,
        policy=actual_policy,
        fallback=fallbacks or None,
        sources={
            "bm25": len(rows),
            "dense": len(dense_results),
            "fused_or_fallback": len(fts_results),
        },
        limits={
            "requested_results": output_limit,
            "candidate_limit": candidate_limit,
            "dense": dense_metadata.get("policy") or {},
        },
        quality={
            "dense_scope": dense_metadata.get("scope"),
            "dense_reason": dense_metadata.get("reason"),
            "corrupt_vectors": dense_metadata.get("corrupt_vectors", 0),
            "auto_refresh_changed": bool((auto_refresh or {}).get("changed")) if isinstance(auto_refresh, dict) else False,
            "recommended_policy": recommended_policy,
        },
    )
    if evidence_source:
        _record_evidence_candidates(root, query_text=text, results=payload["results"], source=evidence_source)
    return payload


def context_pack(root: Path, text: str, *, limit: int = 5, mode: str = "balanced") -> dict[str, Any]:
    from .context_budget import apply as apply_context_budget, candidate_limit

    payload = query(root, text, limit=candidate_limit(limit))
    payload.update(apply_context_budget(payload["results"], mode=mode, limit=limit, query=text))
    _record_evidence_candidates(root, query_text=text, results=payload["results"], source="context_pack")
    return payload


def _auto_refresh_enabled() -> bool:
    raw = os.environ.get("AI_SEARCH_AUTO_REFRESH", "1")
    return str(raw).strip().lower() not in {"0", "off", "false", "no"}


def _auto_refresh_if_stale(root: Path) -> dict[str, Any]:
    if not _auto_refresh_enabled():
        return {"enabled": False, "rebuilt": False, "reason": "disabled"}
    if is_ci():
        return {"enabled": False, "rebuilt": False, "reason": "ci_read_only"}
    effective_policy = index_policy(root)
    if effective_policy.get("ok") is not True:
        return {
            "enabled": False,
            "rebuilt": False,
            "reason": "index_policy_invalid",
            "errors": effective_policy.get("errors", []),
        }
    if effective_policy.get("enabled") is not True:
        return {"enabled": False, "rebuilt": False, "reason": "indexing_disabled"}
    if effective_policy.get("auto_rebuild") is not True:
        return {"enabled": False, "rebuilt": False, "reason": "auto_rebuild_disabled"}
    dirty_paths = _git_dirty_paths(root)
    if dirty_paths:
        dirty_status = index_hash_status(root, paths=dirty_paths)
        changed_dirty = set(dirty_status.get("changed_paths") or [])
        if changed_dirty:
            result = rebuild(root, single_flight=True, incremental=True, paths=changed_dirty)
            return {
                "enabled": True,
                "rebuilt": result.get("ok") is True and not result.get("skipped"),
                "reason": "dirty_hash_mismatch",
                "path_count": len(changed_dirty),
                "result": result,
            }
    db = db_path(root)
    if not db.exists():
        result = rebuild(root, single_flight=True, incremental=True)
        return {
            "enabled": True,
            "rebuilt": result.get("ok") is True and not result.get("skipped"),
            "reason": "missing",
            "result": result,
        }
    hash_status = index_hash_status(
        root,
        use_metadata=True,
        refresh_metadata=True,
        use_candidate_cache=True,
    )
    changed_paths = set(hash_status.get("changed_paths") or [])
    if changed_paths:
        result = rebuild(root, single_flight=True, incremental=True, paths=changed_paths)
        return {
            "enabled": True,
            "rebuilt": result.get("ok") is True and not result.get("skipped"),
            "reason": "hash_mismatch",
            "path_count": len(changed_paths),
            "result": result,
        }
    if not hash_status.get("ok") and hash_status.get("reason") not in {"current"}:
        if hash_status.get("reason") == "index_scan_limit":
            return {
                "enabled": True,
                "rebuilt": False,
                "reason": "index_scan_limit",
                "status": hash_status,
            }
        if hash_status.get("reason") == "legacy_schema":
            # Query/context paths are read-oriented and must never perform a
            # destructive schema migration as a side effect. Preserve the
            # legacy database and let init_schema() raise the actionable
            # `ai index rebuild` remediation below. Only the explicit rebuild
            # command is allowed to drop and recreate legacy tables.
            return {
                "enabled": True,
                "rebuilt": False,
                "reason": "legacy_schema",
                "status": hash_status,
            }
        result = rebuild(root, single_flight=True, incremental=True)
        return {
            "enabled": True,
            "rebuilt": result.get("ok") is True and not result.get("skipped"),
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
    effective_policy = index_policy(root)
    if effective_policy.get("ok") is not True:
        return {
            "ok": False,
            "reason": "index_policy_invalid",
            "detail": "; ".join(str(item) for item in effective_policy.get("errors", [])[:5]),
            "changed_paths": [],
            "indexed_files": 0,
            "policy": effective_policy,
        }
    if paths is None and effective_policy.get("enabled") is not True:
        return {
            "ok": True,
            "reason": "indexing_disabled",
            "detail": "freshness scan disabled by operator policy",
            "changed_paths": [],
            "indexed_files": 0,
            "policy": effective_policy,
            "scan": {"bounded": True, "skipped": True},
        }
    db = db_path(root)
    if not db.exists():
        return {
            "ok": False,
            "reason": "missing",
            "changed_paths": [],
            "indexed_files": 0,
        }
    try:
        with connect(root) as conn:
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

    probe = IndexProgress(
        root=root,
        operation="freshness_probe",
        effective_policy=effective_policy,
        persist=False,
    )
    probe.begin()
    changed: set[str] = set()
    seen: set[str] = set()
    metadata_updates: list[tuple[str, Path, str]] = []
    try:
        if paths is None:
            candidates = (
                (path.relative_to(root).as_posix(), path, state)
                for path, state in iter_text_file_states(
                    root,
                    use_cache=use_candidate_cache,
                    update_cache=False,
                    progress=probe,
                )
            )
        else:
            targeted: list[tuple[str, Path, os.stat_result]] = []
            for rel in sorted(paths):
                probe.candidate(size=len(rel.encode("utf-8", errors="replace")) + 1, path=rel)
                path = root / rel
                state = _indexable_text_stat(root, path)
                if state is not None:
                    probe.scan(size=int(state.st_size), path=rel)
                    targeted.append((rel, path, state))
                elif rel in indexed:
                    changed.add(rel)
            candidates = iter(targeted)

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
            try:
                redacted = str(redact_value(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                changed.add(rel)
                continue
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            if digest != expected:
                changed.add(rel)
            elif use_metadata and refresh_metadata:
                metadata_updates.append((rel, path, digest))
    except IndexScanLimit as exc:
        return {
            "ok": False,
            "reason": "index_scan_limit",
            "detail": str(exc),
            "limit": {"name": exc.limit, "current": exc.current, "maximum": exc.maximum},
            "changed_paths": sorted(changed),
            "indexed_files": len(indexed),
            "policy": effective_policy,
            "scan": {
                "bounded": True,
                "candidate_files": probe.candidate_files,
                "candidate_bytes": probe.candidate_bytes,
                "scanned_files": probe.scanned_files,
                "source_bytes": probe.source_bytes,
            },
        }
    if paths is None:
        changed.update(set(indexed) - seen)
    if metadata_updates:
        try:
            with connect(root) as conn:
                init_schema(conn)
                for rel, path, digest in metadata_updates:
                    _upsert_file_state(conn, rel, path, digest)
                conn.commit()
        except (OSError, sqlite3.Error, RuntimeError):
            pass
    ordered = sorted(changed)
    return {
        "ok": not ordered,
        "reason": "current" if not ordered else "hash_mismatch",
        "changed_paths": ordered,
        "indexed_files": len(indexed),
        "policy": effective_policy,
        "scan": {
            "bounded": True,
            "candidate_files": probe.candidate_files,
            "candidate_bytes": probe.candidate_bytes,
            "scanned_files": probe.scanned_files,
            "source_bytes": probe.source_bytes,
        },
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
    path = db_path(root)
    payload: dict[str, Any] = {
        "ok": True,
        "db_path": path.relative_to(root).as_posix(),
        "exists": path.exists(),
        "retriever": configured_retriever(root),
        "schema_version": None,
        "sqlite_bytes": 0,
        "sqlite_wal_bytes": 0,
        "sqlite_shm_bytes": 0,
        "sqlite_total_bytes": 0,
        "sqlite_max_bytes": int(INDEX_MAX_BYTES),
        "sqlite_within_limit": True,
        "sqlite_page_count": 0,
        "sqlite_free_pages": 0,
        "sqlite_free_ratio": 0.0,
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
        storage = _index_storage_stats(root, conn)
        payload["sqlite_bytes"] = storage["db_bytes"]
        payload["sqlite_wal_bytes"] = storage["wal_bytes"]
        payload["sqlite_shm_bytes"] = storage["shm_bytes"]
        payload["sqlite_total_bytes"] = storage["total_bytes"]
        payload["sqlite_max_bytes"] = storage["max_bytes"]
        payload["sqlite_within_limit"] = storage["within_limit"]
        payload["sqlite_page_count"] = storage["page_count"]
        payload["sqlite_free_pages"] = storage["free_pages"]
        payload["sqlite_free_ratio"] = storage["free_ratio"]
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


def iter_text_file_states(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
    progress: IndexProgress | None = None,
):
    for path in candidate_files(
        root,
        use_cache=use_cache,
        update_cache=update_cache,
        progress=progress,
    ):
        stat_result = _indexable_text_stat(root, path)
        if stat_result is not None:
            if progress is not None:
                progress.scan(
                    size=int(stat_result.st_size),
                    path=path.relative_to(root).as_posix(),
                )
            yield path, stat_result


def iter_text_files(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
    progress: IndexProgress | None = None,
):
    for path, _stat_result in iter_text_file_states(
        root,
        use_cache=use_cache,
        update_cache=update_cache,
        progress=progress,
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
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    # Never follow repository symlinks while indexing. A tracked or untracked
    # link can point outside the project and would otherwise copy arbitrary
    # external source or credential material into the local SQLite index.
    if stat_module.S_ISLNK(stat_result.st_mode):
        return None
    if not stat_module.S_ISREG(stat_result.st_mode) or stat_result.st_size > MAX_TEXT_BYTES:
        return None
    if path.suffix not in TEXT_SUFFIXES and path.name not in {"AGENTS.md", "CLAUDE.md"}:
        return None
    return stat_result


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


def _candidate_cache_parent_confined(root: Path, path: Path) -> bool:
    try:
        path.parent.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _trusted_candidate_cache(root: Path) -> Path | None:
    path = _candidate_cache_path(root)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if not _candidate_cache_parent_confined(root, path):
            return None
        state = path.stat()
        if os.name != "nt" and stat_module.S_IMODE(state.st_mode) & 0o077:
            return None
        if hasattr(os, "geteuid") and state.st_uid != os.geteuid():
            return None
    except OSError:
        return None
    return path


def _candidate_rel_allowed(rel: str) -> bool:
    rel_path = Path(rel)
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
    cache_path = _trusted_candidate_cache(root)
    if cache_path is None:
        return None
    try:
        text, _state = read_root_confined_text(
            cache_path,
            root=root,
            max_bytes=10_000_000,
            require_private=True,
        )
        payload = json.loads(text)
        created = float(payload.get("created_at_unix", 0))
        if time.time() - created < 0 or time.time() - created > CANDIDATE_CACHE_MAX_AGE_SECONDS:
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
            state = path.stat()
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
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not _candidate_cache_parent_confined(root, cache_path):
            return
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


def _bounded_git_candidate_rels(root: Path, progress: IndexProgress) -> list[str]:
    process = subprocess.Popen(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
    stop = threading.Event()

    def read_stdout() -> None:
        read_chunk = getattr(process.stdout, "read1", process.stdout.read)
        try:
            while not stop.is_set():
                chunk = read_chunk(64 * 1024)
                if not chunk:
                    break
                while not stop.is_set():
                    try:
                        chunks.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            while not stop.is_set():
                try:
                    chunks.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue

    reader = threading.Thread(target=read_stdout, name="code-brain-index-candidates", daemon=True)
    reader.start()
    pending = b""
    rels: list[str] = []
    try:
        while True:
            remaining = progress.deadline - time.monotonic()
            if remaining <= 0:
                elapsed = time.monotonic() - progress.started_monotonic
                raise IndexScanLimit(
                    "max_seconds",
                    round(elapsed, 3),
                    progress.effective_policy["max_seconds"],
                )
            try:
                chunk = chunks.get(timeout=min(0.25, remaining))
            except queue.Empty:
                progress.heartbeat(force=True)
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if chunk is None:
                break
            pending += chunk
            parts = pending.split(b"\0")
            pending = parts.pop()
            for raw in parts:
                if not raw:
                    continue
                rel = raw.decode("utf-8", errors="replace")
                progress.candidate(size=len(raw) + 1, path=rel)
                if _candidate_rel_allowed(rel):
                    rels.append(rel)
        if pending:
            rel = pending.decode("utf-8", errors="replace")
            progress.candidate(size=len(pending), path=rel)
            if _candidate_rel_allowed(rel):
                rels.append(rel)
        return_code = process.wait(timeout=2)
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, process.args)
        return rels
    except Exception:
        stop.set()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        stop.set()
        reader.join(timeout=1)


def _bounded_fallback_candidates(root: Path, progress: IndexProgress) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        try:
            state = path.stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(state.st_mode):
            continue
        rel = path.relative_to(root).as_posix()
        progress.candidate(size=len(rel.encode("utf-8", errors="replace")) + 1, path=rel)
        if _candidate_rel_allowed(rel):
            paths.append(path)
    return sorted(paths)


def candidate_files(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
    progress: IndexProgress | None = None,
) -> list[Path]:
    if use_cache:
        cached = _candidate_cache_load(root)
        if cached is not None:
            if progress is not None:
                for path in cached:
                    rel = path.relative_to(root).as_posix()
                    progress.candidate(size=len(rel.encode("utf-8", errors="replace")) + 1, path=rel)
            return cached
    if progress is not None:
        try:
            rels = _bounded_git_candidate_rels(root, progress)
        except (OSError, subprocess.CalledProcessError):
            return _bounded_fallback_candidates(root, progress)
        return sorted(root / rel for rel in rels)
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and _candidate_rel_allowed(path.relative_to(root).as_posix())
        )
    rels = [
        rel
        for item in result.stdout.split(b"\0")
        if item and _candidate_rel_allowed(rel := item.decode("utf-8"))
    ]
    if update_cache and not is_ci():
        _candidate_cache_write(root, rels)
    return sorted(root / rel for rel in rels)


def summarize(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return ""


def snippet_from_file(
    root: Path,
    rel_path: str,
    query_text: str,
    *,
    fallback: str,
    expected_sha: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    path = root / rel_path
    try:
        content, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=MAX_TEXT_BYTES,
            require_private=False,
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return f"[stale index: source unavailable; run ai index rebuild] {fallback}"
    redacted = str(redact_value(content))
    indexed_text = redacted
    if start_line is not None or end_line is not None:
        lines = redacted.split("\n")
        if (
            start_line is None
            or end_line is None
            or start_line < 1
            or end_line < start_line
            or end_line > len(lines)
        ):
            return f"[stale index: source changed; run ai index rebuild] {fallback}"
        indexed_text = "\n".join(lines[start_line - 1 : end_line])
    if expected_sha:
        current_sha = hashlib.sha256(indexed_text.encode("utf-8")).hexdigest()
        if current_sha != expected_sha:
            return f"[stale index: source changed; run ai index rebuild] {fallback}"
    terms = [term.casefold() for term in query_text.split() if term.strip()]
    lowered = indexed_text.casefold()
    hit_at = -1
    for term in terms:
        hit_at = lowered.find(term)
        if hit_at >= 0:
            break
    if hit_at < 0:
        return summarize(indexed_text)
    start = max(0, hit_at - 120)
    end = min(len(indexed_text), hit_at + 240)
    snippet = indexed_text[start:end].replace("\n", "\\n")
    if start > 0:
        snippet = "..." + snippet
    if end < len(indexed_text):
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
