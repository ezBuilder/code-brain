from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
EVALS = ROOT / ".ai" / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

from line_span_eval import LineSpanEvalError, evaluate  # noqa: E402


def _load_runner():
    runner_path = EVALS / "run.py"
    spec = importlib.util.spec_from_file_location("line_span_repo_evals", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_line_span_axis_is_wired_to_production_context_pack() -> None:
    report = _load_runner().run_axis("line_span_retrieval", wired=True)
    assert report["supported"] is True
    assert report["measured"] == report["cases"] == 3
    assert report["passed"] == 3
    assert report["failed"] == []
    assert report["skipped"] == []


def test_span_metrics_are_deterministic_and_file_hit_does_not_imply_span_hit() -> None:
    qrels = [{"path": "src/a.py", "start_line": 2, "end_line": 4}]
    ranked = [{"path": "src/a.py"}, {"path": "src/a.py", "start_line": 4, "end_line": 6}]

    first = evaluate(qrels, ranked, k=2)
    second = evaluate(list(reversed(qrels)), ranked, k=2)

    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    assert first["file_recall_at_k"] == 1.0
    assert first["exact_span_recall_at_k"] == 0.0
    assert first["overlap_span_recall_at_k"] == 1.0
    assert first["rank_weighted_span_coverage"] == round((1 / 3) / 2, 6)
    assert first["qrels"][0]["file_hit"] is True
    assert first["qrels"][0]["exact_span_hit"] is False


@pytest.mark.parametrize(
    "qrels, ranked",
    [
        ([{"path": "../escape.py", "start_line": 1, "end_line": 1}], []),
        ([{"path": "/absolute.py", "start_line": 1, "end_line": 1}], []),
        ([{"path": "src/a.py", "start_line": 0, "end_line": 1}], []),
        ([{"path": "src/a.py", "start_line": 4, "end_line": 2}], []),
        ([{"path": "src/a.py", "start_line": 1, "end_line": 1_000_001}], []),
        ([{"path": "src/a.py", "start_line": 1}], []),
        ([{"path": "src/a.py", "start_line": 1, "end_line": 1}], [{"path": "src/a.py", "start_line": 2}]),
        ([{"path": "src/a.py", "start_line": 1, "end_line": 1}], [{"path": "../escape.py"}]),
    ],
)
def test_malformed_paths_and_spans_are_rejected(qrels, ranked) -> None:
    with pytest.raises(LineSpanEvalError):
        evaluate(qrels, ranked, k=1)


def test_zero_denominators_are_explicitly_zero() -> None:
    report = evaluate([], [{"path": "src/a.py", "start_line": 1, "end_line": 1}], k=1)

    assert report["qrel_count"] == 0
    assert report["expected_file_count"] == 0
    assert report["file_recall_at_k"] == 0.0
    assert report["exact_span_recall_at_k"] == 0.0
    assert report["overlap_span_recall_at_k"] == 0.0
    assert report["rank_weighted_span_coverage"] == 0.0
