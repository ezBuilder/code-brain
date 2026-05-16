from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def run_hook(
    hook_name: str,
    payload: dict,
    *,
    cwd: Path = ROOT,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        merged.pop(name, None)
    merged["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    if env_extra:
        merged.update(env_extra)
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", "hook", hook_name, "--json"],
        cwd=cwd,
        env=merged,
        text=True,
        input=json.dumps({**payload, "dry": True}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _parse_ok(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_pretooluse_grep_recursive_blocks() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn useEffect src/"},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("decision") == "block"
    assert payload.get("precall", {}).get("binary") == "grep"
    assert ".ai/bin/ai exec run" in payload.get("reason", "")


def test_pretooluse_grep_single_file_allows() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "grep pattern file.txt"},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("precall", {}).get("action") == "allow"
    assert payload.get("decision") != "block"
    assert payload["additional_context_bytes"] == 0


def test_pretooluse_rg_blocks() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "rg pattern"},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("decision") == "block"
    assert payload.get("precall", {}).get("binary") == "rg"


def test_pretooluse_find_blocks() -> None:
    cmd = 'find . -name "*.py"'
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("decision") == "block"
    assert payload.get("precall", {}).get("binary") == "find"
    suggestion = payload.get("precall", {}).get("suggestion") or ""
    assert cmd in suggestion


def test_pretooluse_with_pipe_head_blocks() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn pattern src/ | head -20"},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("decision") == "block"
    assert payload.get("precall", {}).get("binary") == "grep"


def test_pretooluse_non_bash_tool_allows() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("precall", {}).get("action") == "allow"
    assert payload.get("decision") != "block"


def test_pretooluse_decision_message_includes_sandbox_pointer() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn foo src/"},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("decision") == "block"
    reason = payload.get("reason", "")
    assert ".ai/bin/ai exec run" in reason
    assert "sandbox_execute" in reason


def test_pretooluse_hot_path_under_200ms() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn foo src/"},
        },
    )
    payload = _parse_ok(result)
    assert payload.get("elapsed_ms", 9999) <= 200


def test_pretooluse_default_output_is_codex_wire_shape() -> None:
    merged = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        merged.pop(name, None)
    merged["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    result = subprocess.run(
        [PYTHON, "-m", "ai_core.cli", "hook", "PreToolUse"],
        cwd=ROOT,
        env=merged,
        text=True,
        input=json.dumps(
            {
                "agent": "codex",
                "dry": True,
                "tool_name": "Bash",
                "tool_input": {"command": "rg pattern"},
            }
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload = _parse_ok(result)
    assert set(payload) == {"hookSpecificOutput"}
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert "long_output_binary:rg" in output["permissionDecisionReason"]


def test_posttooluse_default_output_is_silent() -> None:
    result = run_hook(
        "PostToolUse",
        {
            "agent": "codex",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
        },
    )
    payload = _parse_ok(result)
    assert payload["additional_context_bytes"] == 0
    wire = subprocess.run(
        [PYTHON, "-m", "ai_core.cli", "hook", "PostToolUse"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / ".ai" / "runtime" / "src")},
        text=True,
        input=json.dumps(
            {
                "agent": "codex",
                "dry": True,
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/x"},
            }
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert wire.returncode == 0, wire.stderr
    assert json.loads(wire.stdout) == {}
