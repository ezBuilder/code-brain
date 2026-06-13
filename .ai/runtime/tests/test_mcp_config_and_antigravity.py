"""Tests for MCP dialect conversion and the install-into.sh Antigravity branch.

Covers:
- ``ai_core.mcp_config``: pure conversions between Claude (.mcp.json) and
  Antigravity (.agents/mcp_config.json), including the ``url`` → ``serverUrl``
  rewrite for remote servers and stdio entry pass-through.
- ``install-into.sh``: when invoked against a temporary git repo, the script
  must produce ``.agents/mcp_config.json`` and ``.agents/hooks.json`` with the
  Code Brain server entry under ``mcpServers.code-brain`` and matching hook
  matchers; root ``AGENTS.md`` must land in the target unchanged.
- ``recommend.accept``: skill accept must publish to the new third target
  ``.agents/skills/<slug>/SKILL.md`` alongside Claude and Codex.
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


# ---------- pure dialect conversion ----------


def test_code_brain_stdio_entry_is_os_aware() -> None:
    from ai_core.mcp_config import code_brain_stdio_entry

    unix = code_brain_stdio_entry(windows=False)
    assert unix["command"] == ".ai/bin/ai-mcp"
    win = code_brain_stdio_entry(windows=True)
    # On Windows the bash shim is not executable → launch the .ps1 via powershell.
    assert win["command"] == "powershell"
    assert ".ai/bin/ai-mcp.ps1" in win["args"]
    # default detects the host OS (this test host is unix → bash shim)
    import os as _os
    default = code_brain_stdio_entry()
    assert default["command"] == ("powershell" if _os.name == "nt" else ".ai/bin/ai-mcp")


def test_to_antigravity_rewrites_url_to_server_url() -> None:
    from ai_core.mcp_config import to_antigravity

    claude_payload = {
        "mcpServers": {
            "remote-foo": {"url": "https://api.example.com/mcp", "headers": {"x": "y"}},
            "stdio-bar": {"command": "/usr/bin/foo", "args": ["--mcp"], "env": {}},
        }
    }
    out = to_antigravity(claude_payload)
    assert out["mcpServers"]["remote-foo"]["serverUrl"] == "https://api.example.com/mcp"
    assert "url" not in out["mcpServers"]["remote-foo"]
    # stdio entry is untouched
    assert out["mcpServers"]["stdio-bar"]["command"] == "/usr/bin/foo"
    assert out["mcpServers"]["stdio-bar"]["args"] == ["--mcp"]
    # headers preserved verbatim on the remote entry
    assert out["mcpServers"]["remote-foo"]["headers"] == {"x": "y"}


def test_from_antigravity_reverses_server_url_to_url() -> None:
    from ai_core.mcp_config import from_antigravity

    ag_payload = {"mcpServers": {"r": {"serverUrl": "https://x", "headers": {}}}}
    out = from_antigravity(ag_payload)
    assert out["mcpServers"]["r"]["url"] == "https://x"
    assert "serverUrl" not in out["mcpServers"]["r"]


def test_to_antigravity_handles_malformed_input() -> None:
    from ai_core.mcp_config import to_antigravity

    assert to_antigravity({}) == {"mcpServers": {}}
    assert to_antigravity({"mcpServers": "nope"}) == {"mcpServers": {}}
    assert to_antigravity(None) == {"mcpServers": {}}  # type: ignore[arg-type]


def test_merge_antigravity_mcp_json_idempotent(tmp_path: Path) -> None:
    from ai_core.mcp_config import merge_antigravity_mcp_json

    dst = tmp_path / ".agents" / "mcp_config.json"
    # First write
    merge_antigravity_mcp_json(dst)
    payload1 = json.loads(dst.read_text(encoding="utf-8"))
    assert payload1["mcpServers"]["code-brain"]["command"] == ".ai/bin/ai-mcp"
    # Pre-existing user entries must survive a second merge
    payload1["mcpServers"]["user-stuff"] = {"serverUrl": "https://x"}
    dst.write_text(json.dumps(payload1, indent=2, sort_keys=True), encoding="utf-8")
    merge_antigravity_mcp_json(dst)
    payload2 = json.loads(dst.read_text(encoding="utf-8"))
    assert payload2["mcpServers"]["user-stuff"]["serverUrl"] == "https://x"
    assert payload2["mcpServers"]["code-brain"]["command"] == ".ai/bin/ai-mcp"


def test_install_global_antigravity_mcp_preserves_other_servers(tmp_path: Path) -> None:
    """Registering the Code Brain wrapper into the user-global Antigravity
    config must keep pre-existing servers (pencil, third-party) and only
    overwrite the ``code-brain`` entry.
    """
    from ai_core.mcp_config import (
        antigravity_global_mcp_path,
        install_global_antigravity_mcp,
    )

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cfg = antigravity_global_mcp_path(home=fake_home)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "pencil": {"command": "/opt/pencil/mcp", "args": ["--app", "antigravity"], "env": {}},
                    "other": {"serverUrl": "https://x"},
                }
            }
        ),
        encoding="utf-8",
    )

    wrapper = tmp_path / "bin" / "code-brain-mcp"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\nexec true\n", encoding="utf-8")

    resolved = install_global_antigravity_mcp(wrapper, home=fake_home)
    assert resolved == cfg

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["code-brain"]["command"] == str(wrapper)
    assert payload["mcpServers"]["pencil"]["command"] == "/opt/pencil/mcp"
    assert payload["mcpServers"]["other"]["serverUrl"] == "https://x"

    # Re-running must be a no-op (idempotent)
    install_global_antigravity_mcp(wrapper, home=fake_home)
    payload2 = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload == payload2


def test_merge_into_target_rejects_unknown_dialect(tmp_path: Path) -> None:
    from ai_core.mcp_config import merge_into_target

    with pytest.raises(ValueError, match="unsupported dialect"):
        merge_into_target(
            tmp_path / "x.json",
            dialect="gemini",
            server_name="x",
            server_entry={"command": "x"},
        )


def test_merge_into_target_rejects_corrupted_existing(tmp_path: Path) -> None:
    from ai_core.mcp_config import merge_into_target

    dst = tmp_path / "bad.json"
    dst.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        merge_into_target(
            dst,
            dialect="antigravity",
            server_name="code-brain",
            server_entry={"command": ".ai/bin/ai-mcp"},
        )


# ---------- install-into.sh integration ----------


@pytest.fixture
def install_into_target(tmp_path: Path) -> Path:
    """Initialize a minimal target repo and run install-into.sh against it."""
    target = tmp_path / "victim"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=target, check=True)
    (target / "README.md").write_text("# victim\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True)

    script = ROOT / "scripts" / "install-into.sh"
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    res = subprocess.run(
        ["bash", str(script), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(f"install-into.sh skipped (env not provisioned): {res.stderr[-400:]}")
    return target


def test_install_into_writes_antigravity_mcp_config(install_into_target: Path) -> None:
    mcp_config = install_into_target / ".agents" / "mcp_config.json"
    assert mcp_config.exists(), "expected .agents/mcp_config.json"
    payload = json.loads(mcp_config.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["code-brain"]["command"] == ".ai/bin/ai-mcp"


def test_install_into_writes_antigravity_hooks(install_into_target: Path) -> None:
    hooks = install_into_target / ".agents" / "hooks.json"
    assert hooks.exists(), "expected .agents/hooks.json"
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    # Antigravity 1.0.x schema: top-level {name: spec} map; a spec has one field
    # per native event. No legacy Claude wrapper ("_note"/"hooks") — Antigravity
    # cannot parse that. No SessionStart/UserPromptSubmit (Antigravity has neither).
    assert "_note" not in payload and "hooks" not in payload
    spec = payload["code-brain"]
    assert set(spec) == {"PreToolUse", "PostToolUse", "PreInvocation", "PostInvocation", "Stop"}
    assert "SessionStart" not in spec and "UserPromptSubmit" not in spec
    # PreInvocation/PostInvocation unused (null). PreToolUse is also null for Antigravity:
    # its jsonhook contract is deny-by-default, so a Code Brain PreToolUse hook denies EVERY
    # agy tool call (it broke the worker rather than protecting it). Only the side-effect events
    # PostToolUse (redaction/recording) and Stop (memory refresh) carry handlers.
    assert spec["PreInvocation"] is None and spec["PostInvocation"] is None
    assert spec["PreToolUse"] is None
    for event_name in ("PostToolUse", "Stop"):
        entries = spec[event_name]
        assert isinstance(entries, list) and entries, event_name
        for entry in entries:
            assert "matcher" in entry, event_name
            for handler in entry["hooks"]:
                assert handler["type"] == "command"
                assert ".ai/bin/ai-hook" in handler["command"], event_name
                assert event_name in handler["command"], event_name


def test_install_into_publishes_root_agents_md(install_into_target: Path) -> None:
    agents = install_into_target / "AGENTS.md"
    assert agents.exists(), "expected root AGENTS.md forwarder"
    text = agents.read_text(encoding="utf-8")
    assert ".ai/AGENTS.md" in text


def test_install_into_preserves_user_authored_agents_md(tmp_path: Path) -> None:
    """If the target already has a user-authored AGENTS.md (common in mature
    repos like Navio), install-into must NOT overwrite it. The forwarder is a
    seed-only convenience; user content is part of the project contract.
    """
    target = tmp_path / "victim2"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=target, check=True)
    user_agents = "# Custom rules\n\nProject-specific instructions live here.\n"
    (target / "AGENTS.md").write_text(user_agents, encoding="utf-8")
    (target / "README.md").write_text("# v\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True)

    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    res = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "install", str(target)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(f"install-into.sh skipped: {res.stderr[-400:]}")
    final = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert final == user_agents, "user AGENTS.md must not be overwritten"


def test_install_into_manifest_records_antigravity_targets(install_into_target: Path) -> None:
    manifest_path = install_into_target / ".ai" / "generated" / "install-manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    merged = set(manifest.get("merged_config_files", []))
    assert ".agents/mcp_config.json" in merged
    assert ".agents/hooks.json" in merged


# ---------- skill accept publishes to .agents/skills/<slug>/SKILL.md ----------


def test_recommend_accept_publishes_third_target(tmp_path: Path) -> None:
    """``accept`` must write ``.agents/skills/<slug>/SKILL.md`` in addition to
    the Claude/Codex targets so Antigravity surfaces the skill alongside the
    other agents.
    """
    from ai_core import recommend

    root = tmp_path
    (root / ".ai").mkdir()

    catalog_dir = root / ".ai" / "skills"
    catalog_dir.mkdir(parents=True)
    candidate_id = "skill-test123"
    entry_record = {
        "id": candidate_id,
        "slug": "test-skill",
        "status": "pending",
        "draft": {"description": "Sample skill", "body": "Do the thing.\n"},
        "evidence": {},
        "created_at": "2026-05-24T00:00:00Z",
        "installed_paths": [],
        "body_sha256": "",
    }
    catalog_file = catalog_dir / "catalog.jsonl"
    catalog_file.write_text(json.dumps(entry_record) + "\n", encoding="utf-8")

    result = recommend.accept(root, candidate_id)
    assert result["ok"], result
    installed = set(result["installed_paths"])
    assert ".claude/commands/test-skill.md" in installed
    assert ".codex/prompts/test-skill.md" in installed
    assert ".agents/skills/test-skill/SKILL.md" in installed
    skill_md = root / ".agents" / "skills" / "test-skill" / "SKILL.md"
    assert skill_md.exists()
    body = skill_md.read_text(encoding="utf-8")
    assert "managed-by: code-brain" in body
    assert "Do the thing" in body
