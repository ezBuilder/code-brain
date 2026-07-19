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
    assert report["measured"] == report["cases"] == 3
    assert report["passed"] == 3
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
        "axes": 4,
        "cases": 19,
        "measured": 19,
        "passed": 19,
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