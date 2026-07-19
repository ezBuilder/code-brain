"""Self-validation eval + ratchet for cAST chunking (offline, deterministic).

Code Brain ships AST-aware ("cAST") chunking as opt-in (env ``AI_AST_CHUNK``).
This module lets the *system* decide whether cAST actually improves retrieval on
THIS repo's own corpus, instead of a human flipping a flag on faith.

How it works (all offline, stdlib only, no LLM, no network):

1. Build a self-supervised query set from the indexed repo. For every indexed
   Python symbol (function/class/method) that has a docstring, use the
   docstring's first non-empty line as the query and treat the chunk containing
   that symbol's definition as the relevant target.
2. Build two throwaway indexes in TEMP sqlite paths — one with the default
   (function-boundary) chunker, one with cAST chunking — over the same source
   files. The real index at ``.ai/cache/code.sqlite`` is never touched.
3. Measure recall@k (fraction of queries whose target chunk appears in the
   top-k FTS results) for each index.
4. Verdict: cAST wins iff ``recall_cast >= recall_default + margin`` AND
   ``n >= min_queries``. Persist it atomically to
   ``.ai/runtime/state/cast_verdict.json``.

:func:`verdict` reads that file so the indexer can auto-enable cAST once the
eval has proven it helps — without the env flag.

Fail-soft throughout: any error yields ``{"ok": False, "enabled": False,
"reason": ...}`` and never raises into the indexer/search hot path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import redact_value

# Default knobs (mirrors the indexer's function-chunk path so the comparison is
# apples-to-apples). Keep small + deterministic.
_FTS_TOKENIZE = 'porter unicode61 remove_diacritics 2'


def verdict_path(root: Path) -> Path:
    """Location of the persisted verdict JSON."""
    return Path(root) / ".ai" / "runtime" / "state" / "cast_verdict.json"


# ---------------------------------------------------------------------------
# Verdict read (used by the indexer; must be cheap + fail-soft)
# ---------------------------------------------------------------------------


def verdict(root: Path) -> bool:
    """Return True only if a persisted verdict enables cAST. Fail-soft False.

    Reads ``.ai/runtime/state/cast_verdict.json`` and returns ``data["enabled"]``
    coerced to bool. Any missing file, parse error, or unexpected shape yields
    ``False`` so the indexer falls back to the default chunker.
    """
    try:
        path = verdict_path(root)
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get("enabled") is True)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Self-supervised query set
# ---------------------------------------------------------------------------


def _docstring_first_line(source: str, lineno: int, end_lineno: int) -> str | None:
    """First non-empty docstring line for the def/class starting at ``lineno``.

    Parses just the symbol's source slice with the stdlib ``ast`` and reads
    ``ast.get_docstring`` of the first node. Returns ``None`` when there is no
    docstring. Pure/offline.
    """
    import ast as _ast
    import textwrap

    lines = source.split("\n")
    if lineno < 1 or end_lineno < lineno or end_lineno > len(lines):
        return None
    # Dedent so an indented method slice parses standalone (it would otherwise
    # raise IndentationError, a SyntaxError subclass).
    snippet = textwrap.dedent("\n".join(lines[lineno - 1 : end_lineno]))
    try:
        tree = _ast.parse(snippet)
    except (SyntaxError, ValueError, RecursionError):
        return None
    body = list(getattr(tree, "body", []) or [])
    if not body:
        return None
    node = body[0]
    if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
        return None
    doc = _ast.get_docstring(node)
    if not doc:
        return None
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def build_query_set(root: Path) -> list[dict[str, Any]]:
    """Build the self-supervised query set from the repo's Python files.

    For each Python symbol (function/class/method) that carries a docstring,
    emit ``{"query": <docstring first line>, "path": <rel>, "start_line",
    "end_line", "qualname"}``. The ``path``/line span identifies the chunk that
    must surface for the query to count as a hit. Pure/offline; fail-soft per
    file (a bad file is skipped, not fatal).
    """
    from . import search as _search
    from .codegraph import extract_symbols

    out: list[dict[str, Any]] = []
    for abs_path in _search.iter_text_files(root):
        if abs_path.suffix != ".py":
            continue
        rel = abs_path.relative_to(root).as_posix()
        try:
            content = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            symbols = extract_symbols(content, path=rel)
        except Exception:
            continue
        for sym in symbols:
            try:
                doc_line = _docstring_first_line(content, sym.lineno, sym.end_lineno)
            except Exception:
                doc_line = None
            if not doc_line:
                continue
            out.append({
                "query": doc_line,
                "path": rel,
                "start_line": int(sym.lineno),
                "end_line": int(sym.end_lineno),
                "qualname": sym.qualname,
            })
    # Deterministic order: by path then start line then query text.
    out.sort(key=lambda q: (q["path"], q["start_line"], q["query"]))
    return out


# ---------------------------------------------------------------------------
# Temp index build (default vs cAST), self-contained so the real index is safe
# ---------------------------------------------------------------------------


def _create_eval_schema(conn: sqlite3.Connection) -> None:
    """Minimal FTS schema for the eval index (path + content + line span)."""
    conn.execute("pragma journal_mode=memory")
    conn.executescript(
        f"""
        create table if not exists chunks (
          id integer primary key,
          path text not null,
          start_line integer,
          end_line integer
        );
        create virtual table if not exists chunks_fts using fts5(
          path, content, content='', tokenize="{_FTS_TOKENIZE}"
        );
        """
    )


def _file_chunks_for(path: str, content: str, *, use_cast: bool) -> list[dict[str, Any]]:
    """Function/class chunks for ``content`` under the chosen chunker.

    ``use_cast=True`` runs the cAST chunker (:func:`ast_chunker.chunk_python`);
    ``use_cast=False`` runs the default function-boundary chunker
    (:func:`search._function_chunks_for_python`). Both return dicts that at
    least carry ``text``/``start_line``/``end_line``. The decision is passed
    explicitly here (NOT read from env or verdict) so the comparison is
    independent of ambient state and free of circular dependency.
    """
    if use_cast:
        try:
            from .ast_chunker import chunk_python

            chunks = chunk_python(content)
        except Exception:
            chunks = []
        return [c for c in chunks if isinstance(c, dict)]
    try:
        from .search import _function_chunks_for_python

        return _function_chunks_for_python(path, content)
    except Exception:
        return []


def _build_eval_index(
    db_file: Path,
    files: list[tuple[str, str]],
    *,
    use_cast: bool,
) -> None:
    """Build a throwaway FTS index over ``files`` into ``db_file``.

    ``files`` is a list of ``(rel_path, content)``. Each file gets a file-level
    chunk plus function/class chunks from the chosen chunker (default vs cAST),
    mirroring the real indexer's hybrid chunking so recall is comparable.
    Content is redacted before indexing (same as the real indexer).
    """
    conn = sqlite3.connect(str(db_file))
    try:
        _create_eval_schema(conn)
        conn.execute("begin")
        for rel, content in files:
            redacted = str(redact_value(content))
            # File-level chunk (full file span).
            line_count = redacted.count("\n") + 1
            cur = conn.execute(
                "insert into chunks(path, start_line, end_line) values (?, ?, ?)",
                (rel, 1, line_count),
            )
            cid = int(cur.lastrowid)
            conn.execute(
                "insert into chunks_fts(rowid, path, content) values (?, ?, ?)",
                (cid, rel, redacted),
            )
            # Function/class level chunks.
            for func in _file_chunks_for(rel, content, use_cast=use_cast):
                text = func.get("text")
                start_line = func.get("start_line")
                end_line = func.get("end_line")
                if not isinstance(text, str) or not isinstance(start_line, int) or not isinstance(end_line, int):
                    continue
                qualname = func.get("qualname") or f"cast:{start_line}-{end_line}"
                chunk_path = f"{rel}:{qualname}"
                redacted_text = str(redact_value(text))
                fcur = conn.execute(
                    "insert into chunks(path, start_line, end_line) values (?, ?, ?)",
                    (chunk_path, start_line, end_line),
                )
                fcid = int(fcur.lastrowid)
                conn.execute(
                    "insert into chunks_fts(rowid, path, content) values (?, ?, ?)",
                    (fcid, chunk_path, redacted_text),
                )
        conn.commit()
    finally:
        conn.close()


def _escape_fts_query(text: str) -> str:
    """Use the production FTS query normalizer so eval and runtime cannot drift."""
    from .search import escape_fts_query

    return escape_fts_query(text)


def _query_index(conn: sqlite3.Connection, text: str, *, k: int) -> list[dict[str, Any]]:
    """Top-k FTS rows for ``text`` from an eval index, with line spans."""
    try:
        rows = conn.execute(
            """
            select c.path as path, c.start_line as start_line, c.end_line as end_line
            from chunks_fts
            join chunks c on c.id = chunks_fts.rowid
            where chunks_fts match ?
            order by bm25(chunks_fts)
            limit ?
            """,
            (_escape_fts_query(text), k),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"path": r[0], "start_line": r[1], "end_line": r[2]}
        for r in rows
    ]


def _is_hit(results: list[dict[str, Any]], target: dict[str, Any]) -> bool:
    """A query hits if a top-k result overlaps the target symbol's span.

    The result path may be the file path or a ``file:qualname`` function-chunk
    path; both are normalized to the file before comparing. A file-level result
    for the target file counts (it contains the symbol); a function chunk counts
    when its line span overlaps the target symbol's span.
    """
    tgt_path = target["path"]
    tgt_start = int(target["start_line"])
    tgt_end = int(target["end_line"])
    for r in results:
        rpath = str(r.get("path") or "")
        file_part = rpath.split(":", 1)[0]
        if file_part != tgt_path:
            continue
        rs = r.get("start_line")
        re_ = r.get("end_line")
        # File-level chunk (spans the whole file) → contains the symbol.
        if ":" not in rpath:
            return True
        if not isinstance(rs, int) or not isinstance(re_, int):
            continue
        # Overlap test between [rs, re_] and [tgt_start, tgt_end].
        if rs <= tgt_end and tgt_start <= re_:
            return True
    return False


def _recall_at_k(db_file: Path, queries: list[dict[str, Any]], *, k: int) -> float:
    """Fraction of ``queries`` whose target chunk appears in top-k results."""
    if not queries:
        return 0.0
    conn = sqlite3.connect(str(db_file))
    try:
        hits = 0
        for q in queries:
            results = _query_index(conn, q["query"], k=k)
            if _is_hit(results, q):
                hits += 1
        return hits / len(queries)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Persistence (atomic)
# ---------------------------------------------------------------------------


def _persist_verdict(root: Path, payload: dict[str, Any]) -> None:
    """Atomically write the verdict JSON (tmp file in same dir → os.replace)."""
    path = verdict_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(redact_value(payload), ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".cast_verdict.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public eval entry point
# ---------------------------------------------------------------------------


def evaluate(root: Path, *, k: int = 5, margin: float = 0.02, min_queries: int = 10) -> dict[str, Any]:
    """Self-supervised recall comparison: default chunker vs cAST.

    Returns a dict with at least ``ok``, ``enabled``, ``recall_default``,
    ``recall_cast``, ``n``, ``k``, ``margin``. Persists the verdict to
    ``.ai/runtime/state/cast_verdict.json``. Fail-soft: on any error returns
    ``{"ok": False, "enabled": False, "reason": ...}`` (no raise, no network).
    """
    try:
        root = Path(root)
        k = max(1, int(k))
        margin = float(margin)
        min_queries = max(0, int(min_queries))

        from . import search as _search

        # 1. Collect source files (same selection the real indexer uses) and
        #    the self-supervised query set.
        files: list[tuple[str, str]] = []
        for abs_path in _search.iter_text_files(root):
            if abs_path.suffix != ".py":
                continue
            try:
                content = abs_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            files.append((abs_path.relative_to(root).as_posix(), content))

        queries = build_query_set(root)
        n = len(queries)

        recall_default = 0.0
        recall_cast = 0.0
        if files and queries:
            with tempfile.TemporaryDirectory(prefix="cast_eval_") as tmpdir:
                default_db = Path(tmpdir) / "default.sqlite"
                cast_db = Path(tmpdir) / "cast.sqlite"
                _build_eval_index(default_db, files, use_cast=False)
                _build_eval_index(cast_db, files, use_cast=True)
                recall_default = _recall_at_k(default_db, queries, k=k)
                recall_cast = _recall_at_k(cast_db, queries, k=k)

        enabled = bool(recall_cast >= recall_default + margin and n >= min_queries)

        payload = {
            "ok": True,
            "enabled": enabled,
            "recall_default": round(recall_default, 6),
            "recall_cast": round(recall_cast, 6),
            "n": n,
            "k": k,
            "margin": margin,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        _persist_verdict(root, payload)
        return payload
    except Exception as exc:  # never raise into callers
        return {
            "ok": False,
            "enabled": False,
            "reason": str(redact_value(f"{type(exc).__name__}: {exc}"))[:240],
        }
