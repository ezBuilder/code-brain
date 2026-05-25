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


def test_page_out_dry_run_idempotent(tmp_root: Path):
    """dry-run must not move anything."""
    from ai_core.memory_tier import page_out
    sessions_dir = tmp_root / ".ai" / "memory" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "old-sess").mkdir()
    import os, time
    old = time.time() - 60 * 86400  # 60 days old
    os.utime(sessions_dir / "old-sess", (old, old))
    payload = page_out(tmp_root, dry_run=True)
    assert payload["ok"] is True
    assert (sessions_dir / "old-sess").exists()  # still there
    assert payload["archived"]["dry_run"] is True
    assert "old-sess" in payload["archived"]["moved"]  # would move


def test_page_out_actually_archives_old_sessions(tmp_root: Path):
    from ai_core.memory_tier import page_out
    sessions_dir = tmp_root / ".ai" / "memory" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "old-sess").mkdir()
    (sessions_dir / "old-sess" / "snap.json").write_text("{}", encoding="utf-8")
    (sessions_dir / "fresh-sess").mkdir()
    import os, time
    old = time.time() - 60 * 86400
    os.utime(sessions_dir / "old-sess", (old, old))
    payload = page_out(tmp_root, dry_run=False)
    assert (sessions_dir / "fresh-sess").exists()
    assert not (sessions_dir / "old-sess").exists()
    assert (sessions_dir / ".archive" / "old-sess").exists()
    assert "old-sess" in payload["archived"]["moved"]


def test_page_out_includes_audit_fold_result(tmp_root: Path):
    """page_out() should include audit_fold result in output."""
    from ai_core.memory_tier import page_out
    payload = page_out(tmp_root, dry_run=False)
    assert "audit_fold" in payload
    assert payload["audit_fold"]["ok"] is True


def test_page_out_skips_audit_fold_when_days_zero(tmp_root: Path, monkeypatch):
    """AI_AUDIT_FOLD_DAYS=0 → skip folding."""
    from ai_core.memory_tier import page_out
    monkeypatch.setenv("AI_AUDIT_FOLD_DAYS", "0")
    payload = page_out(tmp_root, dry_run=False)
    assert payload["audit_fold"]["skipped"] is True
    assert "disabled" in payload["audit_fold"]["reason"]


def test_page_out_calls_audit_fold_with_dry_run(tmp_root: Path, monkeypatch):
    """dry_run=True → audit_fold also gets dry_run=True."""
    from ai_core.memory_tier import page_out
    from datetime import datetime, timezone, timedelta
    import json

    # Create an old audit entry (45 days ago)
    audit_dir = tmp_root / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_file = audit_dir / "2026.jsonl"

    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    old_entry = {"action": "test.old", "ts": old_ts, "category": "test"}
    audit_file.write_text(json.dumps(old_entry) + "\n", encoding="utf-8")

    payload = page_out(tmp_root, dry_run=True)
    assert payload["dry_run"] is True
    assert payload["audit_fold"]["dry_run"] is True
    # File should not be modified in dry_run
    content = audit_file.read_text(encoding="utf-8")
    assert "test.old" in content


def test_page_out_actually_folds_old_audit_entries(tmp_root: Path, monkeypatch):
    """page_out() triggers audit_fold for entries > 30 days old."""
    from ai_core.memory_tier import page_out
    from datetime import datetime, timezone, timedelta
    import json

    # Create old and recent audit entries
    audit_dir = tmp_root / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_file = audit_dir / "2026.jsonl"

    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    recent_ts = datetime.now(timezone.utc).isoformat()

    old_entry = {"action": "test.old", "ts": old_ts, "category": "test"}
    recent_entry = {"action": "test.recent", "ts": recent_ts, "category": "test"}

    audit_file.write_text(
        json.dumps(old_entry) + "\n" + json.dumps(recent_entry) + "\n",
        encoding="utf-8"
    )

    payload = page_out(tmp_root, dry_run=False)
    assert payload["ok"] is True
    assert payload["audit_fold"]["ok"] is True
    assert payload["audit_fold"]["folded_days"] >= 1
    assert payload["audit_fold"]["removed_entries"] >= 1

    # Check that file now contains _folded record
    content = audit_file.read_text(encoding="utf-8")
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    fold_records = [json.loads(l) for l in lines if json.loads(l).get("action") == "_folded"]
    assert len(fold_records) >= 1
