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


def test_pretooluse_codex_exec_command_blocks() -> None:
    result = run_hook(
        "PreToolUse",
        {
            "agent": "codex",
            "tool_name": "functions.exec_command",
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


# ---------------------------------------------------------------------------
# T43: importance-scaled exponential decay (_cooldown_score)
# ---------------------------------------------------------------------------

def test_cooldown_score_importance_baseline_matches_legacy() -> None:
    """importance=1.0 must reproduce the legacy 2-arg result bit-for-bit."""
    from ai_core.hooks import _cooldown_score

    for age, hl in [(0.0, 12.0), (6.0, 12.0), (12.0, 12.0), (24.0, 12.0), (48.0, 6.0)]:
        legacy = _cooldown_score(age, hl)
        scaled = _cooldown_score(age, hl, importance=1.0)
        assert legacy == scaled, f"age={age} hl={hl}: legacy={legacy} scaled={scaled}"
    # Edge cases: disabled half-life and zero age behave identically.
    assert _cooldown_score(10.0, 0.0, importance=1.0) == 0.0
    assert _cooldown_score(0.0, 12.0, importance=1.0) == 1.0


def test_cooldown_score_importance_high_slows_decay() -> None:
    """importance>1.0 means longer effective half-life → larger score for same age."""
    from ai_core.hooks import _cooldown_score

    age, hl = 12.0, 12.0  # exactly one half-life
    baseline = _cooldown_score(age, hl, importance=1.0)
    high = _cooldown_score(age, hl, importance=2.0)
    assert high > baseline, f"high={high} should beat baseline={baseline}"
    # 2x half-life → score = 0.5 ** (12 / 24) = sqrt(0.5)
    assert abs(high - (0.5 ** 0.5)) < 1e-9


def test_cooldown_score_importance_low_speeds_decay() -> None:
    """importance<1.0 means shorter effective half-life → smaller score."""
    from ai_core.hooks import _cooldown_score

    age, hl = 12.0, 12.0
    baseline = _cooldown_score(age, hl, importance=1.0)
    low = _cooldown_score(age, hl, importance=0.5)
    assert low < baseline, f"low={low} should be below baseline={baseline}"
    # half_life *= 0.5 → score = 0.5 ** (12 / 6) = 0.25
    assert abs(low - 0.25) < 1e-9


def test_cooldown_score_importance_clamp_floor() -> None:
    """importance<=0 must clamp to 0.1 floor and not divide by zero."""
    from ai_core.hooks import _cooldown_score

    # Should not raise.
    zero = _cooldown_score(12.0, 12.0, importance=0.0)
    neg = _cooldown_score(12.0, 12.0, importance=-5.0)
    # Floor 0.1 → score = 0.5 ** (12 / 1.2) = 0.5 ** 10
    expected = 0.5 ** 10
    assert abs(zero - expected) < 1e-9, f"zero importance score {zero} != {expected}"
    assert abs(neg - expected) < 1e-9, f"negative importance score {neg} != {expected}"


# ---------------------------------------------------------------------------
# T44: PostToolUse updatedToolOutput redaction
# ---------------------------------------------------------------------------

def test_post_tool_use_redacts_secret_into_updated_output() -> None:
    """A secret in tool_response surfaces via hookSpecificOutput.updatedToolOutput."""
    fake_secret = "sk-ant-" + "1234567890abcdefghijABCDEFGHIJKLMNOP"
    result = run_hook(
        "PostToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "env"},
            "tool_response": f"OPENAI_API_KEY={fake_secret}",
        },
    )
    payload = _parse_ok(result)
    hso = payload.get("hookSpecificOutput")
    assert isinstance(hso, dict), f"hookSpecificOutput missing: {payload}"
    assert "updatedToolOutput" in hso, f"updatedToolOutput missing: {hso}"
    cleaned = hso["updatedToolOutput"]
    assert isinstance(cleaned, str)
    assert fake_secret not in cleaned
    assert "[REDACTED]" in cleaned


def test_post_tool_use_no_updated_output_when_clean() -> None:
    """Plain output that survives redact_value unchanged should not add updatedToolOutput."""
    result = run_hook(
        "PostToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
            "tool_response": "hello world",
        },
    )
    payload = _parse_ok(result)
    hso = payload.get("hookSpecificOutput")
    if isinstance(hso, dict):
        assert "updatedToolOutput" not in hso, f"unexpected updatedToolOutput: {hso}"


def test_post_tool_use_disabled_via_env() -> None:
    """AI_HOOK_REDACT_TOOL_OUTPUT=0 disables the updatedToolOutput injection."""
    fake_secret = "sk-ant-" + "1234567890abcdefghijABCDEFGHIJKLMNOP"
    result = run_hook(
        "PostToolUse",
        {
            "agent": "claude",
            "tool_name": "Bash",
            "tool_input": {"command": "env"},
            "tool_response": f"OPENAI_API_KEY={fake_secret}",
        },
        env_extra={"AI_HOOK_REDACT_TOOL_OUTPUT": "0"},
    )
    payload = _parse_ok(result)
    hso = payload.get("hookSpecificOutput")
    if isinstance(hso, dict):
        assert "updatedToolOutput" not in hso, f"updatedToolOutput should be absent: {hso}"


# ---------------------------------------------------------------------------
# T16: recommendation cache dependencies
# ---------------------------------------------------------------------------

def test_recommend_memory_deps_include_all_audit_session_and_codex_global(tmp_path: Path) -> None:
    """Hot recommendation caches must invalidate on every memory input they mine."""
    from ai_core.hooks import _recommend_memory_deps

    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    (audit_dir / "2025.jsonl").write_text("", encoding="utf-8")
    (audit_dir / "2026.jsonl").write_text("", encoding="utf-8")

    deps = _recommend_memory_deps(tmp_path, include_todos=True, include_codex_global=True)
    rels = {p.relative_to(tmp_path).as_posix() for p in deps if p.is_relative_to(tmp_path)}
    outside = {str(p) for p in deps if not p.is_relative_to(tmp_path)}

    assert ".ai/memory/audit-index.jsonl" in rels
    assert ".ai/memory/audit/2025.jsonl" in rels
    assert ".ai/memory/audit/2026.jsonl" in rels
    assert ".ai/memory/session-current.md" in rels
    assert ".ai/memory/todos.jsonl" in rels
    assert any(path.endswith(".codex/memories/raw_memories.md") for path in outside)
