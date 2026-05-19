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
from .redact import redact_value

SCHEMA_VERSION = 5
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
          parent text
        );
        create index if not exists code_symbols_path_idx on code_symbols(path);
        create index if not exists code_symbols_qualname_idx on code_symbols(qualname);
        create table if not exists code_calls (
          id integer primary key,
          path text not null,
          caller text not null,
          callee text not null,
          lineno integer not null
        );
        create index if not exists code_calls_callee_idx on code_calls(callee);
        create index if not exists code_calls_caller_idx on code_calls(caller);
        """
    )
    conn.execute(f"pragma user_version={SCHEMA_VERSION}")


def rebuild(root: Path, *, single_flight: bool = False, incremental: bool = False) -> dict[str, Any]:
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
                return _rebuild_incremental_inner(root) if incremental else _rebuild_inner(root)
            finally:
                unlock(lock_fd)
        finally:
            try:
                lock_fd.close()
            except Exception:
                pass
    return _rebuild_incremental_inner(root) if incremental else _rebuild_inner(root)


def _rebuild_incremental_inner(root: Path) -> dict[str, Any]:
    """Re-index only files whose redacted-content sha256 has changed.

    Drops chunks for deleted files; updates chunks for changed files; leaves
    unchanged files untouched. Codegraph + embedding row are rebuilt for the
    changed set too (drop + insert) so they never diverge from the FTS row.

    Limitation: chunks_fts is a contentless FTS5 virtual table, which does
    not support row-level DELETE. To work around this, we 'delete-all' the
    FTS table once and re-populate it from chunks for both unchanged and
    changed rows. embedding + codegraph data is the expensive part and
    those *are* truly incremental.

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
            for row in conn.execute("select id, path, sha256 from chunks").fetchall()
        }
        if not existing:
            return _rebuild_inner(root)

        conn.execute("begin immediate")
        # Wipe the FTS index once; we'll repopulate it from `chunks` afterwards
        # (contentless FTS5 disallows row DELETE, so this is the only safe path).
        conn.execute("insert into chunks_fts(chunks_fts) values('delete-all')")
        seen: set[str] = set()
        changed = 0
        added = 0
        unchanged = 0
        for path in iter_text_files(root):
            rel = path.relative_to(root).as_posix()
            seen.add(rel)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            redacted = redact_value(content)
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            existing_pair = existing.get(rel)
            if existing_pair is not None and existing_pair[1] == digest:
                # chunks row is up to date; just refill the FTS row so search works
                conn.execute(
                    "insert into chunks_fts(rowid, path, content) values (?, ?, ?)",
                    (existing_pair[0], rel, redacted),
                )
                unchanged += 1
                continue
            # need to (re)write this file: drop dependent tables (NOT chunks_fts;
            # we already wiped it). chunks row identity is replaced so embedding +
            # codegraph re-insert below picks up the new chunk_id.
            if existing_pair is not None:
                _delete_chunk_rows_keep_fts(conn, existing_pair[0])
                # summaries/provenance/codegraph have path-keyed UNIQUE rows; clear them too
                conn.execute("delete from summaries where path = ?", (rel,))
                conn.execute("delete from provenance where path = ?", (rel,))
                conn.execute("delete from code_symbols where path = ?", (rel,))
                conn.execute("delete from code_calls where path = ?", (rel,))
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
            if existing_pair is not None:
                changed += 1
            else:
                added += 1
        # cleanup deleted files (chunks_fts already wiped + repopulated above)
        deleted = 0
        for rel, (cid, _digest) in existing.items():
            if rel not in seen:
                _delete_chunk_rows_keep_fts(conn, cid)
                conn.execute("delete from summaries where path = ?", (rel,))
                conn.execute("delete from provenance where path = ?", (rel,))
                conn.execute("delete from code_symbols where path = ?", (rel,))
                conn.execute("delete from code_calls where path = ?", (rel,))
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
    }


def _delete_chunk_rows_keep_fts(conn: sqlite3.Connection, chunk_id: int) -> None:
    """Delete chunk-dependent rows. Used by the incremental rebuild path,
    where chunks_fts is wiped wholesale at the start of the cycle (FTS5
    contentless DELETE limitation)."""
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
            indexed += 1
        conn.commit()
        conn.execute("vacuum")
    return {"ok": True, "db_path": db_path(root).relative_to(root).as_posix(), "indexed": indexed}


def _codegraph_enabled() -> bool:
    raw = os.environ.get("AI_SEARCH_CODEGRAPH", "1")
    return str(raw).strip().lower() not in {"0", "off", "false", "no"}


def _insert_codegraph_for_path(conn: sqlite3.Connection, rel: str, redacted_text: str, abs_path: Path) -> None:
    """Insert function/class symbols + call edges for Python source files.

    Default ON (AI_SEARCH_CODEGRAPH=1). Skips non-Python and any file the AST
    parser rejects (best-effort indexer behavior).
    """
    if not _codegraph_enabled():
        return
    if not rel.endswith(".py"):
        return
    try:
        from .codegraph import extract_symbols, extract_calls
    except Exception:
        return
    try:
        syms = extract_symbols(redacted_text, path=rel)
        for s in syms:
            conn.execute(
                "insert into code_symbols(path, qualname, kind, lineno, end_lineno, parent) "
                "values (?, ?, ?, ?, ?, ?)",
                (s.path, s.qualname, s.kind, s.lineno, s.end_lineno, s.parent),
            )
        calls = extract_calls(redacted_text, path=rel)
        for c in calls:
            conn.execute(
                "insert into code_calls(path, caller, callee, lineno) values (?, ?, ?, ?)",
                (c.path, c.caller, c.callee, c.lineno),
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

    if _emb.is_enabled():
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
    """Read FTS5 BM25 column weights from env, with safe defaults."""
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


def query(root: Path, text: str, *, limit: int = 5) -> dict[str, Any]:
    retriever = configured_retriever(root)
    if retriever != "bm25":
        raise RuntimeError(f"search retriever '{retriever}' is not implemented; use retriever: bm25")
    path_weight, content_weight = _bm25_weights()
    # Pull a wider candidate pool when dense rerank is enabled (per Codex report's
    # "lexical 100-500 → dense 20-100 → rerank 5-20" recipe — sized to corpus).
    try:
        from . import embedding as _emb
        dense_active = _emb.is_enabled() and _emb.is_model_present(root)
    except Exception:
        dense_active = False
    candidate_limit = max(limit * 8, 40) if dense_active else limit
    with connect(root) as conn:
        init_schema(conn)
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
            # RRF k=60 standard
            RRF_K = 60
            combined = []
            for r in fts_results:
                cid = r["id"]
                bm_r = bm25_rank.get(cid, candidate_limit)
                dn_r = dense_rank.get(cid, candidate_limit)
                fused = 1.0 / (RRF_K + bm_r + 1) + 1.0 / (RRF_K + dn_r + 1)
                row_copy = dict(r)
                row_copy["_rrf"] = fused
                combined.append(row_copy)
            combined.sort(key=lambda x: -x["_rrf"])
            fts_results = combined
            dense_used = True
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
    return {
        "ok": True,
        "query": text,
        "results": fts_results[:limit],
        "rg_fallback": fallback_used,
        "dense_rerank": dense_used,
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
        rel_posix = rel.as_posix()
        if any(rel_posix.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in SKIP_NAMES:
            continue
        if any(path.name.endswith(suffix) for suffix in SKIP_SUFFIXES):
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
    return snippet[:SNIPPET_MAX_BYTES]


def escape_fts_query(text: str) -> str:
    terms = [term.replace('"', "") for term in text.split() if term.strip()]
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)
