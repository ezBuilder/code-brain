"""Tests for P1 hook event handlers: SubagentStart, TaskCreated/Completed,
FileChanged, PostToolUseFailure, plus install-into.sh registration of those
events across Claude/Codex/Antigravity hook config files.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True)
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return tmp_path


# ---------- HOOK SET MEMBERSHIP ----------


def test_subagent_start_is_context_injection_hook() -> None:
    from ai_core.hooks import CONTEXT_INJECTION_HOOKS, INJECTION_HOOKS

    assert "SubagentStart" in CONTEXT_INJECTION_HOOKS
    assert "SubagentStart" in INJECTION_HOOKS


def test_file_changed_triggers_auto_rebuild() -> None:
    from ai_core.hooks import AUTO_REBUILD_HOOKS

    assert "FileChanged" in AUTO_REBUILD_HOOKS


# ---------- LIFECYCLE EVENT DISPATCH ----------


def test_subagent_start_writes_audit(tmp_root: Path) -> None:
    from ai_core.hooks import _handle_lifecycle_event

    _handle_lifecycle_event(
        tmp_root,
        "SubagentStart",
        {"agent_id": "sub-42", "agent_type": "Explore"},
    )
    audit = (tmp_root / ".ai" / "memory" / "audit" / "2026.jsonl").read_text(encoding="utf-8")
    assert "subagent.started" in audit
    assert "sub-42" in audit
    assert "Explore" in audit


def test_task_created_records_todo(tmp_root: Path) -> None:
    from ai_core.hooks import _handle_lifecycle_event

    _handle_lifecycle_event(
        tmp_root,
        "TaskCreated",
        {"title": "Wire SubagentStart context"},
    )
    todos = (tmp_root / ".ai" / "memory" / "todos.jsonl").read_text(encoding="utf-8")
    assert "Wire SubagentStart context" in todos
    parsed = [json.loads(ln) for ln in todos.splitlines() if ln.strip()]
    assert any(p.get("status") == "open" for p in parsed)


def test_task_completed_closes_matching_todo(tmp_root: Path) -> None:
    from ai_core.hooks import _handle_lifecycle_event

    # Seed: create then complete
    _handle_lifecycle_event(tmp_root, "TaskCreated", {"title": "Add P1 audit"})
    _handle_lifecycle_event(tmp_root, "TaskCompleted", {"title": "Add P1 audit"})
    todos = (tmp_root / ".ai" / "memory" / "todos.jsonl").read_text(encoding="utf-8")
    statuses = [
        json.loads(line)["status"]
        for line in todos.splitlines()
        if line.strip()
    ]
    # Both the original "open" record AND the close record must exist
    assert "open" in statuses
    assert "done" in statuses


def test_task_completed_without_match_is_no_op(tmp_root: Path) -> None:
    from ai_core.hooks import _handle_lifecycle_event

    # No prior todo; close should silently do nothing rather than raise
    _handle_lifecycle_event(tmp_root, "TaskCompleted", {})
    # No todos file expected
    assert not (tmp_root / ".ai" / "memory" / "todos.jsonl").exists()


def test_file_changed_writes_audit(tmp_root: Path) -> None:
    from ai_core.hooks import _handle_lifecycle_event

    _handle_lifecycle_event(
        tmp_root,
        "FileChanged",
        {"file_path": "src/foo.py"},
    )
    audit = (tmp_root / ".ai" / "memory" / "audit" / "2026.jsonl").read_text(encoding="utf-8")
    assert "file.changed" in audit
    assert "src/foo.py" in audit


def test_post_tool_use_failure_writes_audit(tmp_root: Path) -> None:
    from ai_core.hooks import _handle_lifecycle_event

    _handle_lifecycle_event(
        tmp_root,
        "PostToolUseFailure",
        {"tool_name": "Bash", "error": "exit 127: command not found"},
    )
    audit = (tmp_root / ".ai" / "memory" / "audit" / "2026.jsonl").read_text(encoding="utf-8")
    assert "tool.failed" in audit
    assert "Bash" in audit


# ---------- INSTALL-INTO.SH REGISTRATION OF P1 EVENTS ----------


@pytest.fixture
def install_into_target(tmp_path: Path) -> Path:
    target = tmp_path / "victim"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=target, check=True)
    (target / "README.md").write_text("# v\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True)
    script = ROOT / "scripts" / "install-into.sh"
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    res = subprocess.run(
        ["bash", str(script), "install", str(target)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(f"install-into.sh skipped: {res.stderr[-400:]}")
    return target


def test_upgrade_prunes_orphan_commands(install_into_target: Path) -> None:
    """ai upgrade removes CB-managed commands no longer shipped (manifest-gated), while keeping
    currently-shipped commands AND user-authored files (never recorded in the manifest)."""
    target = install_into_target
    prompts = target / ".codex" / "prompts"
    # Realistic state: orphans are NOT in the manifest (earlier upgrades rewrote it to current
    # source only). Prune must still catch them via the cb- namespace + retired-legacy list.
    orphan = prompts / "cb-loop.md"
    orphan.write_text("retired loop junk\n", encoding="utf-8")        # cb- namespace orphan
    legacy = prompts / "git-runbook.md"
    legacy.write_text("retired legacy\n", encoding="utf-8")           # retired non-cb basename
    user_cmd = prompts / "my-custom.md"
    user_cmd.write_text("user command\n", encoding="utf-8")           # user file, NOT pruned
    shipped = prompts / "cb-doctor.md"
    assert shipped.is_file(), "precondition: cb-doctor is a shipped command"
    res = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "upgrade", str(target)],
        cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 0, res.stderr[-400:]
    assert not orphan.exists(), "cb- namespace orphan should be pruned"
    assert not legacy.exists(), "retired legacy command should be pruned"
    assert shipped.is_file(), "shipped command must survive prune"
    assert user_cmd.is_file(), "user-authored file must never be pruned"


def test_claude_settings_registers_p1_events(install_into_target: Path) -> None:
    settings = json.loads(
        (install_into_target / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    hooks = settings.get("hooks", {})
    for ev in (
        "SubagentStart",
        "TaskCreated",
        "TaskCompleted",
        "FileChanged",
        "PostToolUseFailure",
    ):
        assert ev in hooks, f"missing Claude hook: {ev}"


def test_codex_hooks_registers_subagent_start(install_into_target: Path) -> None:
    hooks = json.loads(
        (install_into_target / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )
    assert "SubagentStart" in hooks.get("hooks", {})


def test_codex_hooks_only_top_level_hooks_key(install_into_target: Path) -> None:
    """Codex's parser rejects any top-level key other than `hooks` (e.g. a `_note`
    annotation → 'unknown field _note, expected hooks'). The generated file must carry
    `hooks` and nothing else."""
    payload = json.loads(
        (install_into_target / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )
    assert list(payload.keys()) == ["hooks"], payload.keys()
    assert "_note" not in payload


def test_antigravity_hooks_uses_native_events(install_into_target: Path) -> None:
    # Antigravity has no SubagentStart/SessionStart. Its command hooks live under a
    # top-level named-hook map whose spec exposes the five native event fields.
    payload = json.loads(
        (install_into_target / ".agents" / "hooks.json").read_text(encoding="utf-8")
    )
    assert "hooks" not in payload  # not the Claude-style {"hooks": {...}} wrapper
    spec = payload["code-brain"]
    assert set(spec) == {"PreToolUse", "PostToolUse", "PreInvocation", "PostInvocation", "Stop"}
    assert "SubagentStart" not in spec
