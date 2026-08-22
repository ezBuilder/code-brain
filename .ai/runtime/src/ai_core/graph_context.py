from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .graph_rank import GraphRankEdge, bounded_personalized_rank
from .private_write import read_root_confined_text
from .redact import redact_text
from .search import connect, init_schema

MAX_LIMIT = 100
SNIPPET_LINES = 3
MAX_SEED_PATHS = 100
MAX_PATH_CHARS = 1024
MAX_SYMBOL_QUERY_CHARS = 512
MAX_SOURCE_BYTES = 512 * 1024
MAX_SOURCE_CACHE_PATHS = 32
MAX_RELATED_NAMES = 400
MAX_CONTEXT_BYTES = 64 * 1024
MAX_SUMMARY_CHARS = 1000
MAX_SKELETON_SUMMARY_CHARS = 240
MAX_GENERATION_CHARS = 128
SHA256_CHARS = 64
STALE_SOURCE_MARKER = "[STALE_SOURCE]"
UNKNOWN_SOURCE_MARKER = "[UNVERIFIED_SOURCE]"
GRAPH_CONTEXT_SCHEMA_VERSION = 2
GRAPH_RANKING_POLICY = "bounded-personalized-pagerank-over-one-hop"
GRAPH_PPR_MAX_HOPS = 2
GRAPH_PPR_MAX_NODES = 2_048
GRAPH_PPR_MAX_EDGES = 1_600
GRAPH_PPR_ITERATIONS = 25
GRAPH_PPR_RESTART = 0.25
REPRESENTATIONS = frozenset({"full", "skeleton", "refs-only"})


def pack_graph_context(
    root: Path,
    *,
    seed_paths: Iterable[str] | None = None,
    symbol_query: str | None = None,
    limit: int = 20,
    representation: str = "full",
) -> dict[str, Any]:
    """Build a small deterministic context pack from codegraph adjacency tables."""
    root = Path(os.path.abspath(root))
    bounded_limit = _bounded_limit(limit)
    normalized_representation = _normalize_representation(representation)
    if normalized_representation is None:
        return _empty_payload(
            bounded_limit,
            reason="invalid_representation",
        )
    representation = normalized_representation
    try:
        paths = _normalize_paths(seed_paths or [])
    except ValueError:
        return _empty_payload(
            bounded_limit,
            representation=representation,
            reason="invalid_seed_path",
        )
    query = str(symbol_query or "").strip()
    if "\x00" in query or len(query) > MAX_SYMBOL_QUERY_CHARS:
        return _empty_payload(
            bounded_limit,
            paths=paths,
            query=query,
            representation=representation,
            reason="invalid_symbol_query",
        )
    source_generation = _load_source_generation(root)

    try:
        with closing(connect(root)) as conn:
            init_schema(conn)
            seed_symbols = _sanitize_rows(
                _seed_symbols(conn, paths=paths, symbol_query=query, limit=bounded_limit)
            )
            seed_qualnames = [row["qualname"] for row in seed_symbols]
            alias_map = _alias_map(seed_qualnames)
            aliases = sorted({alias for values in alias_map.values() for alias in values})
            caller_edges = _sanitize_rows(
                _caller_edges(conn, aliases=aliases, limit=bounded_limit * 4)
            )
            callee_edges = _sanitize_rows(
                _callee_edges(conn, qualnames=seed_qualnames, paths=paths, limit=bounded_limit * 4)
            )
            related_symbols = _sanitize_rows(
                _related_symbols(
                    conn,
                    seed_symbols,
                    caller_edges,
                    callee_edges,
                    limit=bounded_limit * 4,
                )
            )
            summary_paths = sorted({
                str(row["path"])
                for row in [*seed_symbols, *caller_edges, *callee_edges, *related_symbols]
            })
            summaries = _load_summaries(conn, summary_paths)
            indexed_hashes = _load_indexed_hashes(conn, summary_paths)
    except (sqlite3.Error, OSError):
        return _empty_payload(
            bounded_limit,
            paths=paths,
            query=query,
            source_generation=source_generation,
            representation=representation,
            reason="index_unavailable",
        )

    items: list[dict[str, Any]] = []
    for row in seed_symbols:
        items.append(_symbol_item(row, summaries, role="seed"))
    for row in caller_edges:
        items.append(_edge_item(row, summaries, relation="caller", matched_symbol=_match_alias(alias_map, row["callee"])))
    for row in callee_edges:
        items.append(_edge_item(row, summaries, relation="callee"))
    for row in related_symbols:
        items.append(_symbol_item(row, summaries, role="related"))

    deduped = _dedupe(items)
    ranked_items, ranking = _rank_graph_items(deduped, aliases)
    results = ranked_items[:bounded_limit]
    source_cache: dict[str, dict[str, Any]] = {}
    for item in results:
        snapshot = _source_snapshot(
            root,
            str(item["path"]),
            source_cache,
        )
        _apply_source_trust(
            item,
            snapshot,
            indexed_hashes.get(str(item["path"])),
            source_generation=source_generation,
        )
        if item.get("source_status") != "current":
            item["summary"] = ""
        if representation == "full":
            item["snippet"] = (
                _snippet_from_snapshot(snapshot, int(item["line"]))
                if item.get("source_status") == "current"
                else ""
            )
        elif representation == "skeleton":
            item.pop("snippet", None)
            item["summary"] = str(item.get("summary") or "")[:MAX_SKELETON_SUMMARY_CHARS]
    if representation == "refs-only":
        results = [_reference_item(item) for item in results]
    return {
        "ok": True,
        "schema_version": GRAPH_CONTEXT_SCHEMA_VERSION,
        "ranking_policy": GRAPH_RANKING_POLICY,
        "ranking_applied": ranking["applied"],
        "ranking_parameters": ranking["parameters"],
        "ranked_node_count": ranking["ranked_node_count"],
        "representation": representation,
        "source_generation": source_generation,
        "source_generation_status": "known" if source_generation is not None else "unknown",
        "limit": bounded_limit,
        "seed_paths": paths,
        "symbol_query": _bounded_query_echo(query),
        "seed_symbols": [_public_symbol(row) for row in seed_symbols],
        "count": len(results),
        "results": results,
        "additionalContext": (
            _bounded_reference_context(results)
            if representation == "refs-only"
            else _bounded_context(results)
        ),
    }


def _bounded_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError, OverflowError):
        value = 20
    return max(1, min(MAX_LIMIT, value))


def _empty_payload(
    limit: int,
    *,
    paths: list[str] | None = None,
    query: str = "",
    source_generation: str | None = None,
    representation: str = "full",
    reason: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "schema_version": GRAPH_CONTEXT_SCHEMA_VERSION,
        "ranking_policy": GRAPH_RANKING_POLICY,
        "ranking_applied": False,
        "ranking_parameters": _ranking_parameters(),
        "ranked_node_count": 0,
        "representation": representation,
        "source_generation": source_generation,
        "source_generation_status": "known" if source_generation is not None else "unknown",
        "limit": limit,
        "seed_paths": paths or [],
        "symbol_query": _bounded_query_echo(query),
        "seed_symbols": [],
        "count": 0,
        "results": [],
        "additionalContext": "",
    }


def _normalize_representation(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in REPRESENTATIONS else None


def _bounded_query_echo(value: object) -> str:
    """Return a redacted, bounded query echo; never expose the raw query."""
    return redact_text(str(value or "")[:MAX_SYMBOL_QUERY_CHARS])[:MAX_SYMBOL_QUERY_CHARS]


def _load_source_generation(root: Path) -> str | None:
    marker = root / ".ai" / "cache" / "code-index-generation"
    try:
        value, _state = read_root_confined_text(
            marker,
            root=root,
            max_bytes=MAX_GENERATION_CHARS,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except (OSError, UnicodeDecodeError):
        return None
    value = value.strip()
    if (
        not value
        or len(value) > MAX_GENERATION_CHARS
        or not value.isdigit()
        or any(ord(char) < 32 for char in value)
    ):
        return None
    return value


def _safe_relative_path(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or "\x00" in text or len(text) > MAX_PATH_CHARS:
        return None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        return None
    return Path(*parts).as_posix()


def _normalize_paths(paths: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for path in paths:
        if len(cleaned) >= MAX_SEED_PATHS:
            raise ValueError("too many seed paths")
        value = _safe_relative_path(path)
        if value is None:
            if not str(path or "").strip():
                continue
            raise ValueError("invalid seed path")
        if value in cleaned:
            continue
        cleaned.append(value)
    return sorted(cleaned)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _load_summaries(conn, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    summaries: dict[str, str] = {}
    for offset in range(0, len(paths), 400):
        chunk = paths[offset:offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"select path, summary from summaries where path in ({placeholders}) order by path",
            chunk,
        ).fetchall()
        for row in rows:
            summaries[str(row["path"])] = redact_text(str(row["summary"] or ""))[:MAX_SUMMARY_CHARS]
    return summaries


def _load_indexed_hashes(conn, paths: list[str]) -> dict[str, str]:
    """Load only authoritative file-level hashes, never function-chunk hashes."""
    if not paths:
        return {}
    indexed: dict[str, str] = {}
    for offset in range(0, len(paths), 400):
        chunk = paths[offset:offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            select c.path, c.sha256
            from chunks c
            join chunk_meta m on m.chunk_id = c.id
            where m.kind = 'file' and c.path in ({placeholders})
            order by c.path
            """,
            chunk,
        ).fetchall()
        for row in rows:
            path = _safe_relative_path(row["path"])
            digest = str(row["sha256"] or "").lower()
            if path is None or len(digest) != SHA256_CHARS:
                continue
            try:
                int(digest, 16)
            except ValueError:
                continue
            indexed[path] = digest
    return indexed


def _seed_symbols(conn, *, paths: list[str], symbol_query: str, limit: int) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if paths:
        placeholders = ",".join("?" for _ in paths)
        rows.extend(conn.execute(
            f"""
            select path, qualname, kind, lineno, end_lineno, parent
                   , lang
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
            select path, qualname, kind, lineno, end_lineno, parent, lang
            from code_symbols
            where qualname like ? escape '\\'
            order by length(qualname), path, lineno, qualname
            limit ?
            """,
            (f"%{_escape_like(symbol_query)}%", limit),
        ).fetchall())
    return _dedupe_rows([dict(row) for row in rows], ("path", "qualname", "lineno"))[:limit]


def _alias_map(qualnames: list[str]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for qualname in qualnames:
        if not qualname or len(qualname) > MAX_SYMBOL_QUERY_CHARS or "\x00" in qualname:
            continue
        tail = qualname.rsplit(".", 1)[-1]
        aliases[qualname] = sorted({qualname, tail, f"self.{tail}", f"cls.{tail}"})
    return aliases


def _caller_edges(conn, *, aliases: list[str], limit: int) -> list[dict[str, Any]]:
    if not aliases:
        return []
    placeholders = ",".join("?" for _ in aliases)
    rows = conn.execute(
        f"""
        select path, caller, callee, lineno, lang
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
        select path, caller, callee, lineno, lang
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
    for name in sorted(names)[:MAX_RELATED_NAMES]:
        if len(rows) >= limit:
            break
        if not name or len(name) > MAX_SYMBOL_QUERY_CHARS or "\x00" in name:
            continue
        remaining = max(1, limit - len(rows))
        rows.extend(dict(row) for row in conn.execute(
            """
            select path, qualname, kind, lineno, end_lineno, parent, lang
            from code_symbols
            where qualname = ? or qualname like ? escape '\\'
            order by length(qualname), path, lineno, qualname
            limit ?
            """,
            (name, f"%.{_escape_like(name)}", remaining),
        ).fetchall())
    return _dedupe_rows(rows, ("path", "qualname", "lineno"))[:limit]


def _sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for row in rows:
        path = _safe_relative_path(row.get("path"))
        if path is None:
            continue
        try:
            lineno = int(row.get("lineno") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if lineno <= 0 or lineno > 10_000_000:
            continue
        clean = dict(row)
        clean["path"] = path
        clean["lineno"] = lineno
        for key in ("qualname", "caller", "callee", "kind", "parent", "lang"):
            if key not in clean:
                continue
            value = redact_text(str(clean.get(key) or ""))[:MAX_SYMBOL_QUERY_CHARS]
            if "\x00" in value:
                value = value.replace("\x00", "")
            clean[key] = value
        if "end_lineno" in clean:
            try:
                end_lineno = int(clean.get("end_lineno") or lineno)
            except (TypeError, ValueError, OverflowError):
                end_lineno = lineno
            clean["end_lineno"] = max(lineno, min(10_000_000, end_lineno))
        sanitized.append(clean)
    return sanitized


def _symbol_item(row: dict[str, Any], summaries: dict[str, str], *, role: str) -> dict[str, Any]:
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
        "span": {
            "start_line": int(row["lineno"]),
            "end_line": int(row["end_lineno"]),
        },
        "provenance": _provenance(row.get("lang")),
        "summary": summaries.get(path, ""),
        "snippet": "",
    }


def _edge_item(row: dict[str, Any], summaries: dict[str, str], *, relation: str, matched_symbol: str | None = None) -> dict[str, Any]:
    path = str(row["path"])
    line = int(row["lineno"])
    item = {
        "kind": "edge",
        "relation": relation,
        "path": path,
        "caller": str(row["caller"]),
        "callee": str(row["callee"]),
        "line": line,
        "end_line": line,
        "span": {"start_line": line, "end_line": line},
        "provenance": _provenance(row.get("lang")),
        "target_resolution": "lexical",
        "summary": summaries.get(path, ""),
        "snippet": "",
    }
    if matched_symbol:
        item["matched_symbol"] = matched_symbol
    return item


def _provenance(lang: object) -> dict[str, Any]:
    normalized = str(lang or "").strip().lower()
    if normalized == "python":
        return {
            "origin": "python_ast",
            "evidence_kind": "extracted",
            "confidence": "high",
        }
    if normalized in {"javascript", "typescript", "go", "rust"}:
        return {
            "origin": "ast_grep",
            "evidence_kind": "extracted",
            "confidence": "high",
        }
    return {
        "origin": "unknown_extractor",
        "evidence_kind": "unknown",
        "confidence": "unknown",
    }


def _source_snapshot(
    root: Path,
    rel_path: str,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    safe_rel = _safe_relative_path(rel_path)
    if safe_rel is None:
        return {"readable": False, "lines": [], "sha256": None}
    cached = cache.get(safe_rel)
    if cached is not None:
        return cached
    try:
        text, _state = read_root_confined_text(
            root / safe_rel,
            root=root,
            max_bytes=MAX_SOURCE_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except (OSError, UnicodeDecodeError):
        snapshot = {"readable": False, "lines": [], "sha256": None}
    else:
        redacted = redact_text(text)
        snapshot = {
            "readable": True,
            "lines": redacted.splitlines(),
            "sha256": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        }
    if len(cache) < MAX_SOURCE_CACHE_PATHS:
        cache[safe_rel] = snapshot
    return snapshot


def _apply_source_trust(
    item: dict[str, Any],
    snapshot: dict[str, Any],
    indexed_sha256: str | None,
    *,
    source_generation: str | None,
) -> None:
    current_sha256 = snapshot.get("sha256") if snapshot.get("readable") else None
    item["source_generation"] = source_generation
    item["indexed_content_sha256"] = indexed_sha256
    item["current_content_sha256"] = current_sha256
    if indexed_sha256 and current_sha256 == indexed_sha256:
        item["source_status"] = "current"
        item["invalidated"] = False
        return
    if indexed_sha256 and current_sha256 is not None:
        item["source_status"] = "stale"
        item["source_status_reason"] = "indexed_hash_mismatch"
        item["stale_marker"] = STALE_SOURCE_MARKER
        item["invalidated"] = True
        return
    item["source_status"] = "unknown"
    item["source_status_reason"] = "indexed_hash_or_source_unavailable"
    item["invalidated"] = None


def _snippet_from_snapshot(snapshot: dict[str, Any], lineno: int) -> str:
    if not snapshot.get("readable") or snapshot.get("sha256") is None:
        return ""
    lines = snapshot.get("lines") or []
    if not lines:
        return ""
    start = max(1, lineno)
    end = min(len(lines), start + SNIPPET_LINES - 1)
    snippet = "\\n".join(f"L{idx}: {lines[idx - 1].strip()}" for idx in range(start, end + 1))
    return snippet[:480]


def _reference_item(item: dict[str, Any]) -> dict[str, Any]:
    if item["kind"] == "symbol":
        reason = str(item.get("role") or "symbol")
    else:
        reason = str(item.get("relation") or "edge")
    if item.get("source_status") == "stale":
        reason = "stale_source"
    elif item.get("source_status") != "current":
        reason = "unverified_source"
    reference = {
        "path": item["path"],
        "span": dict(item["span"]),
        "reason": reason,
    }
    if "graph_score" in item:
        reference["graph_score"] = item["graph_score"]
        reference["graph_distance"] = item["graph_distance"]
    return reference


def _bounded_reference_context(results: list[dict[str, Any]]) -> str:
    lines = [
        f"- ref {item['path']}:{item['span']['start_line']}-{item['span']['end_line']} {item['reason']}"
        for item in results
    ]
    output: list[str] = []
    total = 0
    for line in lines:
        encoded = line.encode("utf-8")
        separator = 1 if output else 0
        if total + separator + len(encoded) > MAX_CONTEXT_BYTES:
            break
        output.append(line)
        total += separator + len(encoded)
    return "\n".join(output)


def _bounded_context(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    total = 0
    for item in results:
        line = _context_line(item)
        encoded = line.encode("utf-8")
        separator = 1 if lines else 0
        if total + separator + len(encoded) > MAX_CONTEXT_BYTES:
            break
        lines.append(line)
        total += separator + len(encoded)
    return "\n".join(lines)


def _match_alias(alias_map: dict[str, list[str]], callee: str) -> str | None:
    for qualname, aliases in alias_map.items():
        if callee in aliases:
            return qualname
    return None


def _public_symbol(row: dict[str, Any]) -> dict[str, Any]:
    start_line = int(row["lineno"])
    end_line = int(row["end_lineno"])
    return {
        "path": row["path"],
        "qualname": row["qualname"],
        "kind": row["kind"],
        "line": start_line,
        "end_line": end_line,
        "span": {"start_line": start_line, "end_line": end_line},
        "provenance": _provenance(row.get("lang")),
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


def _ranking_parameters() -> dict[str, Any]:
    return {
        "max_hops": GRAPH_PPR_MAX_HOPS,
        "max_nodes": GRAPH_PPR_MAX_NODES,
        "max_edges": GRAPH_PPR_MAX_EDGES,
        "iterations": GRAPH_PPR_ITERATIONS,
        "restart": GRAPH_PPR_RESTART,
        "bidirectional": True,
        "excluded_relations": ["contains"],
    }


def _item_rank_node(item: dict[str, Any]) -> str:
    if item.get("kind") == "symbol":
        return str(item.get("qualname") or "")
    if item.get("relation") == "caller":
        return str(item.get("caller") or "")
    return str(item.get("callee") or "")


def _rank_graph_items(
    items: list[dict[str, Any]],
    seed_aliases: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parameters = _ranking_parameters()
    edges = [
        GraphRankEdge(
            source=str(item.get("caller") or ""),
            target=str(item.get("callee") or ""),
            relation="calls",
        )
        for item in items
        if item.get("kind") == "edge" and item.get("caller") and item.get("callee")
    ]
    graph_nodes = {edge.source for edge in edges} | {edge.target for edge in edges}
    seeds = sorted({alias for alias in seed_aliases if alias in graph_nodes})
    if not edges or not seeds:
        return sorted(items, key=_sort_key), {
            "applied": False,
            "parameters": parameters,
            "ranked_node_count": 0,
        }
    try:
        ranked_nodes = bounded_personalized_rank(
            edges,
            seeds,
            limit=GRAPH_PPR_MAX_NODES,
            max_hops=GRAPH_PPR_MAX_HOPS,
            max_nodes=GRAPH_PPR_MAX_NODES,
            max_edges=GRAPH_PPR_MAX_EDGES,
            restart=GRAPH_PPR_RESTART,
            iterations=GRAPH_PPR_ITERATIONS,
            bidirectional=True,
            excluded_relations=("contains",),
        )
    except ValueError:
        return sorted(items, key=_sort_key), {
            "applied": False,
            "parameters": parameters,
            "ranked_node_count": 0,
        }
    ranks = {ranked.node: ranked for ranked in ranked_nodes}
    annotated: list[dict[str, Any]] = []
    for item in items:
        candidate = dict(item)
        ranked = ranks.get(_item_rank_node(candidate))
        if ranked is not None:
            candidate["graph_score"] = round(ranked.score, 12)
            candidate["graph_distance"] = ranked.distance
        annotated.append(candidate)
    annotated.sort(
        key=lambda item: (
            0 if "graph_score" in item else 1,
            -float(item.get("graph_score", 0.0)),
            int(item.get("graph_distance", GRAPH_PPR_MAX_HOPS + 1)),
            _sort_key(item),
        )
    )
    return annotated, {
        "applied": bool(ranked_nodes),
        "parameters": parameters,
        "ranked_node_count": len(ranked_nodes),
    }


def _context_line(item: dict[str, Any]) -> str:
    if item.get("source_status") == "stale":
        suffix = f" {item['stale_marker']}"
    elif item.get("source_status") == "unknown":
        suffix = f" {UNKNOWN_SOURCE_MARKER}"
    else:
        suffix = ""
    evidence = item.get("snippet") or item.get("summary") or ""
    if item["kind"] == "symbol":
        return f"- symbol {item['qualname']} {item['path']}:{item['line']} {evidence}{suffix}"
    return f"- {item['relation']} {item['caller']} -> {item['callee']} {item['path']}:{item['line']} {evidence}{suffix}"
