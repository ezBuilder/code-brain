"""Multi-agent survey gate tests (Stage 4 §7.1) — deterministic fan-out policy."""
from __future__ import annotations

from ai_core.autoresearch import orchestration as orch


def test_default_single_when_not_independent():
    r = orch.survey_plan(["a", "b", "c", "d"], independent=False)
    assert r["mode"] == "single" and r["workers"] == []
    assert r["cost_warning"]


def test_too_few_subtopics_stays_single():
    r = orch.survey_plan(["a", "b"], independent=True)
    assert r["mode"] == "single"
    assert str(orch.MIN_FANOUT) in r["reason"]


def test_independent_breadth_first_goes_multi():
    r = orch.survey_plan(["x", "y", "z"], independent=True)
    assert r["mode"] == "multi"
    assert r["workers"] == ["x", "y", "z"]
    assert r["n_subtopics"] == 3 and r["deferred"] == []


def test_workers_capped_and_excess_deferred():
    topics = [f"t{i}" for i in range(12)]
    r = orch.survey_plan(topics, independent=True, max_workers=4)
    assert len(r["workers"]) == 4
    assert len(r["deferred"]) == 8
    assert r["workers"] + r["deferred"] == topics


def test_max_workers_hard_capped():
    topics = [f"t{i}" for i in range(20)]
    r = orch.survey_plan(topics, independent=True, max_workers=999)
    assert len(r["workers"]) == orch.HARD_MAX_WORKERS


def test_blank_and_nonstring_subtopics_filtered():
    r = orch.survey_plan(["a", "  ", "", 5, None, "b", "c"], independent=True)
    assert r["mode"] == "multi"
    assert r["workers"] == ["a", "b", "c"]


def test_bad_max_workers_falls_back():
    r = orch.survey_plan(["a", "b", "c"], independent=True, max_workers="oops")
    assert r["mode"] == "multi"
    assert len(r["workers"]) == 3  # falls back to default cap, only 3 topics present


def test_non_list_subtopics_safe():
    r = orch.survey_plan("not a list", independent=True)
    assert r["mode"] == "single" and r["n_subtopics"] == 0


def test_survey_plan_mcp_dispatch(tmp_path):
    from ai_core import mcp_server
    out = mcp_server._dispatch_tool(tmp_path, "autoresearch_survey_plan",
                                    {"subtopics": ["p", "q", "r"], "independent": True})
    assert out["mode"] == "multi" and len(out["workers"]) == 3
    assert "autoresearch_survey_plan" in mcp_server.TOOL_NAMES
