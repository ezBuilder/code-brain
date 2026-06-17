"""Tests for memory_tier.page_in — sleep-time HOT consolidation (T30 step C).

Covers: empty-root no-op, ranked cache, dry-run no-write, byte bound,
determinism, fail-soft, audit emission, and the no-LLM/offline invariant.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import memory_tier as mt  # noqa: E402
from ai_core import memory_hot  # noqa: E402
from ai_core import loop_engineering as le  # noqa: E402
from ai_core.memory import (  # noqa: E402
    append_audit,
    append_decision,
    append_todo,
    all_audit_files,
)
from ai_core.lessons import add_lesson  # noqa: E402


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True)
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    return tmp_path


def _seed(root: Path, *, decisions: int = 3, lessons: int = 2, todos: int = 2) -> None:
    for i in range(decisions):
        append_decision(root, text=f"decision number {i}", tags=["seed"], source="test")
    for i in range(lessons):
        add_lesson(
            root,
            source="test",
            failure=f"failure {i}",
            cause=f"cause {i}",
            fix=f"fix {i}",
            tags=["seed"],
        )
    for i in range(todos):
        append_todo(root, title=f"open todo {i}", source="test")
    append_audit(root, action="test.recent", category="memory", payload={"x": 1})


def _audit_rows(root: Path) -> list[dict]:
    rows: list[dict] = []
    for af in all_audit_files(root):
        for line in af.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# (1) empty root → ok with no items, never raises.
def test_page_in_empty_root_ok_no_items(tmp_root: Path):
    out = mt.page_in(tmp_root)
    assert out["ok"] is True
    assert out["items"] == []
    # an empty page-in writes the (empty) cache but no audit row
    assert out["written"] is True


# (2) seeded → cache written with non-empty, salience-RANKED items, refs<=80.
def test_page_in_writes_ranked_cache(tmp_root: Path):
    _seed(tmp_root)
    out = mt.page_in(tmp_root)
    assert out["ok"] is True
    cache_path = memory_hot.hot_cache_path(tmp_root)
    assert cache_path.exists()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    items = payload["items"]
    assert len(items) >= 1
    scores = [it["score"] for it in items]
    assert scores == sorted(scores, reverse=True)  # monotonically non-increasing
    for it in items:
        assert len(it["ref"]) <= 80
        assert it["tier"] in ("hot", "warm")


# (3) dry_run → same item count, NO cache file written.
def test_page_in_dry_run_writes_nothing(tmp_root: Path):
    _seed(tmp_root)
    real = mt.page_in(tmp_root, dry_run=False)
    memory_hot.hot_cache_path(tmp_root).unlink()  # remove the real write
    dry = mt.page_in(tmp_root, dry_run=True)
    assert dry["ok"] is True
    assert dry["written"] is False
    assert len(dry["items"]) == len(real["items"])
    assert not memory_hot.hot_cache_path(tmp_root).exists()


# (4) byte bound truncates the item list and stays under the cap.
def test_page_in_byte_bound_truncates(tmp_root: Path, monkeypatch):
    _seed(tmp_root, decisions=8, lessons=6, todos=4)
    unbounded = mt.page_in(tmp_root, dry_run=True)
    monkeypatch.setenv("AI_MEMORY_HOT_CACHE_BYTES", "120")
    bounded = mt.page_in(tmp_root, dry_run=False)
    cache = json.loads(memory_hot.hot_cache_path(tmp_root).read_text(encoding="utf-8"))
    serialized = json.dumps(cache["items"], ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= 120
    assert len(bounded["items"]) < len(unbounded["items"])


# (5) determinism: two runs on identical inputs → identical item ordering.
def test_page_in_deterministic(tmp_root: Path):
    _seed(tmp_root)
    a = mt.page_in(tmp_root, dry_run=True)
    b = mt.page_in(tmp_root, dry_run=True)
    assert a["items"] == b["items"]


# (6) fail-soft: scoring raising → ok=False, never raises.
def test_page_in_fail_soft(tmp_root: Path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr(mt, "scored_durable_items", _boom)
    out = mt.page_in(tmp_root)
    assert out["ok"] is False
    assert "error" in out


# (7) exactly one audit row on a real run, zero on dry_run.
def test_page_in_audit_emission(tmp_root: Path):
    _seed(tmp_root)
    before = [r for r in _audit_rows(tmp_root) if r.get("action") == "memtier.page_in"]
    assert before == []
    mt.page_in(tmp_root, dry_run=True)
    after_dry = [r for r in _audit_rows(tmp_root) if r.get("action") == "memtier.page_in"]
    assert after_dry == []  # dry-run emits nothing
    mt.page_in(tmp_root, dry_run=False)
    after_real = [r for r in _audit_rows(tmp_root) if r.get("action") == "memtier.page_in"]
    assert len(after_real) == 1


# (8a) default env → NO loop request submitted (no LLM, offline).
def test_page_in_no_llm_by_default(tmp_root: Path):
    _seed(tmp_root)
    mt.page_in(tmp_root, dry_run=False)
    inbox = le.loop_root(tmp_root) / "inbox"
    pending = list(inbox.glob("*.json")) if inbox.exists() else []
    assert pending == []


# (8b) AI_MEMORY_HOT_SUMMARIZE=1 → exactly one cheap, non-self loop request.
def test_page_in_optin_enqueues_cheap_nonself(tmp_root: Path, monkeypatch):
    _seed(tmp_root)
    monkeypatch.setenv("AI_MEMORY_HOT_SUMMARIZE", "1")
    out = mt.page_in(tmp_root, dry_run=False)
    assert out["summary"]["enqueued"] is True
    inbox = le.loop_root(tmp_root) / "inbox"
    pending = sorted(inbox.glob("*.json"))
    assert len(pending) == 1
    req = json.loads(pending[0].read_text(encoding="utf-8"))
    assert req["dispatch"]["model_tier"] == "cheap"
    assert req["reviewer_required"] is False
