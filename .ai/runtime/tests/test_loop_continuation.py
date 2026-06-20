"""G3: Stop-hook plan continuation — opt-in, plan-gated, bounded, security-safe."""
from __future__ import annotations

from pathlib import Path

from ai_core import loop_continuation as lc
from ai_core import plan_state as ps


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _active_plan(root: Path) -> None:
    ps.init_plan(root, plan_id="feat", steps=["a", "b"])  # 2 remaining


def test_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.delenv("AI_LOOP_CONTINUATION", raising=False)
    assert lc.continuation_directive({"session_id": "s1"}, root) is None


def test_continues_with_active_plan(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    out = lc.continuation_directive({"session_id": "s1"}, root)
    assert out and "next step: a" in out and "Do NOT stop" in out


def test_no_plan_no_continuation(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    assert lc.continuation_directive({"session_id": "s1"}, root) is None


def test_completed_plan_stops(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    ps.init_plan(root, plan_id="feat", steps=["a"])
    ps.mark_step(root, plan_id="feat", index=1)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    assert lc.continuation_directive({"session_id": "s1"}, root) is None


def test_stop_hook_active_breaks_self_loop(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    assert lc.continuation_directive({"session_id": "s1", "stop_hook_active": True}, root) is None


def test_antigravity_no_continuation(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    assert lc.continuation_directive({"session_id": "s1", "agent": "antigravity"}, root) is None


def test_context_pressure_no_continuation(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    assert lc.continuation_directive({"session_id": "s1", "context_pressure": True}, root) is None


def test_counter_cap_bounds_runaway(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    fired = 0
    for _ in range(lc.MAX_CONTINUATIONS + 5):
        if lc.continuation_directive({"session_id": "s1"}, root, now=1000.0):
            fired += 1
    assert fired == lc.MAX_CONTINUATIONS


def test_wall_clock_cap(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    assert lc.continuation_directive({"session_id": "s2"}, root, now=1000.0)          # first
    assert lc.continuation_directive({"session_id": "s2"}, root,
                                     now=1000.0 + lc.MAX_WALL_SECONDS + 1) is None     # too late


def test_hook_stop_emits_block_when_continuation_fires(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: Stop hook with an active plan + flag re-prompts via decision=block + reason."""
    root = _seed(tmp_path)
    _active_plan(root)
    from ai_core import hooks
    payload = {"agent": "claude", "session_id": "s9", "dry": True, "last_assistant_message": "done"}
    monkeypatch.delenv("AI_LOOP_CONTINUATION", raising=False)
    off = hooks.handle_hook(root, "Stop", dict(payload))
    assert not off.get("continuation") and off.get("decision") != "block"  # off by default
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    on = hooks.handle_hook(root, "Stop", dict(payload))
    assert on.get("continuation") is True
    assert on.get("decision") == "block" and on.get("reason")
