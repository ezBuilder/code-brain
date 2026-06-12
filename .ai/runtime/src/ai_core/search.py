from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .config import load_config
from .policy import is_ci
from .redact import redact_value

SCHEMA_VERSION = 8
import os as _os
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
        drop table if exists code_symbols;
        drop table if exists code_calls;
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
                return _rebuild_incremental_inner(root, paths=paths) if incremental else _rebuild_inner(root)
            finally:
                unlock(lock_fd)
        finally:
            try:
                lock_fd.close()
            except Exception:
                pass
    return _rebuild_incremental_inner(root, paths=paths) if incremental else _rebuild_inner(root)


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

    with connect(root) as conn:
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
            else ((path.relative_to(root).as_posix(), path) for path in iter_text_files(root))
        )
        for rel, path in candidate_paths:
            seen.add(rel)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            redacted = redact_value(content)
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            existing_pair = existing.get(rel)
            if existing_pair is not None and existing_pair[1] == digest:
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
            _insert_function_chunks(conn, rel, content, chunk_id)
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
                # Also delete function chunks for this file
                chunk_ids = conn.execute(
                    "select id from chunks where path like ?", (f"{rel}:%",)
                ).fetchall()
                for (func_cid,) in chunk_ids:
                    _delete_chunk_rows(conn, func_cid)
                deleted += 1
        conn.commit()
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
            _insert_chunk_embedding(conn, chunk_id, redacted, root)
            _insert_codegraph_for_path(conn, rel, redacted, path)
            # For supported languages, also insert function/class level chunks (hybrid chunking)
            _insert_function_chunks(conn, rel, content, chunk_id)
            indexed += 1
        conn.commit()
        conn.execute("vacuum")
    return {"ok": True, "db_path": db_path(root).relative_to(root).as_posix(), "indexed": indexed}


def _codegraph_enabled() -> bool:
    raw = os.environ.get("AI_SEARCH_CODEGRAPH", "1")
    return str(raw).strip().lower() not in {"0", "off", "false", "no"}


def _insert_function_chunks(
    conn: sqlite3.Connection, path: str, source_text: str, file_chunk_id: int
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
                    "insert into code_calls(path, caller, callee, lineno, lang) values (?, ?, ?, ?, ?)",
                    (c.path, c.caller, c.callee, c.lineno, lang),
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


def query(root: Path, text: str, *, limit: int = 5, evidence_source: str | None = None) -> dict[str, Any]:
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
    candidate_limit = max(limit * 8, 40) if dense_active else limit
    with connect(root) as conn:
        init_schema(conn)
        index_state = _index_state_from_conn(conn)
        recommended_policy = retrieval_policy_for_query(text, index_state)
        rows = conn.execute(
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
        # If dense active, fetch vectors for the candidates so we can rerank.
        vectors_by_id: dict[int, list[float]] = {}
        if dense_active and rows:
            chunk_ids = [int(r["id"]) for r in rows]
            placeholders = ",".join("?" * len(chunk_ids))
            vec_rows = conn.execute(
                f"select chunk_id, vector from embeddings_vec0 "
                f"where chunk_id in ({placeholders}) and vector is not null",
                chunk_ids,
            ).fetchall()
            import struct as _struct
            for vr in vec_rows:
                blob = vr["vector"]
                if not blob:
                    continue
                try:
                    floats = list(_struct.unpack(f"<{len(blob)//4}f", blob))
                    vectors_by_id[int(vr["chunk_id"])] = floats
                except Exception:
                    continue
    fts_results: list[dict[str, Any]] = []
    for row in rows:
        fts_results.append({
            "id": int(row["id"]),
            "path": row["path"],
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
        "query": text,
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
        result = rebuild(root, single_flight=True, incremental=True, paths=dirty_paths)
        return {
            "enabled": True,
            "rebuilt": True,
            "reason": "dirty_paths",
            "path_count": len(dirty_paths),
            "result": result,
        }
    db = db_path(root)
    if not db.exists():
        result = rebuild(root, single_flight=True, incremental=True)
        return {"enabled": True, "rebuilt": True, "reason": "missing", "result": result}
    try:
        source_mtime = max((path.stat().st_mtime for path in iter_text_files(root)), default=0.0)
        db_mtime = db.stat().st_mtime
    except OSError as exc:
        return {"enabled": True, "rebuilt": False, "reason": f"stat_error:{exc}"}
    if source_mtime >= db_mtime:
        result = rebuild(root, single_flight=True, incremental=True)
        return {"enabled": True, "rebuilt": True, "reason": "mtime_fallback", "result": result}
    if 0 <= db_mtime - source_mtime <= MTIME_STALE_GRACE_SECONDS:
        changed_paths = _changed_index_paths_by_hash(root)
        if changed_paths:
            result = rebuild(root, single_flight=True, incremental=True, paths=changed_paths)
            return {
                "enabled": True,
                "rebuilt": True,
                "reason": "hash_mismatch",
                "path_count": len(changed_paths),
                "result": result,
            }
    return {"enabled": True, "rebuilt": False, "reason": "current"}


def _changed_index_paths_by_hash(root: Path) -> set[str]:
    try:
        with connect(root) as conn:
            init_schema(conn)
            indexed = {
                str(row["path"]): str(row["sha256"])
                for row in conn.execute(
                    """
                    select c.path, c.sha256
                    from chunks c
                    join chunk_meta m on m.chunk_id = c.id
                    where m.kind = 'file'
                    """
                ).fetchall()
            }
    except Exception:
        return set()
    if not indexed:
        return set()

    changed: set[str] = set()
    seen: set[str] = set()
    for path in iter_text_files(root):
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        expected = indexed.get(rel)
        if expected is None:
            changed.add(rel)
            continue
        try:
            redacted = str(redact_value(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            changed.add(rel)
            continue
        digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
        if digest != expected:
            changed.add(rel)
    changed.update(set(indexed) - seen)
    return changed


def _git_dirty_paths(root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
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


def iter_text_files(root: Path):
    for path in candidate_files(root):
        if _is_indexable_text_file(root, path):
            yield path


def _is_indexable_text_file(root: Path, path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    rel_posix = rel.as_posix()
    if any(rel_posix.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
        return False
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if path.name in SKIP_NAMES:
        return False
    if any(path.name.endswith(suffix) for suffix in SKIP_SUFFIXES):
        return False
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return False
    except OSError:
        return False
    return path.suffix in TEXT_SUFFIXES or path.name in {"AGENTS.md", "CLAUDE.md"}


def _target_text_files(root: Path, rel_paths: set[str]):
    for rel in sorted(rel_paths):
        path = root / rel
        if _is_indexable_text_file(root, path):
            yield rel, path


def candidate_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
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
    return snippet[:SNIPPET_MAX_BYTES]


def escape_fts_query(text: str) -> str:
    terms = [term.replace('"', "") for term in text.split() if term.strip()]
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)
