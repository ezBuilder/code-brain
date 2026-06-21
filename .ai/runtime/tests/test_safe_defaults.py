"""Safe pilot flags flipped to default-ON-unless-explicitly-disabled.

Three read-only / advisory / bounded gates now default ON: UNSET/empty means ON,
and only an explicit "0"/"false"/"no" disables them. These tests pin the new
contract directly on the owning functions plus one end-to-end default-on path each
for the MCP resources surface and the read-triggered directory-context block.

  AI_MCP_RESOURCES        -> mcp_server._resources_enabled (+ resources/list)
  AI_DIR_CONTEXT          -> dir_context.enabled (+ directory_context_for_read)
  AI_MEMORY_CONFLICT_SCAN -> memory_tier.page_out conflict-scan gate

stdlib only, offline, no network/LLM. monkeypatch.delenv/setenv drives each gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import dir_context, mcp_server, memory_tier  # noqa: E402


# Values that MUST disable a default-on gate, and values that keep/turn it on.
_DISABLE = ["0", "false", "no"]
_ENABLE = ["1", "true", "yes", "on"]


# ---- AI_MCP_RESOURCES: _resources_enabled ------------------------------------


def test_mcp_resources_enabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_MCP_RESOURCES", raising=False)
    assert mcp_server._resources_enabled() is True


def test_mcp_resources_enabled_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "")
    assert mcp_server._resources_enabled() is True


@pytest.mark.parametrize("val", _DISABLE)
def test_mcp_resources_disabled_by_explicit_off(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", val)
    assert mcp_server._resources_enabled() is False
    # Case-insensitive: uppercase disables too.
    monkeypatch.setenv("AI_MCP_RESOURCES", val.upper())
    assert mcp_server._resources_enabled() is False


@pytest.mark.parametrize("val", _ENABLE)
def test_mcp_resources_enabled_by_explicit_on(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", val)
    assert mcp_server._resources_enabled() is True


def test_mcp_resources_list_populated_on_default_when_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end default-on path: env UNSET + seeded plan/session -> resources/list
    returns entries (the previously opt-in surface is now active by default)."""
    monkeypatch.delenv("AI_MCP_RESOURCES", raising=False)
    from ai_core import plan_state

    plan_state.init_plan(tmp_path, plan_id="alpha", steps=["one", "two"], title="Alpha")
    session = tmp_path / ".ai" / "memory" / "session-current.md"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("# Session\n\n- did a thing\n", encoding="utf-8")

    resp = mcp_server.handle_request(
        tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    )
    assert resp is not None
    resources = resp["result"]["resources"]
    assert resources, "default-on resources/list must return entries when seeded"
    uris = {r["uri"] for r in resources}
    assert "codebrain://plan/alpha" in uris
    assert "codebrain://session/current" in uris
    assert "codebrain://report/status" in uris


def test_mcp_resources_read_allowed_on_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resources/read serves a body (not the disabled error) when env is UNSET."""
    monkeypatch.delenv("AI_MCP_RESOURCES", raising=False)
    from ai_core import plan_state

    plan_state.init_plan(tmp_path, plan_id="alpha", steps=["one"], title="Alpha")
    resp = mcp_server.handle_request(
        tmp_path,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
         "params": {"uri": "codebrain://plan/alpha"}},
    )
    assert resp is not None
    assert "error" not in resp
    assert resp["result"]["contents"][0]["uri"] == "codebrain://plan/alpha"


def test_mcp_resources_list_empty_when_explicitly_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "0")
    from ai_core import plan_state

    plan_state.init_plan(tmp_path, plan_id="alpha", steps=["one"], title="Alpha")
    resp = mcp_server.handle_request(
        tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    )
    assert resp is not None
    assert resp["result"]["resources"] == []


# ---- AI_DIR_CONTEXT: dir_context.enabled -------------------------------------


def test_dir_context_enabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_DIR_CONTEXT", raising=False)
    assert dir_context.enabled() is True


def test_dir_context_enabled_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_DIR_CONTEXT", "")
    assert dir_context.enabled() is True


@pytest.mark.parametrize("val", _DISABLE)
def test_dir_context_disabled_by_explicit_off(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("AI_DIR_CONTEXT", val)
    assert dir_context.enabled() is False
    monkeypatch.setenv("AI_DIR_CONTEXT", val.upper())
    assert dir_context.enabled() is False


@pytest.mark.parametrize("val", _ENABLE)
def test_dir_context_enabled_by_explicit_on(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("AI_DIR_CONTEXT", val)
    assert dir_context.enabled() is True


def _seed_dir_context(tmp_path: Path) -> Path:
    """Repo with nested AGENTS.md between a deep source file and the root."""
    root = tmp_path
    (root / "AGENTS.md").write_text("root guidance", encoding="utf-8")
    sub = root / "pkg" / "auth"
    sub.mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "AGENTS.md").write_text("pkg guidance", encoding="utf-8")
    (sub / "AGENTS.md").write_text("auth guidance", encoding="utf-8")
    (sub / "login.py").write_text("x = 1\n", encoding="utf-8")
    return root


def test_dir_context_for_read_works_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end default-on path: directory_context_for_read surfaces nested
    AGENTS.md context for a Read payload with the env UNSET (was opt-in before)."""
    monkeypatch.delenv("AI_DIR_CONTEXT", raising=False)
    root = _seed_dir_context(tmp_path)
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")},
        "session_id": "s-default",
    }
    out = dir_context.directory_context_for_read(root, payload)
    assert "auth guidance" in out
    assert "pkg guidance" in out
    assert "Directory Context:" in out


def test_dir_context_for_read_empty_when_explicitly_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_DIR_CONTEXT", "0")
    root = _seed_dir_context(tmp_path)
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")},
        "session_id": "s-off",
    }
    assert dir_context.directory_context_for_read(root, payload) == ""


# ---- AI_MEMORY_CONFLICT_SCAN: memory_tier.page_out gate ----------------------


def test_conflict_scan_runs_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """page_out runs the advisory conflict scan by default (env UNSET): the result
    is present and NOT the disabled-skip marker."""
    monkeypatch.delenv("AI_MEMORY_CONFLICT_SCAN", raising=False)
    payload = memory_tier.page_out(tmp_path, dry_run=True)
    assert "conflict_scan" in payload
    assert payload["conflict_scan"].get("skipped") is not True


def test_conflict_scan_runs_when_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MEMORY_CONFLICT_SCAN", "")
    payload = memory_tier.page_out(tmp_path, dry_run=True)
    assert payload["conflict_scan"].get("skipped") is not True


@pytest.mark.parametrize("val", _DISABLE)
def test_conflict_scan_skipped_by_explicit_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv("AI_MEMORY_CONFLICT_SCAN", val)
    payload = memory_tier.page_out(tmp_path, dry_run=True)
    assert payload["conflict_scan"]["skipped"] is True
    assert "disabled" in payload["conflict_scan"]["reason"]


@pytest.mark.parametrize("val", _ENABLE)
def test_conflict_scan_runs_by_explicit_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv("AI_MEMORY_CONFLICT_SCAN", val)
    payload = memory_tier.page_out(tmp_path, dry_run=True)
    assert payload["conflict_scan"].get("skipped") is not True
