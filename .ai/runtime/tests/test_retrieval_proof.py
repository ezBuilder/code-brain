from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.retrieval_proof import prove_retrieval  # noqa: E402
from ai_core.search import rebuild  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".ai" / "cache").mkdir(parents=True)
    (root / ".ai" / "config.yaml").write_text(
        "project_name: retrieval-proof\nsearch:\n  retriever: bm25\n",
        encoding="utf-8",
    )
    (root / "src" / "service.py").write_text(
        "def alpha():\n"
        "    helper()\n"
        "    return 1\n\n"
        "def helper():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    (root / "src" / "consumer.py").write_text(
        "from service import alpha\n\n"
        "def gamma():\n"
        "    return alpha()\n",
        encoding="utf-8",
    )
    assert rebuild(root)["ok"] is True
    return root


def test_proof_auto_selects_graph_query_and_proves_deterministic_no_growth(tmp_path: Path) -> None:
    payload = prove_retrieval(_repo(tmp_path), repeats=5)

    assert payload["ok"] is True
    assert payload["query_selection"] == "auto_graph_symbol"
    assert payload["effect"]["status"] == "unmeasured"
    assert payload["v2"]["graph_ranking_applied"] is True
    assert payload["v2"]["ranked_node_count"] > 0
    assert payload["v2"]["receipt_count"] == 1
    assert payload["durability"]["unchanged"] is True
    assert all(payload["checks"].values())


def test_proof_measures_expected_path_and_span_improvement(tmp_path: Path) -> None:
    payload = prove_retrieval(
        _repo(tmp_path),
        query="alpha",
        expected_path="src/service.py",
        start_line=5,
        end_line=6,
        repeats=3,
    )

    assert payload["ok"] is True
    assert payload["expected"] == {
        "path": "src/service.py",
        "start_line": 5,
        "end_line": 6,
    }
    assert payload["v2"]["path_rank"] is not None
    assert payload["v2"]["span_overlap"] >= payload["legacy"]["span_overlap"]
    assert payload["effect"]["status"] in {"improved", "parity"}


def test_proof_preserves_dot_prefixed_repository_paths(tmp_path: Path) -> None:
    payload = prove_retrieval(
        _repo(tmp_path),
        query="alpha",
        expected_path=".ai/not-indexed.py",
        repeats=1,
    )

    assert payload["expected"]["path"] == ".ai/not-indexed.py"
    assert payload["checks"]["expected_path_found"] is False
    assert payload["ok"] is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repeats": 0}, "repeats"),
        ({"repeats": 21}, "repeats"),
        ({"limit": 0}, "limit"),
        ({"expected_path": "../escape.py"}, "expected_path"),
        ({"expected_path": "src/service.py", "start_line": 2}, "together"),
        ({"start_line": 1, "end_line": 2}, "expected_path"),
    ],
)
def test_proof_rejects_unbounded_or_invalid_inputs(tmp_path: Path, kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        prove_retrieval(_repo(tmp_path), query="alpha", **kwargs)
