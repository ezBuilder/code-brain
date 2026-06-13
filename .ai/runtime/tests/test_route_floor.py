"""Adaptive per-category model-tier floor: asymmetric, corroborated, deterministic, bounded."""
from __future__ import annotations

from pathlib import Path

from ai_core import route_floor as rf
from ai_core import task_router as tr


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "runtime" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _win(root: Path, cat: str, tier: str) -> dict:
    return rf.record_outcome(root, category=cat, tier=tier, status="done", attempts=1,
                             reviewer_required=True, verdict_pass=True)


def _loss(root: Path, cat: str, tier: str, reason: str = "reviewer rejected: wrong output") -> dict:
    return rf.record_outcome(root, category=cat, tier=tier, status="dead", attempts=1,
                             reviewer_required=True, verdict_pass=False, reason=reason)


def test_cold_start_is_base_prior(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    assert rf.effective_floor_tier(root, "feature_impl") == "balanced"
    assert rf.effective_floor_tier(root, "trivial_edit") == "cheap"
    assert rf.effective_floor_tier(root, "reasoning_design") == "best"


def test_classify_outcome_anti_gaming_unverified_is_neutral(tmp_path: Path) -> None:
    # self-reported done with NO reviewer pass must not count as a win (can't drive cost down)
    assert rf.classify_outcome(status="done", attempts=1, reviewer_required=False, verdict_pass=False) == "neutral"
    assert rf.classify_outcome(status="done", attempts=1, reviewer_required=True, verdict_pass=True) == "win"


def test_classify_outcome_retry_success_is_neutral() -> None:
    assert rf.classify_outcome(status="done", attempts=3, reviewer_required=True, verdict_pass=True) == "neutral"


def test_classify_outcome_env_fault_is_not_loss() -> None:
    assert rf.classify_outcome(status="dead", attempts=1, reviewer_required=True, verdict_pass=False,
                               reason="network timeout, connection refused") == "neutral"
    assert rf.classify_outcome(status="dead", attempts=1, reviewer_required=True, verdict_pass=False,
                               reason="reviewer says logic is wrong") == "loss"


def test_escalates_after_two_attributed_losses(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    _loss(root, "feature_impl", "balanced")
    r = _loss(root, "feature_impl", "balanced")
    assert r["moved"] is True
    assert rf.effective_floor_tier(root, "feature_impl") == "best"  # balanced→best


def test_env_faults_never_escalate(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(5):
        rf.record_outcome(root, category="feature_impl", tier="balanced", status="dead", attempts=1,
                          reviewer_required=True, verdict_pass=False, reason="sandbox denied / tmux pane gone")
    assert rf.effective_floor_tier(root, "feature_impl") == "balanced"  # unchanged


def test_deescalates_after_sustained_corroborated_wins(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    # feature_impl cold floor = balanced; enough clean corroborated wins → drop to cheap
    moved = False
    for _ in range(rf.DOWN_WINS + rf.COOLDOWN_EVENTS + 2):
        r = _win(root, "feature_impl", "balanced")
        moved = moved or r["moved"]
    assert moved is True
    assert rf.effective_floor_tier(root, "feature_impl") == "cheap"


def test_unverified_wins_never_deescalate(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(20):  # many self-reported completes, none corroborated
        rf.record_outcome(root, category="feature_impl", tier="balanced", status="done", attempts=1,
                          reviewer_required=False, verdict_pass=False)
    assert rf.effective_floor_tier(root, "feature_impl") == "balanced"  # held at prior


def test_floor_is_clamped_to_best(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(10):
        _loss(root, "reasoning_design", "best")  # already at best; cannot exceed
    assert rf.effective_floor_tier(root, "reasoning_design") == "best"


def test_floor_is_clamped_to_cheap(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(40):  # trivial cold=cheap; sustained wins cannot go below cheap
        _win(root, "trivial_edit", "cheap")
    assert rf.effective_floor_tier(root, "trivial_edit") == "cheap"


def test_record_outcome_is_failsoft_on_unknown_category(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    r = rf.record_outcome(root, category="does_not_exist", tier="balanced", status="done", attempts=1,
                          reviewer_required=True, verdict_pass=True)
    assert r["ok"] is True and r["category"] == "standard"  # folded to fallthrough, no raise


def test_deescalation_then_escalation_path(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(rf.DOWN_WINS + rf.COOLDOWN_EVENTS + 2):
        _win(root, "feature_impl", "balanced")
    assert rf.effective_floor_tier(root, "feature_impl") == "cheap"
    # now cheap proves inadequate → two attributed losses escalate back up
    _loss(root, "feature_impl", "cheap")
    _loss(root, "feature_impl", "cheap")
    assert rf.effective_floor_tier(root, "feature_impl") == "balanced"


def test_status_reports_all_categories(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    s = rf.status(root)
    assert s["ok"] and set(s["categories"]) == set(tr.CATEGORY_IDS)
