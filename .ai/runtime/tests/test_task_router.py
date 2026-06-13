"""Axis-1 capability classifier: deterministic, first-match-wins, no overlap ambiguity."""
from __future__ import annotations

from ai_core import task_router as tr


def _req(goal: str = "", instruction: str = "", role: str = "worker", checklist=None) -> dict:
    return {"goal": goal, "instruction": instruction, "role": role, "checklist": checklist or []}


def test_every_request_lands_in_exactly_one_category() -> None:
    for goal in ("", "do the thing", "asdf qwerty", "随便"):
        cat = tr.classify(_req(goal=goal))
        assert cat in tr.CATEGORY_IDS


def test_reasoning_wins_over_debug_on_race_condition() -> None:
    # critics' overlap case: "implement a fix for the race condition" must resolve deterministically.
    cat = tr.classify(_req(goal="implement a fix for the race condition in the dispatcher"))
    assert cat == "reasoning_design"  # _COMPLEX(race condition) has priority over debug/feature
    assert tr.base_floor_tier(cat) == "best"
    assert tr.preferred_families(cat)[0] == "codex"


def test_plain_bugfix_is_debug_balanced() -> None:
    cat = tr.classify(_req(goal="fix the off-by-one error in pagination", instruction="test fails"))
    assert cat == "debug_fix"
    assert tr.base_floor_tier(cat) == "balanced"


def test_feature_impl_classified() -> None:
    cat = tr.classify(_req(goal="implement the export endpoint", checklist=["a", "b"]))
    assert cat == "feature_impl"
    assert "claude" in tr.preferred_families(cat)


def test_review_is_not_caught_by_implement() -> None:
    cat = tr.classify(_req(goal="review the diff for security issues", role="reviewer"))
    assert cat == "review_verify"


def test_review_with_fix_verb_falls_through_to_action() -> None:
    # "fix" present → it's not a pure review; must NOT be review_verify
    cat = tr.classify(_req(goal="review and fix the failing test"))
    assert cat != "review_verify"


def test_docs_is_cheap_agy() -> None:
    cat = tr.classify(_req(goal="document the routing module in the README"))
    assert cat == "docs_explain"
    assert tr.base_floor_tier(cat) == "cheap"
    assert tr.preferred_families(cat)[0] == "agy"


def test_trivial_edit_short_and_simple() -> None:
    cat = tr.classify(_req(goal="rename the variable foo to bar"))
    assert cat == "trivial_edit"
    assert tr.base_floor_tier(cat) == "cheap"


def test_trivial_guard_rejects_long_or_complex() -> None:
    long_text = "rename " + ("x" * 300)
    assert tr.classify(_req(goal=long_text)) != "trivial_edit"  # too long → not trivial


def test_research_search() -> None:
    cat = tr.classify(_req(goal="investigate where the lease expiry is handled in the codebase"))
    assert cat == "research_search"
    assert "agy" in tr.preferred_families(cat)


def test_fallthrough_is_standard_balanced() -> None:
    cat = tr.classify(_req(goal="handle the quarterly thing"))
    assert cat == "standard"
    assert tr.base_floor_tier(cat) == "balanced"


def test_classification_is_deterministic_repeatable() -> None:
    r = _req(goal="refactor the worker pool to extract the lease logic, no behavior change")
    assert tr.classify(r) == tr.classify(r) == "refactor"


def test_route_summary_shape() -> None:
    s = tr.route_summary(_req(goal="design the adaptive router"))
    assert s["category"] == "reasoning_design"
    assert s["base_floor_tier"] == "best"
    assert isinstance(s["preferred_families"], list) and s["preferred_families"]
