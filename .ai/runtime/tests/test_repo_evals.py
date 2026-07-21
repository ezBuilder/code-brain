from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / ".ai" / "evals" / "run.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("code_brain_repo_evals", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_precall_routing_axis_exercises_production_logic() -> None:
    runner = _load_runner()
    report = runner.run_axis("precall_routing", wired=True)
    assert report["supported"] is True
    assert report["measured"] == report["cases"] == 5
    assert report["passed"] == 5
    assert report["failed"] == []
    assert report["skipped"] == []
    assert report["pass_rate"] == 1.0


def test_context_budget_axis_measures_caps_and_protected_signals() -> None:
    runner = _load_runner()
    report = runner.run_axis("context_budget", wired=True)
    assert report["measured"] == report["cases"] == 5
    assert report["passed"] == 5
    assert report["failed"] == []


def test_tool_discovery_axis_measures_bounded_recall() -> None:
    runner = _load_runner()
    report = runner.run_axis("tool_discovery", wired=True)
    assert report["measured"] == report["cases"] == 8
    assert report["passed"] == 8
    assert report["failed"] == []


def test_autoresearch_retrieval_axis_measures_ranking_quality() -> None:
    runner = _load_runner()
    report = runner.run_axis("autoresearch_retrieval", wired=True)
    assert report["measured"] == report["cases"] == 3
    assert report["passed"] == 3
    assert report["failed"] == []


def test_code_retrieval_axis_measures_production_search_quality_and_latency() -> None:
    runner = _load_runner()
    report = runner.run_axis("code_retrieval", wired=True)
    assert report["measured"] == report["cases"] == 3
    assert report["passed"] == 3
    assert report["failed"] == []
    baseline = report["case_results"][0]["observed"]
    assert len(baseline["corpus_sha256"]) == 64
    assert baseline["latency_ms"]["p95"] >= 0.0
    assert baseline["retrieval_policy_counts"]


def test_code_navigation_axis_measures_alias_definition_and_trace_quality() -> None:
    runner = _load_runner()
    report = runner.run_axis("code_navigation", wired=True)
    assert report["measured"] == report["cases"] == 4
    assert report["passed"] == 4
    assert report["failed"] == []
    baseline = report["case_results"][0]["observed"]
    assert baseline["recall_at_k"] == 1.0
    assert baseline["mrr"] == 1.0
    assert baseline["ndcg_at_k"] == 1.0
    assert baseline["backend_counts"] == {"syntactic_codegraph": 3}


def test_memory_retrieval_axis_measures_identifier_temporal_and_procedural_recall() -> None:
    runner = _load_runner()
    report = runner.run_axis("memory_retrieval", wired=True)
    assert report["measured"] == report["cases"] == 3
    assert report["passed"] == 3
    assert report["failed"] == []
    baseline = report["case_results"][0]["observed"]
    assert len(baseline["memory_sha256"]) == 64
    assert baseline["latency_ms"]["p95"] >= 0.0
    assert baseline["query_diagnostics"]


def test_unsupported_axis_is_explicitly_skipped_not_passed() -> None:
    runner = _load_runner()
    report = runner.run_axis("decision_logging", wired=True)
    assert report["supported"] is False
    assert report["measured"] == 0
    assert report["passed"] == 0
    assert len(report["skipped"]) == report["cases"] == 4
    assert {item["reason"] for item in report["case_results"]} == {"axis_adapter_unsupported"}


def test_cli_is_a_strict_complete_gate_for_supported_axes() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--axis",
            "precall_routing",
            "--axis",
            "context_budget",
            "--axis",
            "tool_discovery",
            "--axis",
            "autoresearch_retrieval",
            "--axis",
            "code_retrieval",
            "--axis",
            "code_navigation",
            "--axis",
            "memory_retrieval",
            "--wired",
            "--strict",
            "--require-complete",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["summary"] == {
        "axes": 7,
        "cases": 31,
        "measured": 31,
        "passed": 31,
        "failed": 0,
        "skipped": 0,
    }


def test_cli_require_complete_rejects_unsupported_axis() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--axis", "decision_logging", "--wired", "--require-complete"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 2
    assert "4 skipped" in completed.stdout