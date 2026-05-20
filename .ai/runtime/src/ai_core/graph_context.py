from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .redact import redact_text
from .search import connect, init_schema

MAX_LIMIT = 100
SNIPPET_LINES = 3


def pack_graph_context(
    root: Path,
    *,
    seed_paths: Iterable[str] | None = None,
    symbol_query: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Build a small deterministic context pack from codegraph adjacency tables."""
    bounded_limit = _bounded_limit(limit)
    paths = _normalize_paths(seed_paths or [])
    query = (symbol_query or "").strip()

    with connect(root) as conn:
        init_schema(conn)
        summaries = _load_summaries(conn)
        seed_symbols = _seed_symbols(conn, paths=paths, symbol_query=query, limit=bounded_limit)
        seed_qualnames = [row["qualname"] for row in seed_symbols]
        alias_map = _alias_map(seed_qualnames)
        aliases = sorted({alias for values in alias_map.values() for alias in values})
        caller_edges = _caller_edges(conn, aliases=aliases, limit=bounded_limit * 4)
        callee_edges = _callee_edges(conn, qualnames=seed_qualnames, paths=paths, limit=bounded_limit * 4)
        related_symbols = _related_symbols(conn, seed_symbols, caller_edges, callee_edges, limit=bounded_limit * 4)

    items: list[dict[str, Any]] = []
    for row in seed_symbols:
        items.append(_symbol_item(root, row, summaries, role="seed"))
    for row in caller_edges:
        items.append(_edge_item(root, row, summaries, relation="caller", matched_symbol=_match_alias(alias_map, row["callee"])))
    for row in callee_edges:
        items.append(_edge_item(root, row, summaries, relation="callee"))
    for row in related_symbols:
        items.append(_symbol_item(root, row, summaries, role="related"))

    deduped = _dedupe(items)
    deduped.sort(key=_sort_key)
    results = deduped[:bounded_limit]
    return {
        "ok": True,
        "limit": bounded_limit,
        "seed_paths": paths,
        "symbol_query": query,
        "seed_symbols": [_public_symbol(row) for row in seed_symbols],
        "count": len(results),
        "results": results,
        "additionalContext": "\n".join(_context_line(item) for item in results),
    }


def _bounded_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 20
    return max(1, min(MAX_LIMIT, value))


def _normalize_paths(paths: Iterable[str]) -> list[str]:
    cleaned = []
    for path in paths:
        value = str(path).strip()
        if not value:
            continue
        cleaned.append(value.lstrip("./"))
    return sorted(dict.fromkeys(cleaned))


def _load_summaries(conn) -> dict[str, str]:
    rows = conn.execute("select path, summary from summaries order by path").fetchall()
    return {str(row["path"]): redact_text(str(row["summary"] or "")) for row in rows}


def _seed_symbols(conn, *, paths: list[str], symbol_query: str, limit: int) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if paths:
        placeholders = ",".join("?" for _ in paths)
        rows.extend(conn.execute(
            f"""
            select path, qualname, kind, lineno, end_lineno, parent
            from code_symbols
            where path in ({placeholders})
            order by path, lineno, qualname
            limit ?
            """,
            [*paths, limit],
        ).fetchall())
    if symbol_query:
        rows.extend(conn.execute(
            """
            select path, qualname, kind, lineno, end_lineno, parent
            from code_symbols
            where qualname like ?
            order by length(qualname), path, lineno, qualname
            limit ?
            """,
            (f"%{symbol_query}%", limit),
        ).fetchall())
    return _dedupe_rows([dict(row) for row in rows], ("path", "qualname", "lineno"))[:limit]


def _alias_map(qualnames: list[str]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for qualname in qualnames:
        tail = qualname.rsplit(".", 1)[-1]
        aliases[qualname] = sorted({qualname, tail, f"self.{tail}", f"cls.{tail}"})
    return aliases


def _caller_edges(conn, *, aliases: list[str], limit: int) -> list[dict[str, Any]]:
    if not aliases:
        return []
    placeholders = ",".join("?" for _ in aliases)
    rows = conn.execute(
        f"""
        select path, caller, callee, lineno
        from code_calls
        where callee in ({placeholders})
        order by path, lineno, caller, callee
        limit ?
        """,
        [*aliases, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def _callee_edges(conn, *, qualnames: list[str], paths: list[str], limit: int) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if qualnames:
        placeholders = ",".join("?" for _ in qualnames)
        clauses.append(f"caller in ({placeholders})")
        params.extend(qualnames)
    if paths:
        placeholders = ",".join("?" for _ in paths)
        clauses.append(f"path in ({placeholders})")
        params.extend(paths)
    if not clauses:
        return []
    rows = conn.execute(
        f"""
        select path, caller, callee, lineno
        from code_calls
        where {" or ".join(clauses)}
        order by path, lineno, caller, callee
        limit ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def _related_symbols(conn, seed_symbols: list[dict[str, Any]], caller_edges: list[dict[str, Any]], callee_edges: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    names = {str(row["caller"]) for row in caller_edges}
    for row in callee_edges:
        callee = str(row["callee"])
        names.add(callee)
        names.add(callee.rsplit(".", 1)[-1])
    names.discard("<module>")
    names -= {str(row["qualname"]) for row in seed_symbols}
    if not names:
        return []
    rows: list[dict[str, Any]] = []
    for name in sorted(names):
        rows.extend(dict(row) for row in conn.execute(
            """
            select path, qualname, kind, lineno, end_lineno, parent
            from code_symbols
            where qualname = ? or qualname like ?
            order by length(qualname), path, lineno, qualname
            limit ?
            """,
            (name, f"%.{name}", limit),
        ).fetchall())
    return _dedupe_rows(rows, ("path", "qualname", "lineno"))[:limit]


def _symbol_item(root: Path, row: dict[str, Any], summaries: dict[str, str], *, role: str) -> dict[str, Any]:
    path = str(row["path"])
    qualname = str(row["qualname"])
    return {
        "kind": "symbol",
        "role": role,
        "path": path,
        "qualname": qualname,
        "symbol_kind": row["kind"],
        "line": int(row["lineno"]),
        "end_line": int(row["end_lineno"]),
        "summary": summaries.get(path, ""),
        "snippet": _source_snippet(root, path, int(row["lineno"])),
    }


def _edge_item(root: Path, row: dict[str, Any], summaries: dict[str, str], *, relation: str, matched_symbol: str | None = None) -> dict[str, Any]:
    path = str(row["path"])
    item = {
        "kind": "edge",
        "relation": relation,
        "path": path,
        "caller": str(row["caller"]),
        "callee": str(row["callee"]),
        "line": int(row["lineno"]),
        "summary": summaries.get(path, ""),
        "snippet": _source_snippet(root, path, int(row["lineno"])),
    }
    if matched_symbol:
        item["matched_symbol"] = matched_symbol
    return item


def _source_snippet(root: Path, rel_path: str, lineno: int) -> str:
    try:
        text = (root / rel_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines = redact_text(text).splitlines()
    if not lines:
        return ""
    start = max(1, lineno)
    end = min(len(lines), start + SNIPPET_LINES - 1)
    snippet = "\\n".join(f"L{idx}: {lines[idx - 1].strip()}" for idx in range(start, end + 1))
    return snippet[:480]


def _match_alias(alias_map: dict[str, list[str]], callee: str) -> str | None:
    for qualname, aliases in alias_map.items():
        if callee in aliases:
            return qualname
    return None


def _public_symbol(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": row["path"],
        "qualname": row["qualname"],
        "kind": row["kind"],
        "line": int(row["lineno"]),
        "end_line": int(row["end_lineno"]),
    }


def _dedupe_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for row in rows:
        key = tuple(row.get(item) for item in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for item in items:
        if item["kind"] == "symbol":
            key = ("symbol", item["path"], item["qualname"], item["line"], item.get("role"))
        else:
            key = ("edge", item["relation"], item["path"], item["caller"], item["callee"], item["line"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    kind_rank = {"symbol": 0, "edge": 1}.get(str(item.get("kind")), 9)
    role_rank = {"seed": 0, "caller": 1, "callee": 2, "related": 3}.get(str(item.get("role") or item.get("relation")), 9)
    return (
        kind_rank,
        role_rank,
        str(item.get("path", "")),
        int(item.get("line", 0) or 0),
        str(item.get("qualname") or item.get("caller") or ""),
        str(item.get("callee") or ""),
    )


def _context_line(item: dict[str, Any]) -> str:
    if item["kind"] == "symbol":
        return f"- symbol {item['qualname']} {item['path']}:{item['line']} {item['snippet'] or item['summary']}"
    return f"- {item['relation']} {item['caller']} -> {item['callee']} {item['path']}:{item['line']} {item['snippet'] or item['summary']}"
