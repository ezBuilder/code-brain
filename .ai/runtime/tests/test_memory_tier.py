"""memory_tier — MemGPT-style hot/warm/cold classification (T30 step A)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import memory_tier as mt  # noqa: E402
from ai_core.memory import append_audit, append_decision, append_todo, close_todo  # noqa: E402


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True)
    return tmp_path


def test_classify_empty_root_returns_zero_tiers(tmp_root: Path):
    payload = mt.classify(tmp_root)
    assert payload["ok"] is True
    assert payload["totals"]["audit_events"] == 0
    assert payload["tiers"]["hot"]["audit_events"] == 0
    assert payload["tiers"]["warm"]["audit_events"] == 0
    assert payload["tiers"]["cold"]["audit_events"] == 0


def test_classify_counts_recent_audit_as_hot(tmp_root: Path):
    """Fresh audit row (now) → HOT."""
    append_audit(tmp_root, action="test.event", category="memory", payload={"x": 1})
    payload = mt.classify(tmp_root)
    assert payload["totals"]["audit_events"] >= 1
    assert payload["tiers"]["hot"]["audit_events"] >= 1


def test_classify_counts_open_and_closed_todos(tmp_root: Path):
    append_todo(tmp_root, title="open one", source="test")
    append_todo(tmp_root, title="closed one", source="test")
    close_todo(tmp_root, match="closed one", status="done")
    payload = mt.classify(tmp_root)
    assert payload["tiers"]["hot"]["todos_open"] >= 1
    assert payload["tiers"]["cold"]["todos_closed"] >= 1


def test_classify_counts_decisions(tmp_root: Path):
    append_decision(tmp_root, text="dec one", tags=["test"], source="test")
    payload = mt.classify(tmp_root)
    assert payload["tiers"]["warm"]["decisions"] >= 1


def test_hot_pressure_safe_defaults(tmp_root: Path):
    p = mt.hot_pressure(tmp_root)
    assert p["ok"] is True
    assert p["session_md_ratio"] == 0.0
    assert p["page_out_recommended"] is False


def test_env_overrides_ttl(tmp_root: Path, monkeypatch):
    monkeypatch.setenv("AI_MEMORY_HOT_TTL_HOURS", "0")  # nothing is hot
    monkeypatch.setenv("AI_MEMORY_WARM_TTL_DAYS", "0")  # nothing is warm either
    append_audit(tmp_root, action="x.y", category="memory", payload={})
    payload = mt.classify(tmp_root)
    # both TTLs at 0 → all events fall to cold
    assert payload["tiers"]["hot"]["audit_events"] == 0
    assert payload["tiers"]["cold"]["audit_events"] >= 1


def test_pressure_flag_when_session_large(tmp_root: Path, monkeypatch):
    """Force the session-current.md size up to trigger page_out_recommended."""
    from ai_core.memory import session_current_path, _SESSION_NOTE_MAX_BYTES
    spath = session_current_path(tmp_root)
    spath.parent.mkdir(parents=True, exist_ok=True)
    # 80% of cap = page-out recommended
    spath.write_text("# Current Session\n\n" + "x" * int(_SESSION_NOTE_MAX_BYTES * 0.85), encoding="utf-8")
    p = mt.hot_pressure(tmp_root)
    assert p["session_md_ratio"] >= 0.8
    assert p["page_out_recommended"] is True
