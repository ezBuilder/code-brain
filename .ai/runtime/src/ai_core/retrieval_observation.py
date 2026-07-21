"""Privacy-safe, bounded observations for retrieval and context operations.

The shape mirrors the operational concepts used by OpenTelemetry GenAI/agent
semantic conventions (operation, duration, outcome, bounded attributes) without
claiming wire-level compatibility with any specific exporter.
"""
from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path
from typing import Any

from .memory_match import tokenize

SCHEMA_VERSION = 1
SEMANTIC_CONVENTION = "codebrain.retrieval.v1"
_MAX_MAPPING_ITEMS = 32
_MAX_SEQUENCE_ITEMS = 16
_MAX_STRING_CHARS = 192


def start() -> int:
    return time.perf_counter_ns()


def duration_ms(started_ns: int) -> float:
    try:
        elapsed = max(0, time.perf_counter_ns() - int(started_ns))
    except (TypeError, ValueError, OverflowError):
        elapsed = 0
    return round(elapsed / 1_000_000, 3)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _finite_number(value: object) -> float | int | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return round(value, 6)
    return None


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return _finite_number(value)
    if isinstance(value, str):
        return value[:_MAX_STRING_CHARS]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in list(value.items())[:_MAX_MAPPING_ITEMS]:
            out[str(key)[:64]] = _bounded(child, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_bounded(child, depth=depth + 1) for child in list(value)[:_MAX_SEQUENCE_ITEMS]]
    return str(value)[:_MAX_STRING_CHARS]


def query_descriptor(query: object) -> dict[str, Any]:
    text = str(query or "")
    encoded = text.encode("utf-8", errors="replace")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "characters": len(text),
        "bytes": len(encoded),
        "tokens": len(tokenize(text)),
        "raw_included": False,
    }


def build(
    *,
    operation: str,
    query: object,
    started_ns: int,
    returned: int,
    candidates: int,
    partial: bool = False,
    policy: str | None = None,
    fallback: str | list[str] | None = None,
    sources: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    elapsed = duration_ms(started_ns)
    outcome = "error" if error else ("partial" if partial else "ok")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "semantic_convention": SEMANTIC_CONVENTION,
        "gen_ai.operation.name": "retrieval",
        "operation": str(operation)[:64],
        "outcome": outcome,
        "duration_ms": elapsed,
        "query": query_descriptor(query),
        "results": {
            "returned": _nonnegative_int(returned),
            "candidates": _nonnegative_int(candidates),
            "partial": bool(partial),
        },
        "policy": str(policy)[:96] if policy else None,
        "fallback": _bounded(fallback),
        "sources": _bounded(sources or {}),
        "limits": _bounded(limits or {}),
        "quality": _bounded(quality or {}),
        "error": str(error)[:160] if error else None,
        "bounded": True,
    }
    return payload


def runtime_summary(root: Path) -> dict[str, Any]:
    """Return fail-soft configured bounds for all production retrieval paths."""
    operations: dict[str, Any] = {}
    errors: list[str] = []

    try:
        from .dense_retrieval import policy as dense_policy

        dense = dense_policy()
        operations["code.search"] = {
            "bounded": all(_nonnegative_int(dense.get(key)) > 0 for key in ("max_rows", "max_ms", "max_candidates")),
            "dense": dense,
            "fallbacks": ["bm25-shortlist", "ripgrep"],
        }
    except Exception as exc:
        errors.append(f"code.search:{type(exc).__name__}")
        operations["code.search"] = {"bounded": False}

    try:
        from .memory_recall import policy as memory_policy

        memory = memory_policy()
        operations["memory.recall"] = {
            "bounded": all(
                float(memory.get(key) or 0) > 0
                for key in ("max_records_per_store", "max_bytes_per_store", "max_candidates")
            ),
            "limits": memory,
        }
    except Exception as exc:
        errors.append(f"memory.recall:{type(exc).__name__}")
        operations["memory.recall"] = {"bounded": False}

    try:
        from .context_budget import MODES, policy as context_policy

        modes = {mode: context_policy(mode) for mode in MODES}
        operations["context.compress"] = {
            "bounded": all(_nonnegative_int(item.get("max_bytes")) > 0 for item in modes.values()),
            "modes": modes,
        }
    except Exception as exc:
        errors.append(f"context.compress:{type(exc).__name__}")
        operations["context.compress"] = {"bounded": False}

    return {
        "schema_version": SCHEMA_VERSION,
        "semantic_convention": SEMANTIC_CONVENTION,
        "ok": not errors and all(bool(item.get("bounded")) for item in operations.values()),
        "bounded": all(bool(item.get("bounded")) for item in operations.values()),
        "operations": operations,
        "errors": errors[:8],
        "root_fingerprint": hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest(),
    }


__all__ = [
    "SCHEMA_VERSION",
    "SEMANTIC_CONVENTION",
    "build",
    "duration_ms",
    "query_descriptor",
    "runtime_summary",
    "start",
]
