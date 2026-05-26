"""Retention scoring (decay + reinforcement) + lesson confidence/decay.

Covers memory_tier.retention_score / score_tier / retention_report and
lessons.lesson_fingerprint / score_lessons / recall_lessons. All pure-local,
deterministic, no network.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import memory_tier as mt  # noqa: E402
from ai_core import lessons as ls  # noqa: E402
from ai_core.memory import append_decision  # noqa: E402


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True)
    return tmp_path


def _write_lessons(root: Path, records: list[dict]) -> None:
    lp = ls.lessons_path(root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with lp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# --- retention_score ---------------------------------------------------------

def test_score_in_unit_interval_and_type_weighting():
    fresh_decision = mt.retention_score(mem_type="decision", age_days=0)
    fresh_fact = mt.retention_score(mem_type="fact", age_days=0)
    assert 0.0 <= fresh_fact <= fresh_decision <= 1.0
    # decision weight 0.9 vs fact 0.5 at age 0 (decay=1)
    assert fresh_decision == pytest.approx(0.9, abs=1e-9)
    assert fresh_fact == pytest.approx(0.5, abs=1e-9)


def test_unknown_type_uses_default_weight():
    assert mt.retention_score(mem_type="totally-unknown", age_days=0) == pytest.approx(0.5)


def test_temporal_decay_monotonic():
    young = mt.retention_score(mem_type="decision", age_days=1)
    old = mt.retention_score(mem_type="decision", age_days=365)
    assert young > old
    assert old == pytest.approx(0.9 * math.exp(-0.01 * 365), abs=1e-6)


def test_access_count_raises_salience_capped():
    base = mt.retention_score(mem_type="fact", age_days=0, access_count=0)
    some = mt.retention_score(mem_type="fact", age_days=0, access_count=5)
    lots = mt.retention_score(mem_type="fact", age_days=0, access_count=1000)
    assert some > base
    # bonus capped at 0.2 → 0.5 + 0.2 = 0.7
    assert lots == pytest.approx(0.7, abs=1e-9)


def test_confidence_overrides_salience():
    s = mt.retention_score(mem_type="fact", age_days=0, confidence=0.95)
    assert s == pytest.approx(0.95, abs=1e-9)
    # confidence below base does not lower it
    s2 = mt.retention_score(mem_type="decision", age_days=0, confidence=0.1)
    assert s2 == pytest.approx(0.9, abs=1e-9)


def test_reinforcement_boost_increases_and_clamps():
    no_boost = mt.retention_score(mem_type="fact", age_days=30)
    boosted = mt.retention_score(mem_type="fact", age_days=30, recent_access_days=[1, 1, 1])
    assert boosted > no_boost
    # heavy boost + high salience clamps to 1.0
    clamped = mt.retention_score(
        mem_type="decision", age_days=0, confidence=1.0, recent_access_days=[1, 1, 1, 1]
    )
    assert clamped == pytest.approx(1.0)


def test_env_overrides_lambda_and_sigma(monkeypatch):
    monkeypatch.setenv("AI_MEMORY_DECAY_LAMBDA", "0")  # no decay
    s = mt.retention_score(mem_type="fact", age_days=10_000)
    assert s == pytest.approx(0.5, abs=1e-9)
    monkeypatch.setenv("AI_MEMORY_REINFORCE_SIGMA", "1.0")
    boosted = mt.retention_score(mem_type="fact", age_days=0, recent_access_days=[1])
    assert boosted == pytest.approx(1.0)  # 0.5 + 1.0*1, clamped


# --- score_tier --------------------------------------------------------------

def test_score_tier_thresholds():
    assert mt.score_tier(0.9) == "hot"
    assert mt.score_tier(0.7) == "hot"
    assert mt.score_tier(0.5) == "warm"
    assert mt.score_tier(0.2) == "cold"
    assert mt.score_tier(0.05) == "evictable"


def test_score_tier_env_override(monkeypatch):
    monkeypatch.setenv("AI_MEMORY_TIER_HOT", "0.5")
    assert mt.score_tier(0.6) == "hot"


# --- retention_report --------------------------------------------------------

def test_retention_report_empty(tmp_root: Path):
    rep = mt.retention_report(tmp_root)
    assert rep["ok"] is True
    assert rep["scored"] == 0
    assert rep["tiers"] == {"hot": 0, "warm": 0, "cold": 0, "evictable": 0}
    assert rep["evict_candidates"] == []


def test_retention_report_scores_decisions_and_histogram(tmp_root: Path):
    append_decision(tmp_root, text="use sqlite", tags=["db"], source="test")
    append_decision(tmp_root, text="prefer uv", tags=["build"], source="test")
    rep = mt.retention_report(tmp_root)
    assert rep["scored"] == 2
    assert sum(rep["tiers"].values()) == rep["scored"]
    # fresh decisions score 0.9 → hot
    assert rep["tiers"]["hot"] == 2


def test_retention_report_evict_candidates_sorted_and_capped(tmp_root: Path):
    # Old, single-sighting lessons decay to evictable.
    _write_lessons(tmp_root, [
        {"id": f"lesson-{i}", "source": f"s{i}", "failure": f"f{i}",
         "cause": "c", "fix": "x", "created_at": _iso_days_ago(400)}
        for i in range(5)
    ])
    rep = mt.retention_report(tmp_root, evict_limit=3)
    assert rep["scored"] == 5
    assert len(rep["evict_candidates"]) == 3  # capped
    scores = [c["score"] for c in rep["evict_candidates"]]
    assert scores == sorted(scores)  # ascending (worst first)
    assert all(c["tier"] == "evictable" for c in rep["evict_candidates"])


def test_retention_report_includes_procedures(tmp_root: Path):
    from ai_core.procedural_memory import append_procedure
    append_procedure(tmp_root, kind="lesson", trigger="pytest_failure",
                     procedure="run uv sync first")
    rep = mt.retention_report(tmp_root)
    assert rep["scored"] >= 1
    assert sum(rep["tiers"].values()) == rep["scored"]


# --- lesson_fingerprint ------------------------------------------------------

def test_fingerprint_stable_and_distinct():
    a = ls.lesson_fingerprint({"source": "s", "failure": "boom", "cause": "c", "fix": "f"})
    b = ls.lesson_fingerprint({"source": "s", "failure": "boom", "cause": "c", "fix": "f"})
    c = ls.lesson_fingerprint({"source": "s", "failure": "other", "cause": "c", "fix": "f"})
    assert a == b
    assert a != c


def test_fingerprint_eval_fail_shape():
    fp = ls.lesson_fingerprint({"source": "eval_fail", "kind": "cli", "command": "pytest", "outcome": "fail"})
    assert isinstance(fp, str) and len(fp) == 16


# --- score_lessons -----------------------------------------------------------

def test_score_single_fresh_lesson_baseline(tmp_root: Path):
    _write_lessons(tmp_root, [
        {"id": "lesson-1", "source": "s", "failure": "f", "cause": "c",
         "fix": "x", "created_at": _iso_days_ago(0)}
    ])
    out = ls.score_lessons(tmp_root)
    assert out["count"] == 1
    item = out["items"][0]
    assert item["reinforcements"] == 1
    assert item["confidence"] == pytest.approx(0.5, abs=1e-6)
    assert item["stale"] is False


def test_reinforcement_increases_confidence(tmp_root: Path):
    # Same fingerprint repeated 3x (fresh) → reinforced confidence > 0.5
    rec = {"source": "s", "failure": "f", "cause": "c", "fix": "x"}
    _write_lessons(tmp_root, [
        {**rec, "id": f"lesson-{i}", "created_at": _iso_days_ago(0)} for i in range(3)
    ])
    out = ls.score_lessons(tmp_root)
    assert out["count"] == 1  # collapsed by fingerprint
    item = out["items"][0]
    assert item["reinforcements"] == 3
    # 0.5 -> 0.55 -> 0.595
    assert item["confidence"] == pytest.approx(0.595, abs=1e-6)


def test_decay_makes_old_single_lesson_stale(tmp_root: Path):
    _write_lessons(tmp_root, [
        {"id": "lesson-old", "source": "s", "failure": "f", "cause": "c",
         "fix": "x", "created_at": _iso_days_ago(400)}
    ])
    out = ls.score_lessons(tmp_root, include_stale=True)
    item = out["items"][0]
    # ~57 weeks * 0.05 >> 0.5 → floored 0.05, single sighting → stale
    assert item["confidence"] == pytest.approx(0.05, abs=1e-9)
    assert item["stale"] is True
    # excluded when include_stale=False
    out2 = ls.score_lessons(tmp_root, include_stale=False)
    assert out2["count"] == 0


def test_score_lessons_decay_rate_override(tmp_root: Path):
    _write_lessons(tmp_root, [
        {"id": "lesson-1", "source": "s", "failure": "f", "cause": "c",
         "fix": "x", "created_at": _iso_days_ago(70)}  # 10 weeks
    ])
    # decay_rate=0 → no decay, stays 0.5
    out = ls.score_lessons(tmp_root, decay_rate=0.0)
    assert out["items"][0]["confidence"] == pytest.approx(0.5, abs=1e-6)


def test_score_lessons_sorted_desc(tmp_root: Path):
    _write_lessons(tmp_root, [
        {"id": "old", "source": "a", "failure": "fa", "cause": "c", "fix": "x",
         "created_at": _iso_days_ago(60)},
        {"id": "fresh", "source": "b", "failure": "fb", "cause": "c", "fix": "x",
         "created_at": _iso_days_ago(0)},
    ])
    items = ls.score_lessons(tmp_root)["items"]
    confs = [i["confidence"] for i in items]
    assert confs == sorted(confs, reverse=True)


# --- recall_lessons ----------------------------------------------------------

def test_recall_empty_query(tmp_root: Path):
    assert ls.recall_lessons(tmp_root, query="")["count"] == 0


def test_recall_matches_and_ranks(tmp_root: Path):
    _write_lessons(tmp_root, [
        {"id": "l1", "source": "s", "failure": "pytest import error on venv",
         "cause": "missing sync", "fix": "run uv sync", "created_at": _iso_days_ago(0)},
        {"id": "l2", "source": "s2", "failure": "docker build cache miss",
         "cause": "layer order", "fix": "reorder", "created_at": _iso_days_ago(0)},
    ])
    out = ls.recall_lessons(tmp_root, query="pytest import")
    assert out["count"] == 1
    top = out["items"][0]
    assert top["id"] == "l1"
    assert top["relevance"] == pytest.approx(1.0)
    assert top["recall_score"] > 0


def test_recall_excludes_stale_by_default(tmp_root: Path):
    _write_lessons(tmp_root, [
        {"id": "old", "source": "s", "failure": "pytest fails", "cause": "c",
         "fix": "x", "created_at": _iso_days_ago(400)},
    ])
    assert ls.recall_lessons(tmp_root, query="pytest")["count"] == 0
    assert ls.recall_lessons(tmp_root, query="pytest", include_stale=True)["count"] == 1


def test_recall_respects_limit(tmp_root: Path):
    _write_lessons(tmp_root, [
        {"id": f"l{i}", "source": f"s{i}", "failure": f"pytest case {i}",
         "cause": "c", "fix": "x", "created_at": _iso_days_ago(0)}
        for i in range(5)
    ])
    out = ls.recall_lessons(tmp_root, query="pytest", limit=2)
    assert out["count"] == 2
