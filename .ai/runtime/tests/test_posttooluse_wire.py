"""Regression: PostToolUse `updatedToolOutput` must be a string on the Codex wire.

A dict/list tool_response (e.g. exec_command's {"stdout": ...}) that contains a
secret used to be redacted into a *dict* and placed in `updatedToolOutput`,
which Codex/Claude Code reject as "invalid post-tool-use JSON output", breaking
every tool call whose structured output held a redactable token.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.hooks import codex_wire_output, handle_hook  # noqa: E402

SECRET = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"


def _wire(tmp_path: Path, tool_response) -> dict:
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "wire-test",
        "tool_name": "Bash",
        "tool_input": {"command": "cat token"},
        "tool_response": tool_response,
    }
    return codex_wire_output(handle_hook(tmp_path, "PostToolUse", payload))


def test_dict_tool_response_never_emits_nonstring_updated_output(tmp_path: Path) -> None:
    wire = _wire(tmp_path, {"stdout": SECRET, "exit_code": 0})
    uto = wire.get("hookSpecificOutput", {}).get("updatedToolOutput")
    # Either omitted, or a string — never a dict/list.
    assert uto is None or isinstance(uto, str)


def test_string_tool_response_redacts_into_string(tmp_path: Path) -> None:
    wire = _wire(tmp_path, f"login token {SECRET} done")
    uto = wire.get("hookSpecificOutput", {}).get("updatedToolOutput")
    assert isinstance(uto, str)
    assert SECRET not in uto and "[REDACTED]" in uto


def test_clean_output_emits_empty_wire(tmp_path: Path) -> None:
    wire = _wire(tmp_path, {"stdout": "all good", "exit_code": 0})
    # Nothing to redact -> no hookSpecificOutput action.
    assert wire == {}


def test_posttooluse_never_emits_block_decision(tmp_path: Path) -> None:
    # A blockable stream-guard match in *already-executed* tool output must not
    # turn into decision=block (pointless post-hoc, and Codex rejects the shape).
    private_key = "-----BEGIN PRIVATE KEY-----\nMIIabc123\n-----END PRIVATE KEY-----"
    wire = _wire(tmp_path, {"stdout": private_key, "exit_code": 0})
    assert wire.get("decision") != "block"
    # Whatever is returned, updatedToolOutput (if present) is still a string.
    uto = wire.get("hookSpecificOutput", {}).get("updatedToolOutput")
    assert uto is None or isinstance(uto, str)
