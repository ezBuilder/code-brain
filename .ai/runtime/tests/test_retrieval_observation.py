from __future__ import annotations

import json
import time
from pathlib import Path

from ai_core import obs
from ai_core.retrieval_observation import (
    SCHEMA_VERSION,
    SEMANTIC_CONVENTION,
    build,
    query_descriptor,
    runtime_summary,
)


def test_query_descriptor_is_stable_and_never_contains_raw_query() -> None:
    secret_query = "customerToken=super-secret processGroup cleanup"
    first = query_descriptor(secret_query)
    second = query_descriptor(secret_query)

    assert first == second
    assert first["raw_included"] is False
    assert first["characters"] == len(secret_query)
    assert first["bytes"] == len(secret_query.encode("utf-8"))
    assert first["tokens"] >= 4
    assert len(first["sha256"]) == 64
    assert secret_query not in json.dumps(first, ensure_ascii=False)
    assert "super-secret" not in json.dumps(first, ensure_ascii=False)


def test_build_is_bounded_and_reports_partial_fallback_quality() -> None:
    started_ns = time.perf_counter_ns()
    observation = build(
        operation="code.search",
        query="NeedleSymbol",
        started_ns=started_ns,
        returned=2,
        candidates=40,
        partial=True,
        policy="bm25+dense-shortlist+rg",
        fallback=["dense-shortlist", "ripgrep"],
        sources={f"source-{index}": index for index in range(80)},
        limits={"candidate_limit": 40, "max_ms": 500},
        quality={"coverage": 1.0, "bad": float("nan"), "note": "x" * 1000},
    )

    assert observation["schema_version"] == SCHEMA_VERSION
    assert observation["semantic_convention"] == SEMANTIC_CONVENTION
    assert observation["gen_ai.operation.name"] == "retrieval"
    assert observation["outcome"] == "partial"
    assert observation["duration_ms"] >= 0.0
    assert observation["results"] == {"returned": 2, "candidates": 40, "partial": True}
    assert observation["fallback"] == ["dense-shortlist", "ripgrep"]
    assert len(observation["sources"]) == 32
    assert observation["quality"]["bad"] is None
    assert len(observation["quality"]["note"]) == 192
    assert observation["bounded"] is True


def test_runtime_summary_exposes_all_bounded_retrieval_operations(tmp_path: Path) -> None:
    summary = runtime_summary(tmp_path)

    assert summary["ok"] is True
    assert summary["bounded"] is True
    assert set(summary["operations"]) == {
        "code.search",
        "memory.recall",
        "context.compress",
    }
    assert all(item["bounded"] is True for item in summary["operations"].values())
    assert len(summary["root_fingerprint"]) == 64


def test_metrics_and_health_surface_retrieval_runtime_additively(tmp_path: Path) -> None:
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")

    metrics = obs.metrics(tmp_path, include_usage=False)
    health = obs.health_summary(tmp_path)

    assert metrics["retrieval"]["schema_version"] == SCHEMA_VERSION
    assert metrics["retrieval"]["ok"] is True
    assert health["retrieval"]["schema_version"] == SCHEMA_VERSION
    assert health["retrieval"]["bounded"] is True
