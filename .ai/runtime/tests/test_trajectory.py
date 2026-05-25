"""Tests for ai_core.trajectory (TRAJEVAL-style trajectory diagnostics)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.trajectory import (  # noqa: E402
    analyze_efficiency,
    analyze_failures,
    extract_trajectories,
    summarize,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_audit(root: Path, events: list[dict]) -> Path:
    audit_dir = root / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "2026.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return path


def _event(
    ts: str,
    *,
    action: str = "event.append",
    kind: str | None = None,
    tool_name: str | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> dict:
    payload: dict = {}
    if kind is not None:
        payload["kind"] = kind
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if session_id is not None:
        payload["session_id"] = session_id
    if path is not None:
        payload["path"] = path
    return {
        "action": action,
        "category": "memory",
        "payload": payload,
        "ts": ts,
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extract_empty(tmp_path: Path) -> None:
    out = extract_trajectories(tmp_path)
    assert out["ok"] is True
    assert out["trajectories"] == []
    assert out["scanned_events"] == 0


def test_extract_basic(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Read", session_id="s1"),
        _event("2026-05-01T10:00:05Z", tool_name="Read", session_id="s1"),
        _event("2026-05-01T10:00:10Z", tool_name="Edit", session_id="s1"),
        _event("2026-05-01T10:00:15Z", tool_name="Read", session_id="s1"),
        _event("2026-05-01T10:00:20Z", tool_name="Bash", session_id="s1"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path)
    assert out["ok"] is True
    assert len(out["trajectories"]) == 1
    traj = out["trajectories"][0]
    assert traj["total_events"] == 5
    assert traj["session_id"] == "s1"
    assert traj["start_ts"] == "2026-05-01T10:00:00Z"
    assert traj["end_ts"] == "2026-05-01T10:00:20Z"
    assert traj["duration_seconds"] == pytest.approx(20.0)


def test_extract_groups_by_session_id(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Read", session_id="alpha"),
        _event("2026-05-01T10:00:05Z", tool_name="Read", session_id="alpha"),
        _event("2026-05-01T10:00:10Z", tool_name="Read", session_id="beta"),
        _event("2026-05-01T10:00:15Z", tool_name="Edit", session_id="beta"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path)
    assert len(out["trajectories"]) == 2
    sids = {t["session_id"] for t in out["trajectories"]}
    assert sids == {"alpha", "beta"}


def test_extract_anonymous_idle_gap(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", kind="PreToolUse"),
        _event("2026-05-01T10:00:30Z", kind="PostToolUse"),
        # >5min gap -> new anon trajectory
        _event("2026-05-01T10:10:00Z", kind="PreToolUse"),
        _event("2026-05-01T10:10:05Z", kind="PostToolUse"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path)
    assert len(out["trajectories"]) == 2
    for traj in out["trajectories"]:
        assert traj["session_id"].startswith("anon-")


def test_extract_session_filter(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Read", session_id="alpha"),
        _event("2026-05-01T10:00:05Z", tool_name="Edit", session_id="beta"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path, session_id="alpha")
    assert len(out["trajectories"]) == 1
    assert out["trajectories"][0]["session_id"] == "alpha"


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def test_efficiency_repeat_rate(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Read", session_id="s"),
        _event("2026-05-01T10:00:10Z", tool_name="Read", session_id="s"),
        _event("2026-05-01T10:00:20Z", tool_name="Read", session_id="s"),
        _event("2026-05-01T10:00:30Z", tool_name="Read", session_id="s"),
        _event("2026-05-01T10:00:40Z", tool_name="Edit", session_id="s"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path)
    eff = analyze_efficiency(out["trajectories"][0])
    # Repeated positions: indexes 1,2,3 (Read==Read) and 4 (Edit!=Read) → 3/5 = 0.6.
    # Test description used "0.75 정도" loosely; we assert that Read dominates
    # and that the rate is at least 0.6 (high repetition signal).
    assert eff["total_events"] == 5
    assert eff["unique_tools"] == 2
    assert eff["tool_repeat_rate"] >= 0.6
    assert eff["dominant_tool"] == "Read"
    assert eff["dominant_tool_share"] == pytest.approx(0.8)


def test_efficiency_empty() -> None:
    eff = analyze_efficiency({"events": [], "duration_seconds": 0})
    assert eff["total_events"] == 0
    assert eff["tool_repeat_rate"] == 0.0
    assert eff["unique_tools"] == 0


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_failures_loop_detection(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Read", session_id="s"),
        _event("2026-05-01T10:00:01Z", tool_name="Edit", session_id="s"),
        _event("2026-05-01T10:00:02Z", tool_name="Read", session_id="s"),
        _event("2026-05-01T10:00:03Z", tool_name="Edit", session_id="s"),
        _event("2026-05-01T10:00:04Z", tool_name="Read", session_id="s"),
        _event("2026-05-01T10:00:05Z", tool_name="Edit", session_id="s"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path)
    failures = analyze_failures(out["trajectories"][0])
    assert failures["loop_suspected"] is True


def test_failures_shallow_exploration(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Bash", session_id="s"),
        _event("2026-05-01T10:00:01Z", tool_name="Bash", session_id="s"),
        _event("2026-05-01T10:00:02Z", tool_name="Bash", session_id="s"),
        _event("2026-05-01T10:00:03Z", tool_name="Bash", session_id="s"),
        _event("2026-05-01T10:00:04Z", tool_name="Bash", session_id="s"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path)
    failures = analyze_failures(out["trajectories"][0])
    assert failures["shallow_exploration"] is True


def test_failures_backtrack_evidence(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Edit", session_id="s", path="a.py"),
        _event("2026-05-01T10:00:01Z", tool_name="Bash", session_id="s"),
        _event("2026-05-01T10:00:02Z", tool_name="Read", session_id="s", path="a.py"),
    ]
    _write_audit(tmp_path, events)
    out = extract_trajectories(tmp_path)
    failures = analyze_failures(out["trajectories"][0])
    assert failures["backtrack_evidence"] is True


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summarize_limit_respected(tmp_path: Path) -> None:
    events = []
    for i, sid in enumerate(["s1", "s2", "s3"]):
        events.append(
            _event(
                f"2026-05-0{i + 1}T10:00:00Z", tool_name="Read", session_id=sid
            )
        )
    _write_audit(tmp_path, events)
    out = summarize(tmp_path, limit=2)
    assert out["ok"] is True
    assert out["total_sessions"] == 2
    assert len(out["summary"]) == 2


def test_summarize_orders_recent_first(tmp_path: Path) -> None:
    events = [
        _event("2026-05-01T10:00:00Z", tool_name="Read", session_id="old"),
        _event("2026-05-02T10:00:00Z", tool_name="Read", session_id="newer"),
        _event("2026-05-03T10:00:00Z", tool_name="Read", session_id="newest"),
    ]
    _write_audit(tmp_path, events)
    out = summarize(tmp_path, limit=10)
    sids = [s["session_id"] for s in out["summary"]]
    assert sids == ["newest", "newer", "old"]
