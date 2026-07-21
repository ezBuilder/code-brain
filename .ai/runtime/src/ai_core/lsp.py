"""LSP-grade symbol navigation (PoC).

Wraps `multilspy` (Microsoft, MIT) to expose precise reference resolution,
goto-definition, and workspace symbol lookup as a graceful, optional layer
on top of the existing heuristic codegraph.

Design constraints:
  - `multilspy` is an OPTIONAL dependency. Import is wrapped in try/except so
    this module always loads even when the extra isn't installed.
  - All public functions return a `dict` shape with at least `ok: bool` and
    a `reason: str` when unavailable. They never raise.
  - A small TTL memory cache (5s) is kept for `find_references` keyed by
    (root, file_path, line, column). Larger persistent caches are out of
    scope for this PoC.

The actual `multilspy.SyncLanguageServer` usage is intentionally NOT
implemented in this PoC — we only ship the detection layer, the API
contract, the cache scaffold, and shape-stable responses. A follow-up
round wires the real LSP calls behind the same surface.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any

# Optional dep: multilspy. We import lazily and never raise on absence.
try:  # pragma: no cover - exercised by absence test
    import multilspy  # type: ignore[import-not-found]

    _MULTILSPY_AVAILABLE = True
except Exception:  # noqa: BLE001 - any failure means "not usable"
    multilspy = None  # type: ignore[assignment]
    _MULTILSPY_AVAILABLE = False


# Known language server binaries we probe via PATH.
# Order is significant only for the returned `servers_detected` list.
_LANGUAGE_SERVERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("pyright-langserver", "pyright", "pylsp")),
    ("go", ("gopls",)),
    ("typescript", ("typescript-language-server",)),
    ("rust", ("rust-analyzer",)),
    ("c_cpp", ("clangd",)),
)


# ---------------------------------------------------------------------------
# Cache scaffold
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 5.0
_cache_lock = RLock()
_references_cache: dict[tuple[str, str, int, int, int, int], tuple[float, dict[str, Any]]] = {}


def _cache_get(key: tuple[str, str, int, int, int, int]) -> dict[str, Any] | None:
    with _cache_lock:
        item = _references_cache.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            _references_cache.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[str, str, int, int, int, int], value: dict[str, Any]) -> None:
    with _cache_lock:
        _references_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)


def _cache_clear() -> None:
    """Test helper — drop all cached entries."""
    with _cache_lock:
        _references_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_servers() -> list[str]:
    """Return the list of LSP server binaries currently visible on PATH."""
    found: list[str] = []
    for _lang, candidates in _LANGUAGE_SERVERS:
        for binary in candidates:
            if shutil.which(binary):
                found.append(binary)
                break  # one per language is enough
    return found


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "reason": reason}
    payload.update(extra)
    return payload


def _normalise_root(root: Path) -> Path:
    try:
        return root.resolve()
    except OSError:
        return root


# ---------------------------------------------------------------------------
# Real backend (multilspy, per-call, Python only). Wired behind lsp_available so
# the unavailable contract is unchanged. No daemon, no hooks — explicit calls only.
# OmO ships a TS unix-socket LSP daemon; that design is reimplemented here per-call,
# not ported. A warm daemon is intentionally out of scope (cold-start would flake the
# hot-path SLO if ever wired into a hook).
# ---------------------------------------------------------------------------


def _source_line(root: Path, rel_or_abs_path: str, line: int) -> str:
    """Best-effort unmodified source line at ``line`` (0-indexed)."""
    try:
        from .private_write import read_root_confined_text

        resolved_root = _normalise_root(root)
        p = Path(rel_or_abs_path)
        if p.is_absolute():
            p = p.resolve().relative_to(resolved_root)
        text, _state = read_root_confined_text(
            resolved_root / p,
            root=resolved_root,
            max_bytes=2_000_000,
            require_private=False,
        )
        lines = text.splitlines()
        if 0 <= line < len(lines):
            return lines[line][:2000]
    except (OSError, UnicodeDecodeError, ValueError):
        pass
    return ""


def _line_preview(root: Path, rel_or_abs_path: str, line: int) -> str:
    """Bounded display preview for a result location. Never raises."""
    return _source_line(root, rel_or_abs_path, line).strip()[:200]


def _map_location(loc: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Map a multilspy Location dict → {path, line, column, preview}. Pure; None if unusable."""
    if not isinstance(loc, dict):
        return None
    path = loc.get("relativePath") or loc.get("absolutePath") or ""
    if not path and isinstance(loc.get("uri"), str):
        path = loc["uri"].removeprefix("file://")
    rng = loc.get("range") if isinstance(loc.get("range"), dict) else {}
    start = rng.get("start") if isinstance(rng.get("start"), dict) else {}
    line = int(start.get("line", 0) or 0)
    column = int(start.get("character", 0) or 0)
    if not path:
        return None
    try:
        candidate = Path(str(path))
        if candidate.is_absolute():
            path = candidate.resolve().relative_to(_normalise_root(root)).as_posix()
        else:
            path = candidate.as_posix().lstrip("./")
    except (OSError, ValueError):
        return None
    return {"path": str(path), "line": line, "column": column,
            "preview": _line_preview(root, str(path), line)}


_IDENTIFIER_RE = re.compile(r"[^\W\d]\w*", flags=re.UNICODE)


def _index_fingerprint(root: Path) -> tuple[int, int]:
    try:
        from .search import db_path

        path = db_path(root)
        if path.is_symlink() or not path.is_file():
            return (0, 0)
        state = path.stat()
        return (int(state.st_mtime_ns), int(state.st_size))
    except OSError:
        return (0, 0)


def _open_codegraph(root: Path) -> sqlite3.Connection | None:
    try:
        from .search import db_path

        path = db_path(root)
        if path.is_symlink() or not path.is_file():
            return None
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in conn.execute(
                "select name from sqlite_master where type='table' and name in ('code_symbols','code_calls')"
            ).fetchall()
        }
        if tables != {"code_symbols", "code_calls"}:
            conn.close()
            return None
        return conn
    except (OSError, sqlite3.Error):
        return None


def _normalise_relative_path(root: Path, file_path: str) -> str | None:
    try:
        resolved_root = _normalise_root(root)
        candidate = Path(file_path)
        if candidate.is_absolute():
            return candidate.resolve().relative_to(resolved_root).as_posix()
        resolved = (resolved_root / candidate).resolve()
        return resolved.relative_to(resolved_root).as_posix()
    except (OSError, ValueError):
        return None


def _identifier_at(root: Path, file_path: str, line: int, column: int) -> str | None:
    rel = _normalise_relative_path(root, file_path)
    if rel is None:
        return None
    source_line = _source_line(root, rel, line)
    if not source_line:
        return None
    bounded_column = max(0, min(int(column), len(source_line)))
    matches = list(_IDENTIFIER_RE.finditer(source_line))
    for match in matches:
        if match.start() <= bounded_column <= match.end():
            return match.group(0)
    if matches:
        return min(matches, key=lambda match: min(abs(match.start() - bounded_column), abs(match.end() - bounded_column))).group(0)
    return None


def _call_projection(conn: sqlite3.Connection) -> str:
    columns = {str(row[1]) for row in conn.execute("pragma table_info(code_calls)").fetchall()}
    lexical = "lexical_callee" if "lexical_callee" in columns else "callee as lexical_callee"
    target = "target" if "target" in columns else "null as target"
    resolution = "resolution" if "resolution" in columns else "'lexical' as resolution"
    confidence = "confidence" if "confidence" in columns else "0.45 as confidence"
    return f"path, caller, callee, lineno, lang, {lexical}, {target}, {resolution}, {confidence}"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?",
            (name,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _reference_projection(conn: sqlite3.Connection) -> str | None:
    if not _has_table(conn, "code_references"):
        return None
    columns = {str(row[1]) for row in conn.execute("pragma table_info(code_references)").fetchall()}
    required = {
        "path",
        "scope",
        "name",
        "lexical_name",
        "kind",
        "lineno",
        "column",
        "end_lineno",
        "end_column",
        "lang",
        "target",
        "resolution",
        "confidence",
    }
    if not required <= columns:
        return None
    return ", ".join(sorted(required))


def _reference_anchor(
    conn: sqlite3.Connection,
    *,
    path: str,
    line: int,
    column: int,
    token: str,
) -> dict[str, Any] | None:
    projection = _reference_projection(conn)
    if projection is None:
        return None
    try:
        rows = conn.execute(
            f"select {projection} from code_references "
            "where path = ? and lineno = ? "
            "and column <= ? and (end_lineno > lineno or end_column >= ?) "
            "order by confidence desc, abs(column - ?), id limit 32",
            (path, line + 1, max(0, int(column)), max(0, int(column)), max(0, int(column))),
        ).fetchall()
    except sqlite3.Error:
        return None
    token_fold = token.casefold()
    for row in rows:
        item = dict(row)
        values = (
            str(item.get("lexical_name") or ""),
            str(item.get("name") or ""),
            str(item.get("target") or ""),
        )
        if any(value.casefold() == token_fold or value.casefold().endswith(f".{token_fold}") for value in values):
            return item
    return dict(rows[0]) if rows else None


def _call_anchor(
    conn: sqlite3.Connection,
    *,
    path: str,
    line: int,
    token: str,
) -> dict[str, Any] | None:
    projection = _call_projection(conn)
    try:
        rows = conn.execute(
            f"select {projection} from code_calls where path = ? and lineno = ? order by confidence desc, id limit 32",
            (path, line + 1),
        ).fetchall()
    except sqlite3.Error:
        return None
    token_fold = token.casefold()
    for row in rows:
        item = dict(row)
        values = (
            str(item.get("lexical_callee") or ""),
            str(item.get("callee") or ""),
            str(item.get("target") or ""),
        )
        if any(value.casefold() == token_fold or value.casefold().endswith(f".{token_fold}") for value in values):
            return item
    return dict(rows[0]) if rows else None


def _module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    while parts and parts[0] in {"src", "lib", "python"}:
        parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _definition_rank(row: dict[str, Any], *, names: set[str], target: str | None) -> tuple[int, int, str, int]:
    qualname = str(row.get("qualname") or "")
    canonical = ".".join(part for part in (_module_name(str(row.get("path") or "")), qualname) if part)
    if target and canonical == target:
        tier = 0
    elif target and target.endswith(f".{qualname}"):
        tier = 1
    elif qualname in names:
        tier = 2
    elif qualname.rsplit(".", 1)[-1] in {name.rsplit(".", 1)[-1] for name in names}:
        tier = 3
    else:
        tier = 4
    return (tier, len(qualname), str(row.get("path") or ""), int(row.get("lineno") or 0))


def _syntactic_definition(
    root: Path,
    file_path: str,
    line: int,
    column: int,
    *,
    fallback_reason: str,
) -> dict[str, Any] | None:
    rel = _normalise_relative_path(root, file_path)
    token = _identifier_at(root, file_path, line, column)
    conn = _open_codegraph(root)
    if rel is None or token is None or conn is None:
        if conn is not None:
            conn.close()
        return None
    using_reference_index = False
    try:
        anchor = _reference_anchor(
            conn,
            path=rel,
            line=line,
            column=column,
            token=token,
        ) or _call_anchor(conn, path=rel, line=line, token=token)
        names = {token}
        target: str | None = None
        if anchor:
            names.update(
                str(anchor.get(key) or "")
                for key in ("name", "lexical_name", "callee", "lexical_callee", "target")
                if anchor.get(key)
            )
            target = str(anchor.get("target") or "") or None
        tails = sorted({name.rsplit(".", 1)[-1] for name in names if name})[:16]
        placeholders = ",".join("?" for _ in tails)
        rows = conn.execute(
            f"select path, qualname, kind, lineno, end_lineno, parent, lang from code_symbols "
            f"where qualname in ({placeholders}) or "
            f"substr(qualname, length(qualname) - instr(reverse(qualname), '.') + 2) in ({placeholders}) "
            f"limit 256",
            [*tails, *tails],
        ).fetchall()
    except sqlite3.Error:
        # SQLite has no built-in reverse() on all builds; use a bounded LIKE fallback.
        try:
            clauses = " or ".join("qualname = ? or qualname like ?" for _ in tails)
            params: list[Any] = []
            for tail in tails:
                params.extend([tail, f"%.{tail}"])
            rows = conn.execute(
                f"select path, qualname, kind, lineno, end_lineno, parent, lang from code_symbols where {clauses} limit 256",
                params,
            ).fetchall() if tails else []
        except sqlite3.Error:
            rows = []
    finally:
        conn.close()
    candidates = [dict(row) for row in rows]
    if not candidates:
        return {
            "ok": True,
            "definition": None,
            "backend": "syntactic_codegraph",
            "precision": "syntactic",
            "fallback_reason": fallback_reason,
            "complete": False,
        }
    best = min(candidates, key=lambda row: _definition_rank(row, names=names, target=target))
    definition = {
        "path": str(best["path"]),
        "line": max(0, int(best["lineno"]) - 1),
        "column": 0,
        "preview": _line_preview(root, str(best["path"]), max(0, int(best["lineno"]) - 1)),
        "qualname": str(best["qualname"]),
        "kind": str(best["kind"]),
    }
    return {
        "ok": True,
        "definition": definition,
        "backend": "syntactic_codegraph",
        "precision": "syntactic",
        "fallback_reason": fallback_reason,
        "complete": False,
    }


def _syntactic_references(
    root: Path,
    file_path: str,
    line: int,
    column: int,
    *,
    fallback_reason: str,
    limit: int = 200,
) -> dict[str, Any] | None:
    rel = _normalise_relative_path(root, file_path)
    token = _identifier_at(root, file_path, line, column)
    conn = _open_codegraph(root)
    if rel is None or token is None or conn is None:
        if conn is not None:
            conn.close()
        return None
    try:
        anchor = _reference_anchor(
            conn,
            path=rel,
            line=line,
            column=column,
            token=token,
        ) or _call_anchor(conn, path=rel, line=line, token=token)
        names = {token}
        if anchor:
            names.update(
                str(anchor.get(key) or "")
                for key in ("name", "lexical_name", "callee", "lexical_callee", "target")
                if anchor.get(key)
            )
        names = {name for name in names if name}
        reference_projection = _reference_projection(conn)
        using_reference_index = reference_projection is not None
        if using_reference_index:
            clauses = []
            params = []
            for name in sorted(names)[:16]:
                escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append(
                    "name = ? or lexical_name = ? or target = ? or target like ? escape '\\'"
                )
                params.extend([name, name, name, f"%.{escaped}"])
            rows = conn.execute(
                f"select {reference_projection} from code_references "
                f"where {' or '.join(f'({clause})' for clause in clauses)} "
                "order by confidence desc, case when kind = 'import_binding' then 1 else 0 end, "
                "path, lineno, column, kind limit ?",
                [*params, max(1, min(500, int(limit)))],
            ).fetchall() if clauses else []
        else:
            projection = _call_projection(conn)
            clauses = []
            params = []
            for name in sorted(names)[:16]:
                escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append(
                    "callee = ? or lexical_callee = ? or target = ? or target like ? escape '\\'"
                )
                params.extend([name, name, name, f"%.{escaped}"])
            rows = conn.execute(
                f"select {projection} from code_calls where {' or '.join(f'({clause})' for clause in clauses)} "
                "order by confidence desc, path, lineno limit ?",
                [*params, max(1, min(500, int(limit)))],
            ).fetchall() if clauses else []
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    references: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str, str]] = set()
    for row in rows:
        item = dict(row)
        path = str(item["path"])
        zero_line = max(0, int(item["lineno"]) - 1)
        preview = _line_preview(root, path, zero_line)
        lexical = str(
            item.get("lexical_name")
            or item.get("lexical_callee")
            or item.get("name")
            or item.get("callee")
            or token
        )
        exact_column = max(0, int(item.get("column") or 0))
        key = (path, zero_line, exact_column, lexical, str(item.get("kind") or "call"))
        if key in seen:
            continue
        seen.add(key)
        references.append(
            {
                "path": path,
                "line": zero_line,
                "column": (
                    exact_column
                    if using_reference_index
                    else (max(0, preview.find(lexical.split(".", 1)[0])) if preview else 0)
                ),
                "end_line": max(0, int(item.get("end_lineno") or item["lineno"]) - 1),
                "end_column": max(exact_column, int(item.get("end_column") or exact_column)),
                "preview": preview,
                "scope": str(item.get("scope") or item.get("caller") or ""),
                "kind": str(item.get("kind") or "call"),
                "name": str(item.get("name") or item.get("callee") or ""),
                "lexical_name": lexical,
                "caller": str(item.get("caller") or item.get("scope") or ""),
                "callee": str(item.get("callee") or item.get("name") or ""),
                "target": str(item.get("target") or "") or None,
                "resolution": str(item.get("resolution") or "lexical"),
                "confidence": float(item.get("confidence") or 0.0),
            }
        )
    return {
        "ok": True,
        "references": references,
        "backend": "syntactic_codegraph",
        "precision": "syntactic",
        "fallback_reason": fallback_reason,
        "complete": False,
        "reference_index": "code_references" if using_reference_index else "code_calls_legacy",
    }


def _syntactic_workspace_symbols(
    root: Path,
    query: str,
    *,
    limit: int,
    fallback_reason: str,
) -> dict[str, Any] | None:
    conn = _open_codegraph(root)
    if conn is None:
        return None
    cap = max(0, min(200, int(limit)))
    try:
        escaped = str(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "select path, qualname, kind, lineno, parent, lang from code_symbols "
            "where qualname like ? escape '\\' order by length(qualname), path, lineno limit ?",
            (f"%{escaped}%", cap),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    symbols = [
        {
            "name": str(row["qualname"]),
            "kind": str(row["kind"]),
            "path": str(row["path"]),
            "line": max(0, int(row["lineno"]) - 1),
            "container": str(row["parent"] or "") or None,
            "language": str(row["lang"]),
        }
        for row in rows
    ]
    return {
        "ok": True,
        "symbols": symbols,
        "backend": "syntactic_codegraph",
        "precision": "syntactic",
        "fallback_reason": fallback_reason,
        "complete": False,
    }


def _lsp_call(root: Path, file_path: str, line: int, column: int, *, kind: str) -> list[dict[str, Any]] | None:
    """Per-call multilspy query (Python/pyright). Returns raw locations, or None on any failure."""
    try:
        from multilspy import SyncLanguageServer
        from multilspy.multilspy_config import MultilspyConfig
        from multilspy.multilspy_logger import MultilspyLogger

        config = MultilspyConfig.from_dict({"code_language": "python"})
        server = SyncLanguageServer.create(config, MultilspyLogger(), str(_normalise_root(root)))
        with server.start_server():
            if kind == "references":
                raw = server.request_references(file_path, line, column)
            else:
                raw = server.request_definition(file_path, line, column)
        return list(raw) if raw else []
    except Exception:  # noqa: BLE001 — any backend failure degrades to fallback, never raises
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lsp_available(root: Path) -> dict[str, Any]:
    """Probe whether LSP-grade navigation is usable from `root`.

    Returns a dict with the keys:
      ok                : True iff multilspy is importable AND at least one
                          known language server binary is on PATH.
      reason            : Short machine-readable cause when ok=False.
                          One of: 'multilspy_not_installed',
                          'no_language_server_on_path'.
      servers_detected  : List of binaries found on PATH (possibly empty).
    """
    servers = _detect_servers()
    if not _MULTILSPY_AVAILABLE:
        return {
            "ok": False,
            "reason": "multilspy_not_installed",
            "servers_detected": servers,
        }
    if not servers:
        return {
            "ok": False,
            "reason": "no_language_server_on_path",
            "servers_detected": servers,
        }
    return {
        "ok": True,
        "reason": "",
        "servers_detected": servers,
        "root": _normalise_root(root).as_posix(),
    }


def find_references(
    root: Path,
    file_path: str,
    line: int,
    column: int,
) -> dict[str, Any]:
    """Find all references to the symbol at `(line, column)` in `file_path`.

    `file_path` is interpreted relative to `root`. `line` and `column` follow
    the LSP convention (0-indexed).

    Response shape (always):
      {
        "ok": bool,
        "references": [
            {"path": str, "line": int, "column": int, "preview": str},
            ...
        ],
        "reason"?: str,
      }

    When the LSP layer is unavailable the function returns ok=False with a
    `reason` field; `references` is always present (empty list).
    """
    avail = lsp_available(root)
    if not avail["ok"]:
        fallback = _syntactic_references(
            root,
            file_path,
            int(line),
            int(column),
            fallback_reason=str(avail["reason"]),
        )
        if fallback is not None:
            return fallback
        return {
            "ok": False,
            "reason": avail["reason"],
            "references": [],
        }

    fingerprint = _index_fingerprint(root)
    cache_key = (
        _normalise_root(root).as_posix(),
        file_path,
        int(line),
        int(column),
        fingerprint[0],
        fingerprint[1],
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    raw = _lsp_call(root, file_path, int(line), int(column), kind="references")
    if raw is None:
        fallback = _syntactic_references(
            root,
            file_path,
            int(line),
            int(column),
            fallback_reason="lsp_query_failed",
        )
        if fallback is not None:
            _cache_put(cache_key, fallback)
            return fallback
        return {"ok": False, "reason": "lsp_query_failed", "references": []}
    refs = [m for loc in raw if (m := _map_location(loc, root)) is not None]
    result: dict[str, Any] = {
        "ok": True,
        "references": refs,
        "backend": "lsp",
        "precision": "precise",
        "complete": True,
    }
    _cache_put(cache_key, result)
    return result


def goto_definition(
    root: Path,
    file_path: str,
    line: int,
    column: int,
) -> dict[str, Any]:
    """Locate the definition for the symbol at `(line, column)`.

    Response shape (always):
      {
        "ok": bool,
        "definition": {"path": str, "line": int, "column": int, "preview": str} | None,
        "reason"?: str,
      }
    """
    avail = lsp_available(root)
    if not avail["ok"]:
        fallback = _syntactic_definition(
            root,
            file_path,
            int(line),
            int(column),
            fallback_reason=str(avail["reason"]),
        )
        if fallback is not None:
            return fallback
        return {
            "ok": False,
            "reason": avail["reason"],
            "definition": None,
        }
    raw = _lsp_call(root, file_path, int(line), int(column), kind="definition")
    if raw is None:
        fallback = _syntactic_definition(
            root,
            file_path,
            int(line),
            int(column),
            fallback_reason="lsp_query_failed",
        )
        if fallback is not None:
            return fallback
        return {"ok": False, "reason": "lsp_query_failed", "definition": None}
    definition: dict[str, Any] | None = None
    for loc in raw:
        definition = _map_location(loc, root)
        if definition is not None:
            break
    return {
        "ok": True,
        "definition": definition,
        "backend": "lsp",
        "precision": "precise",
        "complete": True,
    }


def workspace_symbols(
    root: Path,
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Fuzzy lookup of workspace-wide symbols matching `query`.

    Response shape (always):
      {
        "ok": bool,
        "symbols": [
            {"name": str, "kind": str, "path": str, "line": int,
             "container"?: str},
            ...
        ],
        "reason"?: str,
      }

    `limit` caps the returned list. It is honoured regardless of whether the
    LSP backend is wired up.
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 20
    if cap < 0:
        cap = 0

    avail = lsp_available(root)
    if not avail["ok"]:
        fallback = _syntactic_workspace_symbols(
            root,
            query,
            limit=cap,
            fallback_reason=str(avail["reason"]),
        )
        if fallback is not None:
            return fallback
        return {
            "ok": False,
            "reason": avail["reason"],
            "symbols": [],
        }

    fallback = _syntactic_workspace_symbols(
        root,
        query,
        limit=cap,
        fallback_reason="lsp_workspace_symbols_not_wired",
    )
    if fallback is not None:
        return fallback
    return {"ok": True, "symbols": [], "reason": "lsp_backend_not_wired"}


__all__ = [
    "lsp_available",
    "find_references",
    "goto_definition",
    "workspace_symbols",
]
