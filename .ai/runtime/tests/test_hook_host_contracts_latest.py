"""Regression coverage for current host-specific hook contracts.

These tests intentionally exercise the wire boundary separately from the internal
diagnostic response.  A valid internal decision with the wrong host projection is a
silent production failure: Claude task gates need exit code 2, Kiro consumes stdout as
plain context, and non-blockable terminal events must never burn continuation budget.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import hooks  # noqa: E402


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True)
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return tmp_path


def _todo_statuses(root: Path) -> list[str]:
    path = root / ".ai" / "memory" / "todos.jsonl"
    if not path.exists():
        return []
    return [
        str(json.loads(line).get("status") or "")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_normalize_agent_supports_kiro_alias_and_explicit_hook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hooks.normalize_agent({"agent": "kiro-cli"}) == "kiro"
    monkeypatch.setenv("AI_HOOK_AGENT", "kiro")
    assert hooks.normalize_agent({}) == "kiro"


def test_claude_lifecycle_uses_current_payload_field_names(memory_root: Path) -> None:
    hooks._handle_lifecycle_event(
        memory_root,
        "TaskCreated",
        {"task_id": "task-7", "task_subject": "Verify current hook contracts"},
    )
    hooks._handle_lifecycle_event(
        memory_root,
        "CwdChanged",
        {"old_cwd": "/repo", "new_cwd": "/repo/src"},
    )

    todos = (memory_root / ".ai" / "memory" / "todos.jsonl").read_text(encoding="utf-8")
    audit = next((memory_root / ".ai" / "memory" / "audit").glob("*.jsonl")).read_text(
        encoding="utf-8"
    )
    assert "Verify current hook contracts" in todos
    assert '"previous_cwd":"/repo"' in audit.replace(" ", "")


def test_task_completed_closes_todo_only_after_quality_gate_allows(
    memory_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks._handle_lifecycle_event(
        memory_root,
        "TaskCreated",
        {"task_id": "task-9", "task_subject": "Run acceptance checks"},
    )
    monkeypatch.setattr(hooks, "_spawn_background_rebuild", lambda _root: None)
    monkeypatch.setattr(hooks, "_spawn_sleep_time_jobs", lambda _root: {})

    from ai_core import completion_guard

    monkeypatch.setattr(
        completion_guard,
        "guard_directive",
        lambda _payload, _root, **_kwargs: "Run the missing acceptance check.",
    )
    blocked = hooks.handle_hook(
        memory_root,
        "TaskCompleted",
        {
            "agent": "claude",
            "session_id": "s-task",
            "task_id": "task-9",
            "task_subject": "Run acceptance checks",
        },
    )
    assert blocked.get("decision") == "block"
    assert _todo_statuses(memory_root) == ["open"]

    monkeypatch.setattr(completion_guard, "guard_directive", lambda _payload, _root, **_kwargs: None)
    allowed = hooks.handle_hook(
        memory_root,
        "TaskCompleted",
        {
            "agent": "claude",
            "session_id": "s-task",
            "task_id": "task-9",
            "task_subject": "Run acceptance checks",
        },
    )
    assert allowed.get("decision") != "block"
    assert _todo_statuses(memory_root) == ["open", "done"]


def test_claude_task_and_teammate_blocks_use_exit_code_two() -> None:
    for event in ("TaskCompleted", "TeammateIdle"):
        response = {"hook": event, "decision": "block", "reason": "quality gate failed"}
        request = {"agent": "claude"}
        assert hooks.hook_exit_code(response, request) == 2
        assert hooks.hook_stderr(response, request) == "quality gate failed"
    assert hooks.hook_exit_code({"hook": "TaskCompleted"}, {"agent": "claude"}) == 0


def test_kiro_wire_is_plain_context_or_silent_and_never_blocks_stop() -> None:
    start = {
        "hook": "SessionStart",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "bounded project context",
        },
    }
    assert hooks.hook_wire_output(start, {"agent": "kiro"}) == "bounded project context"
    assert hooks.hook_wire_output({"hook": "PostToolUse"}, {"agent": "kiro"}) is None

    blocked_tool = {"hook": "PreToolUse", "decision": "block", "reason": "unsafe command"}
    assert hooks.hook_exit_code(blocked_tool, {"agent": "kiro"}) == 2
    assert hooks.hook_stderr(blocked_tool, {"agent": "kiro"}) == "unsafe command"

    # Kiro's official Stop trigger is observational and cannot block.  Never return a
    # non-zero code that the host would treat as a generic hook failure.
    blocked_stop = {"hook": "Stop", "decision": "block", "reason": "unfinished"}
    assert hooks.hook_exit_code(blocked_stop, {"agent": "kiro"}) == 0


def test_stop_failure_is_audited_with_current_error_fields(memory_root: Path) -> None:
    hooks._handle_lifecycle_event(
        memory_root,
        "StopFailure",
        {
            "session_id": "failed-session",
            "agent": "claude",
            "error": "rate_limit",
            "error_details": "429 Too Many Requests",
        },
    )
    audit = next((memory_root / ".ai" / "memory" / "audit").glob("*.jsonl")).read_text(
        encoding="utf-8"
    )
    assert "session.stop_failure" in audit
    assert "rate_limit" in audit
