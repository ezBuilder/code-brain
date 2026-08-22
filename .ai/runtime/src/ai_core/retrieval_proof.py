"""Bounded, local A/B proof for legacy versus graph/PPR context retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .graph_context import GRAPH_RANKING_POLICY
from .redact import redact_text
from .search import connect, context_pack, init_schema

MAX_REPEATS = 20
MAX_LIMIT = 20
MAX_QUERY_CHARS = 512


def _bounded_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _safe_expected_path(value: object) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or "\x00" in text or ":" in text or len(text) > 1_024:
        raise ValueError("expected_path must be a safe repository-relative path")
    normalized = path.as_posix()
    if not normalized:
        raise ValueError("expected_path must be a safe repository-relative path")
    return normalized


def _auto_query(root: Path) -> tuple[str, str]:
    try:
        with closing(connect(root)) as conn:
            init_schema(conn)
            symbols = [str(row[0]) for row in conn.execute("select qualname from code_symbols order by qualname")]
            callees = [str(row[0]) for row in conn.execute("select distinct callee from code_calls order by callee")]
    except (OSError, RuntimeError, sqlite3.Error):
        return "main", "fallback"
    aliases: dict[str, str] = {}
    for symbol in symbols:
        aliases.setdefault(symbol, symbol)
        aliases.setdefault(symbol.rsplit(".", 1)[-1], symbol)
    for callee in callees:
        if callee in aliases:
            return aliases[callee], "auto_graph_symbol"
    return (symbols[0], "auto_symbol") if symbols else ("main", "fallback")


def _normalize_query(root: Path, value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return _auto_query(root)
    if "\x00" in text or len(text) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be a non-empty string up to {MAX_QUERY_CHARS} characters")
    return text, "explicit"


def _percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _timing(values: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(median(values), 3),
        "p95_ms": round(_percentile_95(values), 3),
    }


def _legacy_references(payload: dict[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for rank, item in enumerate(payload.get("results") or [], start=1):
        if not isinstance(item, dict) or not item.get("path"):
            continue
        references.append(
            {
                "path": str(item["path"]),
                "rank": rank,
                "start_line": item.get("start_line") or item.get("line"),
                "end_line": item.get("end_line") or item.get("line"),
                "source": "lexical",
            }
        )
    return references


def _v2_references(payload: dict[str, Any]) -> list[dict[str, Any]]:
    receipt = payload.get("context_receipt")
    raw = receipt.get("references") if isinstance(receipt, dict) else []
    return [dict(item) for item in raw if isinstance(item, dict) and item.get("path")]


def _reference_signature(references: Iterable[dict[str, Any]]) -> str:
    canonical = [
        {
            "path": item.get("path"),
            "rank": item.get("rank"),
            "start_line": item.get("start_line"),
            "end_line": item.get("end_line"),
            "source": item.get("source"),
        }
        for item in references
    ]
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _deduplicated_paths(references: Iterable[dict[str, Any]], *, limit: int = 10) -> list[str]:
    paths: list[str] = []
    for item in references:
        path = str(item.get("path") or "")
        if path and path not in paths:
            paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _expected_metrics(
    references: list[dict[str, Any]],
    *,
    expected_path: str | None,
    start_line: int | None,
    end_line: int | None,
) -> dict[str, Any]:
    if expected_path is None:
        return {"path_rank": None, "span_overlap": None}
    matching = [item for item in references if str(item.get("path") or "") == expected_path]
    path_rank = min((index for index, item in enumerate(references, start=1) if item in matching), default=None)
    if start_line is None or end_line is None:
        return {"path_rank": path_rank, "span_overlap": None}
    expected_size = end_line - start_line + 1
    overlap = 0
    for item in matching:
        try:
            item_start = int(item.get("start_line"))
            item_end = int(item.get("end_line"))
        except (TypeError, ValueError):
            continue
        overlap = max(overlap, max(0, min(end_line, item_end) - max(start_line, item_start) + 1))
    return {"path_rank": path_rank, "span_overlap": round(overlap / expected_size, 6)}


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    paths = (
        root / ".ai" / "cache" / "code.sqlite",
        root / ".ai" / "cache" / "code.sqlite-wal",
        root / ".ai" / "cache" / "code.sqlite-shm",
        root / ".ai" / "cache" / "code-index-generation",
        root / ".ai" / "memory" / "evidence.jsonl",
    )
    snapshot: dict[str, tuple[int, str]] = {}
    for path in paths:
        if path.is_file():
            body = path.read_bytes()
            snapshot[path.relative_to(root).as_posix()] = (len(body), hashlib.sha256(body).hexdigest())
    return snapshot


def _effect_status(
    legacy: dict[str, Any],
    v2: dict[str, Any],
    *,
    expected_path: str | None,
) -> str:
    if expected_path is None:
        return "unmeasured"
    legacy_rank = legacy.get("path_rank")
    v2_rank = v2.get("path_rank")
    legacy_overlap = legacy.get("span_overlap")
    v2_overlap = v2.get("span_overlap")
    legacy_score = (
        legacy_rank is not None,
        float(legacy_overlap or 0.0),
        1.0 / float(legacy_rank) if legacy_rank else 0.0,
    )
    v2_score = (
        v2_rank is not None,
        float(v2_overlap or 0.0),
        1.0 / float(v2_rank) if v2_rank else 0.0,
    )
    if v2_score > legacy_score:
        return "improved"
    if v2_score < legacy_score:
        return "regressed"
    return "parity"


def prove_retrieval(
    root: Path,
    *,
    query: str | None = None,
    expected_path: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    repeats: int = 5,
    limit: int = 10,
) -> dict[str, Any]:
    """Compare legacy/v2 retrieval and prove bounded deterministic durability."""

    root = Path(root).resolve()
    bounded_repeats = _bounded_int(repeats, name="repeats", minimum=1, maximum=MAX_REPEATS)
    bounded_limit = _bounded_int(limit, name="limit", minimum=1, maximum=MAX_LIMIT)
    selected_query, selection = _normalize_query(root, query)
    normalized_expected = _safe_expected_path(expected_path)
    if (start_line is None) != (end_line is None):
        raise ValueError("start_line and end_line must be provided together")
    if start_line is not None and end_line is not None:
        start_line = _bounded_int(start_line, name="start_line", minimum=1, maximum=10_000_000)
        end_line = _bounded_int(end_line, name="end_line", minimum=start_line, maximum=10_000_000)
        if normalized_expected is None:
            raise ValueError("expected_path is required for a line span")

    context_pack(root, selected_query, limit=bounded_limit, representation="legacy", evidence_source=None)
    context_pack(root, selected_query, limit=bounded_limit, representation="v2", evidence_source=None)
    before = _snapshot(root)
    legacy_times: list[float] = []
    v2_times: list[float] = []
    legacy_signatures: set[str] = set()
    v2_signatures: set[str] = set()
    receipt_ids: set[str] = set()
    legacy_payload: dict[str, Any] = {}
    v2_payload: dict[str, Any] = {}
    legacy_refs: list[dict[str, Any]] = []
    v2_refs: list[dict[str, Any]] = []
    for _ in range(bounded_repeats):
        started = time.perf_counter()
        legacy_payload = context_pack(
            root,
            selected_query,
            limit=bounded_limit,
            representation="legacy",
            evidence_source=None,
        )
        legacy_times.append((time.perf_counter() - started) * 1_000)
        legacy_refs = _legacy_references(legacy_payload)
        legacy_signatures.add(_reference_signature(legacy_refs))

        started = time.perf_counter()
        v2_payload = context_pack(
            root,
            selected_query,
            limit=bounded_limit,
            representation="v2",
            evidence_source=None,
        )
        v2_times.append((time.perf_counter() - started) * 1_000)
        v2_refs = _v2_references(v2_payload)
        v2_signatures.add(_reference_signature(v2_refs))
        receipt = v2_payload.get("context_receipt")
        if isinstance(receipt, dict) and receipt.get("receipt_id"):
            receipt_ids.add(str(receipt["receipt_id"]))
    after = _snapshot(root)

    legacy_expected = _expected_metrics(
        legacy_refs,
        expected_path=normalized_expected,
        start_line=start_line,
        end_line=end_line,
    )
    v2_expected = _expected_metrics(
        v2_refs,
        expected_path=normalized_expected,
        start_line=start_line,
        end_line=end_line,
    )
    graph = v2_payload.get("graph_context") if isinstance(v2_payload.get("graph_context"), dict) else {}
    budget = v2_payload.get("context_budget") if isinstance(v2_payload.get("context_budget"), dict) else {}
    actual_bytes = len(str(v2_payload.get("additionalContext") or "").encode("utf-8"))
    max_bytes = int(budget.get("max_bytes") or 0)
    effect = _effect_status(legacy_expected, v2_expected, expected_path=normalized_expected)
    checks = {
        "legacy_deterministic": len(legacy_signatures) == 1,
        "v2_deterministic": len(v2_signatures) == 1 and len(receipt_ids) == 1,
        "retrieval_files_unchanged": before == after,
        "context_budget_ok": max_bytes > 0 and actual_bytes <= max_bytes,
        "v2_default_contract": (
            v2_payload.get("representation") == "v2"
            and graph.get("ranking_policy") == GRAPH_RANKING_POLICY
        ),
        "expected_path_found": normalized_expected is None or v2_expected["path_rank"] is not None,
        "quality_not_regressed": effect != "regressed",
    }
    return {
        "ok": all(checks.values()),
        "schema_version": 1,
        "query": redact_text(selected_query)[:MAX_QUERY_CHARS],
        "query_selection": selection,
        "settings": {"repeats": bounded_repeats, "limit": bounded_limit},
        "expected": {
            "path": normalized_expected,
            "start_line": start_line,
            "end_line": end_line,
        },
        "legacy": {
            **_timing(legacy_times),
            **legacy_expected,
            "top_paths": _deduplicated_paths(legacy_refs),
            "signature_count": len(legacy_signatures),
        },
        "v2": {
            **_timing(v2_times),
            **v2_expected,
            "top_paths": _deduplicated_paths(v2_refs),
            "signature_count": len(v2_signatures),
            "receipt_count": len(receipt_ids),
            "graph_ranking_applied": bool(graph.get("ranking_applied")),
            "ranked_node_count": int(graph.get("ranked_node_count") or 0),
            "ranking_policy": graph.get("ranking_policy"),
            "context_bytes": actual_bytes,
            "max_context_bytes": max_bytes,
        },
        "effect": {
            "status": effect,
            "path_rank_delta": (
                legacy_expected["path_rank"] - v2_expected["path_rank"]
                if legacy_expected["path_rank"] is not None and v2_expected["path_rank"] is not None
                else None
            ),
            "span_overlap_delta": (
                round(v2_expected["span_overlap"] - legacy_expected["span_overlap"], 6)
                if legacy_expected["span_overlap"] is not None and v2_expected["span_overlap"] is not None
                else None
            ),
        },
        "durability": {
            "files_checked": sorted(set(before) | set(after)),
            "unchanged": before == after,
        },
        "checks": checks,
    }


__all__ = ["prove_retrieval"]
