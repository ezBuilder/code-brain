"""-011: naive/aware datetime normalization across audit/memory readers.

Readers that compare a parsed timestamp against an aware cutoff used to return
a NAIVE datetime for offset-less input; the comparison then raised TypeError
PAST fail-soft guards that only catch ValueError. Audit and memory files are
git-synced and hand-editable, so the offset-less shape does arrive. Guarded
surfaces:

  1. every per-module parse helper reads offset-less as UTC (never naive),
  2. memory_tier.classify / scored_durable_items — the SessionStart HOT-cache
     path — survive naive, null, empty, and garbage timestamps,
  3. hooks' cooldown scanners survive naive audit ts,
  4. hooks.build_context renders a SessionStart context over a store seeded
     with offset-less timestamps (the P0 gate),
  5. audit rotation no longer aborts on "ts": null / "" / garbage rows,
  6. loop_engineering._is_expired stays fail-soft for naive bounds.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import audit_fold  # noqa: E402
from ai_core import doctor  # noqa: E402
from ai_core import hooks  # noqa: E402
from ai_core import loop_engineering  # noqa: E402
from ai_core import memory  # noqa: E402
from ai_core import memory_tier  # noqa: E402
from ai_core import obs  # noqa: E402
from ai_core import trajectory  # noqa: E402

NAIVE_WALL = "2026-07-30T12:00:00"
AWARE_Z = "2026-07-30T12:00:00Z"
AWARE_OFFSET = "2026-07-30T07:00:00-05:00"  # same instant as AWARE_Z


def _private_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _naive_recent(hours_ago: float = 0.1) -> str:
    """Offset-less timestamp a few minutes in the past (UTC wall time)."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None).isoformat()


PARSERS = [
    pytest.param(hooks._parse_audit_ts_utc, id="hooks"),
    pytest.param(obs._parse_ts_utc, id="obs"),
    pytest.param(memory_tier._parse_ts, id="memory_tier"),
    pytest.param(audit_fold._parse_ts, id="audit_fold"),
    pytest.param(trajectory._parse_ts, id="trajectory"),
]


@pytest.mark.parametrize("parse", PARSERS)
def test_parse_helpers_read_offsetless_as_utc(parse) -> None:
    naive = parse(NAIVE_WALL)
    assert naive is not None and naive.tzinfo is not None
    assert naive == parse(AWARE_Z), "offset-less must mean the same instant as the Z form"
    assert parse(AWARE_OFFSET) == parse(AWARE_Z)


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("bad", ["", "garbage", "2026-99-99T00:00:00"])
def test_parse_helpers_fail_soft(parse, bad: str) -> None:
    assert parse(bad) is None


def test_classify_counts_naive_recent_audit_row_as_live(tmp_path: Path) -> None:
    rows = [
        {"ts": _naive_recent(), "action": "a", "category": "t", "payload": {}},
        {"ts": memory.now_iso(), "action": "b", "category": "t", "payload": {}},
        {"ts": None, "action": "c", "category": "t", "payload": {}},
        {"ts": "", "action": "d", "category": "t", "payload": {}},
        {"ts": "garbage", "action": "e", "category": "t", "payload": {}},
    ]
    _private_file(
        memory.audit_path(tmp_path),
        "".join(json.dumps(r) + "\n" for r in rows),
    )
    out = memory_tier.classify(tmp_path)
    tiers = out["tiers"]
    live = tiers["hot"]["audit_events"] + tiers["warm"]["audit_events"]
    # naive-recent + Z-now are live; null/empty/garbage stay cold (fail-soft), not a crash
    assert live == 2
    assert tiers["cold"]["audit_events"] == 3


def test_scored_durable_items_survives_naive_decided_at(tmp_path: Path) -> None:
    (tmp_path / ".ai" / "memory").mkdir(parents=True)
    rows = [
        {"id": "dec-1", "decided_at": _naive_recent(), "decision": "naive ts", "kind": "decision"},
        {"id": "dec-2", "decided_at": memory.now_iso(), "decision": "aware ts", "kind": "decision"},
    ]
    _private_file(
        tmp_path / ".ai" / "memory" / "decisions.jsonl",
        "".join(json.dumps(r) + "\n" for r in rows),
    )
    items = [i for i in memory_tier.scored_durable_items(tmp_path) if i["kind"] == "decision"]
    assert len(items) == 2
    # a recent naive timestamp must read as recent, not "unknown age → 365d stale"
    assert all(i["age_days"] < 1.0 for i in items)


def test_cooldown_scanners_survive_naive_audit_ts(tmp_path: Path) -> None:
    rows = [
        {"ts": _naive_recent(), "action": "skill.recommend_pending", "category": "m", "payload": {"id": "cand-naive"}},
        {"ts": memory.now_iso(), "action": "skill.recommend_pending", "category": "m", "payload": {"id": "cand-aware"}},
        {"ts": "2001-01-01T00:00:00", "action": "skill.recommend_pending", "category": "m", "payload": {"id": "cand-old"}},
    ]
    _private_file(
        memory.audit_path(tmp_path),
        "".join(json.dumps(r) + "\n" for r in rows),
    )
    recent = hooks._recently_surfaced_ids(tmp_path, cooldown_hours=24.0)
    assert recent == {"cand-naive", "cand-aware"}

    weights = hooks._cooldown_weights(tmp_path, half_life_hours=24.0)
    assert set(weights) == {"cand-naive", "cand-aware", "cand-old"}
    # naive-recent decays like aware-recent; the ancient one is fully decayed
    assert weights["cand-naive"] > 0.5
    assert weights["cand-old"] < 0.01


def test_build_context_renders_over_offsetless_store(tmp_path: Path) -> None:
    """P0 gate: a store seeded with offset-less timestamps renders SessionStart context."""
    audit_rows = [
        {"ts": NAIVE_WALL, "action": "skill.recommend_pending", "category": "m", "payload": {"id": "cand-1"}},
        {"ts": _naive_recent(), "action": "skill.auto_accept", "category": "memory", "payload": {"id": "cand-1"}},
    ]
    _private_file(
        memory.audit_path(tmp_path),
        "".join(json.dumps(r) + "\n" for r in audit_rows),
    )
    decision_rows = [
        {"id": "dec-1", "decided_at": NAIVE_WALL, "decision": "offset-less decision", "kind": "decision"},
    ]
    _private_file(
        tmp_path / ".ai" / "memory" / "decisions.jsonl",
        "".join(json.dumps(r) + "\n" for r in decision_rows),
    )
    context = hooks.build_context("SessionStart", {"agent": "operator", "dry": True}, root=tmp_path)
    assert isinstance(context, str) and context
    assert "offset-less decision" in context


def test_audit_segmentation_preserves_null_empty_and_garbage_ts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)

    bad_rows = [
        {"ts": None, "action": "bad.null", "category": "t", "payload": {"pad": "x" * 60}},
        {"ts": "", "action": "bad.empty", "category": "t", "payload": {"pad": "x" * 60}},
        {"ts": "not-a-time", "action": "bad.garbage", "category": "t", "payload": {"pad": "x" * 60}},
        {"ts": NAIVE_WALL, "action": "bad.naive", "category": "t", "payload": {"pad": "x" * 60}},
    ]
    path = memory.audit_path(tmp_path)
    filler = [
        {"ts": memory.now_iso(), "action": "fill", "category": "t", "payload": {"pad": "x" * 60, "i": i}}
        for i in range(14)
    ]
    _private_file(path, "".join(json.dumps(r) + "\n" for r in [*filler, *bad_rows]))
    assert path.stat().st_size > 2_000

    # This append seals the oversized file byte-for-byte; malformed timestamps
    # are raw evidence and must neither be parsed nor discarded.
    memory.append_audit(tmp_path, action="after.rotate", category="t", payload={})

    assert path.stat().st_size <= 2_000
    rows = [
        json.loads(line)
        for audit_file in memory.all_audit_files(tmp_path)
        for line in audit_file.read_text().splitlines()
    ]
    assert any(row["action"] == "audit.segment_started" for row in rows)
    assert rows[-1]["action"] == "after.rotate"
    kept_actions = {r["action"] for r in rows}
    assert {"bad.null", "bad.empty", "bad.garbage", "bad.naive"} <= kept_actions
    chain = doctor.check_audit_chain(tmp_path)
    assert chain.ok is True
    assert "legacy_unverifiable" in chain.detail


def test_loop_is_expired_failsoft_for_naive_bounds() -> None:
    assert loop_engineering._is_expired("2001-01-01T00:00:00") is True
    future_naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert loop_engineering._is_expired(future_naive) is False
    assert loop_engineering._is_expired("garbage") is False
    assert loop_engineering._is_expired(None) is False
