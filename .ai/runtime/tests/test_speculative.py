"""Tests for ai_core.speculative — PASTE-style speculative tool execution PoC."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import speculative as spec  # noqa: E402


# ---------- fixtures ----------

def _audit_path(root: Path) -> Path:
    year = datetime.now(timezone.utc).year
    p = root / ".ai" / "memory" / "audit" / f"{year}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_pretool(audit: Path, *, tool: str, session: str = "s1") -> None:
    """Append a synthetic PreToolUse audit record matching the legacy
    `event.append` / `payload.kind` dialect that the production hook emits."""
    record = {
        "action": "event.append",
        "category": "memory",
        "payload": {
            "agent": "unknown",
            "kind": "PreToolUse",
            "tool_name": tool,
            "session_id": session,
        },
        "ts": "2026-05-25T00:00:00.000000Z",
    }
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


@pytest.fixture()
def fresh_root(tmp_path: Path) -> Path:
    """Empty repo root with .ai/ scaffolding."""
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "cache").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------- mining ----------

def test_mine_empty(tmp_path: Path) -> None:
    """No audit directory at all -> ok=True with empty patterns list."""
    result = spec.mine_patterns(tmp_path)
    assert result["ok"] is True
    assert result["patterns"] == []
    assert result["scanned_events"] == 0


def test_mine_basic_bigram(fresh_root: Path) -> None:
    """5 events Read,Edit,Read,Edit,Read => (Read->Edit) support>=2."""
    audit = _audit_path(fresh_root)
    for tool in ("Read", "Edit", "Read", "Edit", "Read"):
        _write_pretool(audit, tool=tool, session="s1")

    result = spec.mine_patterns(fresh_root, min_support=2, min_confidence=0.1)
    assert result["ok"] is True
    bigrams = {(p["preceding"], p["following"]): p for p in result["patterns"]}
    assert ("Read", "Edit") in bigrams
    assert bigrams[("Read", "Edit")]["support"] >= 2
    # Read precedes Edit twice out of two Read-transitions => confidence 1.0
    assert bigrams[("Read", "Edit")]["confidence"] == pytest.approx(1.0)


def test_mine_respects_threshold(fresh_root: Path) -> None:
    """min_confidence=0.99 prunes any pattern below ~1.0."""
    audit = _audit_path(fresh_root)
    # Mixed sequence: Read -> Edit (once), Read -> Bash (once)
    # => P(Edit|Read)=0.5, P(Bash|Read)=0.5 — both below 0.99
    for tool in ("Read", "Edit", "Read", "Bash"):
        _write_pretool(audit, tool=tool, session="mixed")

    result = spec.mine_patterns(fresh_root, min_support=1, min_confidence=0.99)
    assert result["ok"] is True
    # No pattern from "Read" should survive
    for p in result["patterns"]:
        if p["preceding"] == "Read":
            assert p["confidence"] >= 0.99


def test_predict_next_returns_top_pattern(fresh_root: Path) -> None:
    """After mining a clear Read->Edit pattern, predict_next("Read") -> Edit."""
    audit = _audit_path(fresh_root)
    for tool in ("Read", "Edit", "Read", "Edit", "Read", "Edit"):
        _write_pretool(audit, tool=tool, session="s1")

    result = spec.predict_next(fresh_root, "Read", min_confidence=0.5)
    assert result["ok"] is True
    assert result["prediction"] is not None
    assert result["prediction"]["following"] == "Edit"
    assert result["prediction"]["confidence"] >= 0.5


def test_predict_next_no_match(fresh_root: Path) -> None:
    """Unknown current_tool returns prediction=None (not error)."""
    audit = _audit_path(fresh_root)
    for tool in ("Read", "Edit", "Read", "Edit"):
        _write_pretool(audit, tool=tool, session="s1")
    result = spec.predict_next(fresh_root, "Glob", min_confidence=0.5)
    assert result["ok"] is True
    assert result["prediction"] is None


def test_record_speculation_and_outcome_roundtrip(fresh_root: Path) -> None:
    """record_speculation + record_outcome -> hit_rate counts match."""
    pattern = {"preceding": "Read", "following": "Edit", "support": 3, "confidence": 0.8}

    spec.record_speculation(fresh_root, exec_id="x1", pattern=pattern, predicted_tool="Edit")
    spec.record_outcome(fresh_root, exec_id="x1", hit=True, actual_tool="Edit")

    spec.record_speculation(fresh_root, exec_id="x2", pattern=pattern, predicted_tool="Edit")
    spec.record_outcome(fresh_root, exec_id="x2", hit=False, actual_tool="Bash")

    spec.record_speculation(fresh_root, exec_id="x3", pattern=pattern, predicted_tool="Edit")
    spec.record_outcome(fresh_root, exec_id="x3", hit=True, actual_tool="Edit")

    rate = spec.hit_rate(fresh_root)
    assert rate["ok"] is True
    assert rate["total"] == 3
    assert rate["hits"] == 2
    assert rate["hit_rate"] == pytest.approx(2 / 3, rel=1e-3)


def test_atomic_record_no_partial_lines(fresh_root: Path) -> None:
    """N record calls -> exactly N parseable jsonl lines, no partials."""
    pattern = {"preceding": "Read", "following": "Edit", "support": 1, "confidence": 0.7}
    N = 25
    for i in range(N):
        spec.record_speculation(
            fresh_root,
            exec_id=f"run-{i}",
            pattern=pattern,
            predicted_tool="Edit",
        )
        spec.record_outcome(
            fresh_root,
            exec_id=f"run-{i}",
            hit=(i % 2 == 0),
            actual_tool="Edit",
        )

    log = fresh_root / ".ai" / "cache" / "speculative.jsonl"
    assert log.exists()
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == N * 2
    for raw in lines:
        rec = json.loads(raw)  # parses cleanly -> no partial writes
        assert "kind" in rec
        assert rec["kind"] in {"speculate", "outcome"}


def test_within_session_only(fresh_root: Path) -> None:
    """Cross-session boundaries do not produce false bigrams."""
    audit = _audit_path(fresh_root)
    # Session A ends with Edit; session B starts with Bash.
    # If we naively chained, (Edit -> Bash) would appear. It must NOT.
    _write_pretool(audit, tool="Read", session="A")
    _write_pretool(audit, tool="Edit", session="A")
    _write_pretool(audit, tool="Bash", session="B")
    _write_pretool(audit, tool="Glob", session="B")

    result = spec.mine_patterns(fresh_root, min_support=1, min_confidence=0.01)
    pairs = {(p["preceding"], p["following"]) for p in result["patterns"]}
    assert ("Read", "Edit") in pairs
    assert ("Bash", "Glob") in pairs
    assert ("Edit", "Bash") not in pairs


def test_skips_events_without_tool_name(fresh_root: Path) -> None:
    """PreToolUse events lacking tool_name (legacy format) are silently skipped."""
    audit = _audit_path(fresh_root)
    # Two legacy bare events (no tool_name) bracketing two valid ones.
    legacy = {
        "action": "event.append",
        "category": "memory",
        "payload": {"agent": "unknown", "kind": "PreToolUse"},
        "ts": "2026-05-25T00:00:00.000000Z",
    }
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy) + "\n")
    _write_pretool(audit, tool="Read", session="s1")
    _write_pretool(audit, tool="Edit", session="s1")
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy) + "\n")

    result = spec.mine_patterns(fresh_root, min_support=1, min_confidence=0.1)
    assert result["ok"] is True
    # Should still see (Read -> Edit) and not blow up on the bare ones.
    pairs = {(p["preceding"], p["following"]) for p in result["patterns"]}
    assert ("Read", "Edit") in pairs


def test_hit_rate_empty(tmp_path: Path) -> None:
    """No cache file -> ok=True totals=0."""
    r = spec.hit_rate(tmp_path)
    assert r["ok"] is True
    assert r["total"] == 0
    assert r["hits"] == 0
    assert r["hit_rate"] == 0.0


def test_record_handles_bad_exec_id(fresh_root: Path) -> None:
    """Empty exec_id is dropped silently — no exception, no log entry."""
    spec.record_speculation(fresh_root, exec_id="", pattern={}, predicted_tool="Edit")
    spec.record_outcome(fresh_root, exec_id="", hit=True, actual_tool="Edit")
    log = fresh_root / ".ai" / "cache" / "speculative.jsonl"
    # File may not exist OR may exist empty
    if log.exists():
        assert log.read_text(encoding="utf-8").strip() == ""
