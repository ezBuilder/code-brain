from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import doctor  # noqa: E402


def _command(event: str) -> list[dict[str, object]]:
    entry: dict[str, object] = {
        "type": "command", "command": f".ai/bin/ai-hook {event}", "timeout": 2,
    }
    if event in {"SessionStart", "SubagentStart"}:
        entry["additionalContextLimit"] = 5000
    elif event == "UserPromptSubmit":
        entry["additionalContextLimit"] = 2500
    group: dict[str, object] = {"hooks": [entry]}
    if event == "PreToolUse":
        group["matcher"] = "apply_patch|Edit|Write"
    return [group]


def test_codex_interrupt_is_version_gated(tmp_path: Path, monkeypatch) -> None:
    hooks = {
        event: _command(event)
        for event in (
            "PreToolUse",
            "PostToolUse",
            "SessionStart",
            "Stop",
            "SubagentStop",
            "SessionEnd",
            "Interrupt",
        )
    }
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    monkeypatch.setattr(doctor, "_command_semver", lambda binary: (0, 147, 0) if binary == "codex" else None)
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "cannot parse Interrupt" in check.detail


def test_codex_current_version_requires_interrupt(tmp_path: Path, monkeypatch) -> None:
    hooks = {
        event: _command(event)
        for event in (
            "PreToolUse",
            "PostToolUse",
            "SessionStart",
            "Stop",
            "SubagentStop",
            "SessionEnd",
        )
    }
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    monkeypatch.setattr(doctor, "_command_semver", lambda binary: (0, 150, 1) if binary == "codex" else None)
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "Interrupt" in check.detail


def test_kiro_reports_real_active_surface_and_advisory_stop(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {
            "name": f"Code Brain {event}",
            "trigger": event,
            "enabled": True,
            "timeout": 2,
                "action": {
                    "type": "command",
                    "command": f"AI_HOOK_AGENT=kiro .ai/bin/ai-hook {event}",
                },
            }
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
    ]
    path = tmp_path / ".kiro" / "hooks" / "code-brain.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": "v1", "hooks": rows}), encoding="utf-8")
    monkeypatch.setattr(doctor, "_command_semver", lambda binary: (2, 19, 2) if binary == "kiro-cli" else None)

    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is True
    assert "kiro=5 active" in check.detail
    assert "surface=IDE/v3" in check.detail
    assert "stop=advisory" in check.detail


def test_antigravity_count_excludes_null_placeholders(tmp_path: Path) -> None:
    spec = {
        "PreToolUse": None,
        "PostToolUse": _command("PostToolUse"),
        "PreInvocation": _command("PreInvocation"),
        "PostInvocation": None,
        "Stop": _command("Stop"),
    }
    path = tmp_path / ".agents" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"code-brain": spec}), encoding="utf-8")

    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is True
    assert "antigravity=3/5 active" in check.detail
    assert "disabled=PostInvocation,PreToolUse" in check.detail


def test_managed_command_timeout_and_pretool_matcher_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": ".ai/bin/ai-hook PreToolUse", "timeout": 6}]}],
        "PostToolUse": _command("PostToolUse"),
        "SessionStart": [{"hooks": [{"type": "command", "command": ".ai/bin/ai-hook SessionStart"}]}],
    }
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    monkeypatch.setattr(doctor, "_command_semver", lambda _binary: None)
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "timeout missing" in check.detail
    assert "timeout 6>5s" in check.detail
    assert "matcher must include" in check.detail


def test_managed_timeout_limits_allow_non_code_brain_and_missing_hosts(tmp_path: Path) -> None:
    hooks = {
        "SessionEnd": [{"hooks": [{"type": "command", "command": ".ai/bin/ai-hook SessionEnd", "timeout": 3}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "user-hook", "timeout": 99}]}],
    }
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "SessionEnd" not in check.detail  # missing-event capability is unrelated here
    assert "timeout" not in check.detail


def test_kiro_reads_timeout_from_hook_row_not_action(tmp_path: Path) -> None:
    path = tmp_path / ".kiro" / "hooks" / "code-brain.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": "v1",
        "hooks": [{
            "name": "Code Brain PostToolUse",
            "trigger": "PostToolUse",
            "timeout": 3,
            "action": {"type": "command", "command": ".ai/bin/ai-hook PostToolUse", "timeout_ms": 1},
        }],
    }), encoding="utf-8")
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "kiro PostToolUse Code Brain command hook timeout 3>2s" in check.detail


def test_kiro_tool_matcher_may_be_omitted_for_all_tools(tmp_path: Path) -> None:
    path = tmp_path / ".kiro" / "hooks" / "code-brain.json"
    path.parent.mkdir(parents=True)
    rows = [
        {
            "name": f"Code Brain {event}",
            "trigger": event,
            "timeout": 5 if event == "PreToolUse" else 2,
            "action": {"type": "command", "command": f".ai/bin/ai-hook {event}"},
        }
        for event in ("PreToolUse", "PostToolUse")
    ]
    path.write_text(json.dumps({"version": "v1", "hooks": rows}), encoding="utf-8")
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False  # missing other required Kiro events, but no matcher issue
    assert "Code Brain matcher must be omitted" not in check.detail


def test_kiro_tool_matcher_rejects_regex_wildcard_and_narrow_filters(tmp_path: Path) -> None:
    path = tmp_path / ".kiro" / "hooks" / "code-brain.json"
    path.parent.mkdir(parents=True)
    rows = [
        {
            "name": "Code Brain PreToolUse",
            "trigger": "PreToolUse",
            "matcher": "*",
            "timeout": 5,
            "action": {"type": "command", "command": ".ai/bin/ai-hook PreToolUse"},
        },
        {
            "name": "Code Brain PostToolUse",
            "trigger": "PostToolUse",
            "matcher": "shell|write|.*",
            "timeout": 2,
            "action": {"type": "command", "command": ".ai/bin/ai-hook PostToolUse"},
        },
    ]
    path.write_text(json.dumps({"version": "v1", "hooks": rows}), encoding="utf-8")
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "kiro PreToolUse Code Brain matcher must be omitted for always-match" in check.detail
    assert "kiro PostToolUse Code Brain matcher must be omitted for always-match" in check.detail


def test_context_limits_are_required_only_on_context_producers(tmp_path: Path) -> None:
    hooks = {
        "SessionStart": [{"hooks": [{"type": "command", "command": ".ai/bin/ai-hook SessionStart", "timeout": 2, "additionalContextLimit": 0}]}],
        "SubagentStart": [{"hooks": [{"type": "command", "command": ".ai/bin/ai-hook SubagentStart", "timeout": 2, "additionalContextLimit": 5000}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": ".ai/bin/ai-hook UserPromptSubmit", "timeout": 2, "additionalContextLimit": 2500}]}],
        "PostToolUse": [{"hooks": [{"type": "command", "command": ".ai/bin/ai-hook PostToolUse", "timeout": 2, "additionalContextLimit": 0}]}],
    }
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "codex SessionStart Code Brain command hook additionalContextLimit missing/zero" in check.detail
    assert "PostToolUse Code Brain command hook additionalContextLimit" not in check.detail


def test_kiro_zero_and_negative_timeout_fail(tmp_path: Path) -> None:
    path = tmp_path / ".kiro" / "hooks" / "code-brain.json"
    path.parent.mkdir(parents=True)
    rows = [
        {"name": f"Code Brain {event}", "trigger": event, "timeout": timeout,
         "action": {"type": "command", "command": f".ai/bin/ai-hook {event}"}}
        for event, timeout in (("PostToolUse", 0), ("SessionStart", -1))
    ]
    path.write_text(json.dumps({"version": "v1", "hooks": rows}), encoding="utf-8")
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "kiro PostToolUse Code Brain command hook timeout missing" in check.detail
    assert "kiro SessionStart Code Brain command hook timeout missing" in check.detail


def test_claude_pretool_matcher_requires_edit_and_write_not_apply_patch(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    hooks = {"PreToolUse": [{"matcher": "Edit|Write", "hooks": [{
        "type": "command", "command": ".ai/bin/ai-hook PreToolUse", "timeout": 5,
    }]}]}
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert "Claude missing active hooks" in check.detail
    assert "Claude PreToolUse Code Brain matcher must include" not in check.detail


def test_version_gates_do_not_require_unknown_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    codex.parent.mkdir(parents=True)
    core = {event: _command(event) for event in ("PreToolUse", "PostToolUse", "SessionStart", "Stop", "SubagentStop")}
    codex.write_text(json.dumps({"hooks": core}), encoding="utf-8")
    monkeypatch.setattr(doctor, "_command_semver", lambda _binary: None)
    assert doctor.check_hook_capabilities(tmp_path).ok is True

    claude = tmp_path / ".claude" / "settings.json"
    claude.parent.mkdir(parents=True)
    claude.write_text(json.dumps({"hooks": core}), encoding="utf-8")
    assert doctor.check_hook_capabilities(tmp_path).ok is True


@pytest.mark.parametrize(
    ("version", "event"),
    [
        ((2, 1, 33), "TaskCompleted"), ((2, 1, 78), "StopFailure"),
        ((2, 1, 83), "CwdChanged"), ((2, 1, 83), "FileChanged"),
        ((2, 1, 84), "TaskCreated"), ((0, 117, 0), "SessionEnd"),
        ((0, 150, 0), "Interrupt"),
    ],
)
def test_version_gate_boundaries_require_new_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int], event: str
) -> None:
    is_codex = version[0] == 0
    path = tmp_path / (".codex" if is_codex else ".claude") / ("hooks.json" if is_codex else "settings.json")
    path.parent.mkdir(parents=True)
    core = {name: _command(name) for name in ("PreToolUse", "PostToolUse", "SessionStart", "Stop", "SubagentStop")}
    path.write_text(json.dumps({"hooks": core}), encoding="utf-8")
    monkeypatch.setattr(doctor, "_command_semver", lambda binary: version if binary == ("codex" if is_codex else "claude") else None)
    check = doctor.check_hook_capabilities(tmp_path)
    assert check.ok is False
    assert event in check.detail
