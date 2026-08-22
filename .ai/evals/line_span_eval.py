"""Pure, deterministic evaluator for file and line-span retrieval.

The evaluator intentionally accepts ranked retrieval observations instead of
calling a retriever.  This makes the metric contract testable before a
production adapter exposes trustworthy source spans.

All spans are one-based and inclusive.  A candidate with only a matching path
is a file hit, never a span hit.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

MAX_K = 1_000
MAX_ITEMS = 10_000
MAX_LINE = 1_000_000
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class LineSpanEvalError(ValueError):
    """Raised when an evaluation input violates the metric contract."""


def _path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise LineSpanEvalError(f"{field} must be a non-empty POSIX path")
    if "\x00" in value or "\\" in value or any(ord(char) < 32 for char in value):
        raise LineSpanEvalError(f"{field} contains a forbidden path character")
    if value.startswith("/") or _WINDOWS_DRIVE.match(value):
        raise LineSpanEvalError(f"{field} must be relative")
    parsed = PurePosixPath(value)
    if any(part in {".", ".."} for part in parsed.parts):
        raise LineSpanEvalError(f"{field} contains a traversal component")
    if str(parsed) != value or not parsed.parts:
        raise LineSpanEvalError(f"{field} is not a canonical POSIX path")
    return value


def _line(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LineSpanEvalError(f"{field} must be an integer")
    if value < 1 or value > MAX_LINE:
        raise LineSpanEvalError(f"{field} must be between 1 and {MAX_LINE}")
    return value


def _span(item: dict[str, Any], *, field: str, required: bool) -> tuple[int, int] | None:
    has_start = "start_line" in item
    has_end = "end_line" in item
    if not has_start and not has_end:
        if required:
            raise LineSpanEvalError(f"{field} requires start_line and end_line")
        return None
    if not has_start or not has_end:
        raise LineSpanEvalError(f"{field} requires both start_line and end_line")
    start = _line(item["start_line"], field=f"{field}.start_line")
    end = _line(item["end_line"], field=f"{field}.end_line")
    if end < start:
        raise LineSpanEvalError(f"{field} has a reversed span")
    return start, end


def _qrels(value: object) -> list[tuple[str, int, int]]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise LineSpanEvalError(f"qrels must be a list of at most {MAX_ITEMS} items")
    normalized: list[tuple[str, int, int]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise LineSpanEvalError(f"qrels[{index}] must be an object")
        path = _path(raw.get("path"), field=f"qrels[{index}].path")
        span = _span(raw, field=f"qrels[{index}]", required=True)
        assert span is not None
        normalized.append((path, span[0], span[1]))
    if len(set(normalized)) != len(normalized):
        raise LineSpanEvalError("qrels must not contain duplicate path/span pairs")
    return sorted(normalized)


def _ranked(value: object) -> list[tuple[str, tuple[int, int] | None]]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise LineSpanEvalError(f"ranked must be a list of at most {MAX_ITEMS} items")
    normalized: list[tuple[str, tuple[int, int] | None]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise LineSpanEvalError(f"ranked[{index}] must be an object")
        path = _path(raw.get("path"), field=f"ranked[{index}].path")
        normalized.append((path, _span(raw, field=f"ranked[{index}]", required=False)))
    return normalized


def _k(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LineSpanEvalError("k must be an integer")
    if value < 1 or value > MAX_K:
        raise LineSpanEvalError(f"k must be between 1 and {MAX_K}")
    return value


def _overlap(expected: tuple[int, int], candidate: tuple[int, int]) -> int:
    return max(0, min(expected[1], candidate[1]) - max(expected[0], candidate[0]) + 1)


def _round(value: float) -> float:
    return round(value, 6)


def evaluate(
    qrels: object,
    ranked: object,
    *,
    k: object = 5,
) -> dict[str, Any]:
    """Evaluate ranked observations against path/span qrels.

    ``file_recall_at_k`` uses unique expected paths as its denominator.
    Exact and overlap span recall use qrel spans as their denominator.  An
    inclusive overlap is positive when ``min(end) - max(start) + 1 > 0``.
    ``rank_weighted_span_coverage`` is the macro-average over qrels of the
    best ``overlap_fraction / rank`` among the first K candidates, where
    ``overlap_fraction`` is intersection lines divided by expected-span
    length.  Every metric returns ``0.0`` when its denominator is zero.

    Ranked candidates may omit both line fields.  Such a candidate can satisfy
    file recall but cannot satisfy exact/overlap span recall or coverage.
    """

    bounded_k = _k(k)
    expected = _qrels(qrels)
    candidates = _ranked(ranked)
    top = candidates[:bounded_k]

    expected_paths = sorted({path for path, _start, _end in expected})
    file_hits = [path for path in expected_paths if any(candidate_path == path for candidate_path, _ in top)]

    details: list[dict[str, Any]] = []
    exact_hits = 0
    overlap_hits = 0
    coverage_total = 0.0
    for path, start, end in expected:
        expected_span = (start, end)
        exact_rank: int | None = None
        overlap_rank: int | None = None
        best_weighted = 0.0
        best_overlap = 0.0
        best_rank: int | None = None
        for rank, (candidate_path, candidate_span) in enumerate(top, start=1):
            if candidate_path != path or candidate_span is None:
                continue
            if candidate_span == expected_span and exact_rank is None:
                exact_rank = rank
            overlap_lines = _overlap(expected_span, candidate_span)
            if overlap_lines <= 0:
                continue
            if overlap_rank is None:
                overlap_rank = rank
            overlap_fraction = overlap_lines / (end - start + 1)
            weighted = overlap_fraction / rank
            if weighted > best_weighted:
                best_weighted = weighted
                best_overlap = overlap_fraction
                best_rank = rank
        exact_hit = exact_rank is not None
        overlap_hit = overlap_rank is not None
        exact_hits += int(exact_hit)
        overlap_hits += int(overlap_hit)
        coverage_total += best_weighted
        details.append(
            {
                "path": path,
                "start_line": start,
                "end_line": end,
                "file_hit": path in file_hits,
                "exact_span_hit": exact_hit,
                "exact_rank": exact_rank,
                "overlap_span_hit": overlap_hit,
                "overlap_rank": overlap_rank,
                "best_overlap_fraction": _round(best_overlap),
                "best_rank": best_rank,
                "rank_weighted_coverage": _round(best_weighted),
            }
        )

    qrel_count = len(expected)
    file_denominator = len(expected_paths)
    return {
        "metric_version": "line-span-v1",
        "evaluation_mode": "pure_contract",
        "k": bounded_k,
        "qrel_count": qrel_count,
        "expected_file_count": file_denominator,
        "file_recall_at_k": _round(len(file_hits) / file_denominator) if file_denominator else 0.0,
        "exact_span_recall_at_k": _round(exact_hits / qrel_count) if qrel_count else 0.0,
        "overlap_span_recall_at_k": _round(overlap_hits / qrel_count) if qrel_count else 0.0,
        "rank_weighted_span_coverage": _round(coverage_total / qrel_count) if qrel_count else 0.0,
        "top_k": [
            {
                "rank": rank,
                "path": path,
                **(
                    {"start_line": span[0], "end_line": span[1]}
                    if span is not None
                    else {}
                ),
            }
            for rank, (path, span) in enumerate(top, start=1)
        ],
        "qrels": details,
    }


__all__ = ["LineSpanEvalError", "MAX_K", "MAX_ITEMS", "MAX_LINE", "evaluate"]
