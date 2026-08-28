"""G3: Stop-hook plan continuation — opt-in, plan-gated, bounded, security-safe."""
from __future__ import annotations

from pathlib import Path

from ai_core import loop_continuation as lc
from ai_core import plan_state as ps


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    from ai_core import completion_guard

    assert completion_guard.begin_request(tmp_path, "s1")
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


def test_stop_hook_active_does_not_bypass_plan_but_stall_guard_bounds_it(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    payload = {"session_id": "s1", "stop_hook_active": True}
    assert lc.continuation_directive(payload, root)
    assert lc.continuation_directive(payload, root)
    assert lc.continuation_directive(payload, root) is None


def test_antigravity_model_stop_continuation(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    from ai_core import completion_guard

    assert completion_guard.begin_request(root, "agy-1")
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    payload = {
        "conversationId": "agy-1",
        "terminationReason": "model_stop",
        "fullyIdle": True,
    }
    assert lc.continuation_directive(payload, root)


def test_antigravity_terminal_stop_no_continuation(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    payload = {
        "conversationId": "agy-2",
        "terminationReason": "max_steps_exceeded",
        "fullyIdle": True,
    }
    assert lc.continuation_directive(payload, root) is None


def test_context_pressure_no_continuation(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    _active_plan(root)
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    assert lc.continuation_directive({"session_id": "s1", "context_pressure": True}, root) is None


def test_counter_cap_bounds_runaway(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    fired = 0
    for _ in range(lc.MAX_CONTINUATIONS + 5):
        if lc._bump_counter(root, "s1", now=1000.0):
            fired += 1
    assert fired == lc.MAX_CONTINUATIONS


def test_wall_clock_cap(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    assert lc._bump_counter(root, "s2", now=1000.0)
    assert lc._bump_counter(root, "s2", now=1000.0 + lc.MAX_WALL_SECONDS + 1) is False


def test_cap_notice_is_scoped_and_consumed_once(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(lc.MAX_CONTINUATIONS):
        assert lc._bump_counter(root, "notice-session", now=1000.0)
    assert lc._bump_counter(root, "notice-session", now=1001.0) is False
    notice = lc.consume_limit_notice(root, "notice-session")
    assert "repository/worktree + host session" in notice
    assert lc.consume_limit_notice(root, "notice-session") == ""
    assert lc.consume_limit_notice(root, "other-session") == ""


def test_claude_wire_surfaces_cap_release_without_reentering_loop(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    for _ in range(lc.MAX_CONTINUATIONS):
        assert lc._bump_counter(root, "wire-cap", now=1000.0)
    assert lc._bump_counter(root, "wire-cap", now=1001.0) is False
    from ai_core import hooks

    monkeypatch.setenv("AI_COMPLETION_GUARD", "0")
    response = hooks.handle_hook(
        root,
        "Stop",
        {"agent": "claude", "session_id": "wire-cap", "dry": True},
    )
    wire = hooks.hook_wire_output(
        response, {"agent": "claude", "session_id": "wire-cap"}
    )
    assert wire["continue"] is True
    assert "safety cap reached" in wire["systemMessage"]


def test_new_user_request_resets_counter(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(lc.MAX_CONTINUATIONS):
        assert lc._bump_counter(root, "same-session", now=1000.0)
    assert lc._bump_counter(root, "same-session", now=1000.0) is False
    assert lc.reset_counter(root, "same-session") is True
    assert lc._bump_counter(root, "same-session", now=2000.0) is True


def test_hook_stop_emits_block_when_continuation_fires(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: Stop hook with an active plan + flag re-prompts via decision=block + reason."""
    root = _seed(tmp_path)
    from ai_core import hooks
    from ai_core import completion_guard

    assert completion_guard.begin_request(root, "s9")
    _active_plan(root)
    payload = {"agent": "claude", "session_id": "s9", "dry": True, "last_assistant_message": "done"}
    monkeypatch.setenv("AI_COMPLETION_GUARD", "0")  # isolate the opt-in plan driver
    monkeypatch.delenv("AI_LOOP_CONTINUATION", raising=False)
    off = hooks.handle_hook(root, "Stop", dict(payload))
    assert not off.get("continuation") and off.get("decision") != "block"  # off by default
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "1")
    on = hooks.handle_hook(root, "Stop", dict(payload))
    assert on.get("continuation") is True
    assert on.get("decision") == "block" and on.get("reason")
