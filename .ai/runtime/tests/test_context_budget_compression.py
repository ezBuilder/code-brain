from __future__ import annotations

from ai_core.context_budget import apply


def _compression_candidates() -> list[dict]:
    return [
        {
            "path": "src/process_a.py",
            "snippet": "bounded process group cleanup terminates descendants",
            "provenance": {"processor": "bm25", "confidence": 0.9},
        },
        {
            "path": "src/process_b.py",
            "snippet": "bounded process_group cleanup terminates child descendants",
            "provenance": {"processor": "dense", "confidence": 0.8},
        },
        {
            "path": "src/database.py",
            "snippet": "database rollback transaction lock",
        },
        {
            "path": "tests/failure.md",
            "snippet": "failure: process group cleanup missed grandchildren",
        },
    ]


def test_query_aware_compression_prunes_redundancy_and_preserves_negative_evidence() -> None:
    result = apply(
        _compression_candidates(),
        mode="balanced",
        limit=4,
        query="processGroup cleanup descendants",
    )

    budget = result["context_budget"]
    paths = [item["path"] for item in result["results"]]
    assert budget["schema_version"] == 3
    assert budget["query_aware"] is True
    assert budget["coverage_ratio"] == 1.0
    assert budget["redundancy_pruned"] == 1
    assert budget["selected_results"] == 3
    assert "src/process_a.py" in paths
    assert "src/process_b.py" not in paths
    assert "tests/failure.md" in paths
    assert budget["negative_evidence_available"] == 1
    assert budget["negative_evidence_selected"] == 1
    assert budget["negative_evidence_dropped"] == 0
    assert budget["provenance_available"] == 2
    assert budget["provenance_selected"] == 1
    assert budget["saved_bytes"] > 0
    assert budget["savings_ratio"] > 0.0
    observation = result["retrieval_observation"]
    assert observation["operation"] == "context.compress"
    assert observation["query"]["raw_included"] is False
    assert observation["quality"]["redundancy_pruned"] == 1
    assert observation["quality"]["negative_evidence_dropped"] == 0
    assert budget["coverage_preservation_ratio"] == 1.0
    assert budget["query_terms_lost"] == []
    assert budget["saved_lexical_tokens"] > 0


def test_query_aware_compression_is_order_invariant() -> None:
    candidates = _compression_candidates()
    forward = apply(candidates, mode="balanced", limit=4, query="processGroup cleanup")
    reverse = apply(list(reversed(candidates)), mode="balanced", limit=4, query="processGroup cleanup")
    assert forward["additionalContext"] == reverse["additionalContext"]
    assert forward["context_budget"] == reverse["context_budget"]


def test_protected_evidence_survives_even_when_it_exceeds_byte_budget() -> None:
    result = apply(
        [
            {"path": "docs/handoff.md", "snippet": "handoff: " + "h" * 700},
            {"path": "src/noise.py", "snippet": "ordinary " + "n" * 400},
        ],
        mode="aggressive",
        limit=2,
        base_max_bytes=512,
        query="resume work",
    )

    budget = result["context_budget"]
    assert [item["path"] for item in result["results"]] == ["docs/handoff.md"]
    assert budget["protected_available"] == 1
    assert budget["protected_selected"] == 1
    assert budget["protected_dropped"] == 0
    assert budget["byte_pruned"] == 1
    assert budget["over_budget_to_preserve"] is True
    assert budget["bytes"] > budget["max_bytes"]


def test_exact_duplicate_prefers_deterministic_path() -> None:
    result = apply(
        [
            {"path": "src/z.py", "snippet": "same repeated context body"},
            {"path": "src/a.py", "snippet": "same repeated context body"},
        ],
        mode="balanced",
        limit=2,
        query="repeated context",
    )
    assert [item["path"] for item in result["results"]] == ["src/a.py"]
    assert result["context_budget"]["redundancy_pruned"] == 1


def test_opposite_evidence_polarities_are_not_deduplicated() -> None:
    result = apply(
        [
            {
                "path": "docs/success.md",
                "snippet": "process group cleanup terminates descendants",
                "status": "confirmed",
            },
            {
                "path": "docs/failure.md",
                "snippet": "process group cleanup terminates descendants",
                "status": "failed",
            },
        ],
        mode="aggressive",
        limit=2,
        query="process group cleanup descendants",
    )

    assert {item["path"] for item in result["results"]} == {
        "docs/success.md",
        "docs/failure.md",
    }
    assert result["context_budget"]["redundancy_pruned"] == 0
    assert result["context_budget"]["negative_evidence_selected"] == 1


def test_protected_and_source_provenance_are_recovered_beyond_requested_limit() -> None:
    result = apply(
        [
            {"path": "src/a.py", "snippet": "ordinary alpha"},
            {"path": "src/b.py", "snippet": "ordinary beta"},
            {"path": "src/c.py", "snippet": "ordinary gamma"},
            {"path": "docs/handoff.md", "snippet": "handoff: retain release blocker"},
            {
                "path": "evidence/report.md",
                "snippet": "measured release evidence",
                "provenance": {"source_id": "report-17", "processor": "memory"},
            },
        ],
        mode="balanced",
        limit=2,
        query="release blocker evidence",
    )

    paths = {item["path"] for item in result["results"]}
    budget = result["context_budget"]
    assert "docs/handoff.md" in paths
    assert "evidence/report.md" in paths
    assert budget["requested_limit"] == 2
    assert budget["candidate_limit"] == 8
    assert budget["considered_results"] == 5
    assert budget["protected_dropped"] == 0
    assert budget["protected_provenance_available"] == 1
    assert budget["protected_provenance_selected"] == 1
    assert budget["protected_provenance_dropped"] == 0


def test_marginal_query_selection_preserves_available_coverage() -> None:
    result = apply(
        [
            {"path": "src/a.py", "snippet": "alpha beta common implementation"},
            {"path": "src/b.py", "snippet": "alpha beta common implementation variant"},
            {"path": "src/c.py", "snippet": "gamma isolated behavior"},
            {"path": "src/d.py", "snippet": "delta isolated behavior"},
            {"path": "src/noise.py", "snippet": "unrelated noise"},
        ],
        mode="aggressive",
        limit=5,
        query="alpha beta gamma delta",
    )

    paths = {item["path"] for item in result["results"]}
    budget = result["context_budget"]
    assert {"src/a.py", "src/c.py", "src/d.py"}.issubset(paths)
    assert budget["ordinary_result_cap"] == 3
    assert budget["query_terms_available"] == 4
    assert budget["query_terms_covered"] == 4
    assert budget["input_coverage_ratio"] == 1.0
    assert budget["coverage_preservation_ratio"] == 1.0
    assert budget["query_terms_lost"] == []


def test_candidate_cap_is_explicit_and_still_prioritizes_protected_tail() -> None:
    results = [
        {"path": f"src/noise-{idx:03d}.py", "snippet": f"ordinary noise {idx}"}
        for idx in range(20)
    ]
    results.append({"path": "docs/verdict.md", "snippet": "verdict: protected tail evidence"})

    result = apply(results, mode="balanced", limit=1, query="tail evidence")
    budget = result["context_budget"]
    assert "docs/verdict.md" in {item["path"] for item in result["results"]}
    assert budget["candidate_limit"] == 4
    assert budget["candidate_cap_pruned"] == 17
    assert budget["protected_selected"] == 1
    assert budget["truncated"] is True
