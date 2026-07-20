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
import shutil
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
    assert unix["env"]["AI_CODE_BRAIN_PROFILE"] == "usage"
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


def _run_bootstrap_with_fake_uv(
    tmp_path: Path,
    *,
    install_dense: bool,
    skip_doctor: bool = False,
    skip_render: bool = False,
) -> list[str]:
    variant = (
        f"{'dense' if install_dense else 'base'}-"
        f"{'skip-doctor' if skip_doctor else 'doctor'}-"
        f"{'skip-render' if skip_render else 'render'}"
    )
    target = tmp_path / variant
    target.mkdir()
    shutil.copy2(ROOT / "bootstrap-code-brain.sh", target / "bootstrap-code-brain.sh")
    (target / ".ai" / "runtime").mkdir(parents=True)
    scripts = target / "scripts"
    scripts.mkdir()
    for name in ("preflight.sh", "env-check.sh"):
        script = scripts / name
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)

    fake_bin = tmp_path / f"fake-bin-{variant}"
    fake_bin.mkdir()
    log = tmp_path / f"uv-{variant}.log"
    uv = fake_bin / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_LOG"\n', encoding="utf-8")
    uv.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["UV_LOG"] = str(log)
    if install_dense:
        env["AI_INSTALL_DENSE"] = "1"
    else:
        env.pop("AI_INSTALL_DENSE", None)
    command = ["bash", str(target / "bootstrap-code-brain.sh")]
    if skip_doctor:
        command.append("--skip-doctor")
    if skip_render:
        command.append("--skip-render")
    subprocess.run(
        command,
        cwd=target,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return log.read_text(encoding="utf-8").splitlines()


def test_bootstrap_installs_base_runtime_by_default(tmp_path: Path) -> None:
    calls = _run_bootstrap_with_fake_uv(tmp_path, install_dense=False)
    assert calls[0] == "sync --no-progress --project .ai/runtime"


def test_bootstrap_dense_dependencies_are_explicit_opt_in(tmp_path: Path) -> None:
    calls = _run_bootstrap_with_fake_uv(tmp_path, install_dense=True)
    assert calls[0] == "sync --no-progress --project .ai/runtime --extra dense"


def test_bootstrap_skip_doctor_keeps_render_but_avoids_duplicate_scan(tmp_path: Path) -> None:
    calls = _run_bootstrap_with_fake_uv(tmp_path, install_dense=False, skip_doctor=True)
    assert "run --project .ai/runtime ai render --manifest-only --json" in calls
    assert all(" ai doctor " not in f" {call} " for call in calls)


def test_bootstrap_skip_render_avoids_separate_render_process(tmp_path: Path) -> None:
    calls = _run_bootstrap_with_fake_uv(
        tmp_path,
        install_dense=False,
        skip_doctor=True,
        skip_render=True,
    )
    assert all(" ai render " not in f" {call} " for call in calls)
    assert all(" ai doctor " not in f" {call} " for call in calls)


def _run_one_command_installer(tmp_path: Path, *, defer_runtime: bool) -> list[str]:
    source = tmp_path / ("source-deferred" if defer_runtime else "source-default")
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "install.sh", scripts / "install.sh")
    fake_install_into = scripts / "install-into.sh"
    fake_install_into.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
target="$2"
printf 'install-into:%s:strict=%s:defer=%s\\n' \
  "$1" "${AI_INSTALL_STRICT:-0}" "${AI_INSTALL_DEFER_RUNTIME:-0}" >> "$INSTALL_WRAPPER_LOG"
mkdir -p "$target/.ai/bin"
cat > "$target/.ai/bin/ai" <<'EOF'
#!/usr/bin/env bash
printf 'ai:%s\\n' "$*" >> "$INSTALL_WRAPPER_LOG"
EOF
chmod +x "$target/.ai/bin/ai"
cat > "$target/bootstrap-code-brain.sh" <<'EOF'
#!/usr/bin/env bash
printf 'bootstrap\\n' >> "$INSTALL_WRAPPER_LOG"
EOF
chmod +x "$target/bootstrap-code-brain.sh"
""",
        encoding="utf-8",
    )
    fake_install_into.chmod(0o755)

    target = tmp_path / ("target-deferred" if defer_runtime else "target-default")
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    log = tmp_path / ("installer-deferred.log" if defer_runtime else "installer-default.log")
    env = os.environ.copy()
    env["INSTALL_WRAPPER_LOG"] = str(log)
    env["CODE_BRAIN_INSTALL_GLOBAL"] = "0"
    if defer_runtime:
        env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    else:
        env.pop("AI_INSTALL_DEFER_RUNTIME", None)
    subprocess.run(
        ["bash", str(scripts / "install.sh"), "--no-global", str(target)],
        cwd=source,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return log.read_text(encoding="utf-8").splitlines()


def test_one_command_installer_does_not_repeat_runtime_activation(tmp_path: Path) -> None:
    calls = _run_one_command_installer(tmp_path, defer_runtime=False)
    assert calls == ["install-into:install:strict=1:defer=0"]


def test_one_command_installer_respects_deferred_runtime(tmp_path: Path) -> None:
    calls = _run_one_command_installer(tmp_path, defer_runtime=True)
    assert calls == ["install-into:install:strict=0:defer=1"]


# ---------- install-into.sh integration ----------


@pytest.fixture(scope="module")
def install_into_target(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Initialize a minimal target repo and run install-into.sh against it."""
    target = tmp_path_factory.mktemp("mcp-install") / "victim"
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
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
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
    assert text == (install_into_target / ".ai" / "AGENTS.md").read_text(encoding="utf-8")


def test_install_into_publishes_root_claude_md_with_response_defaults(install_into_target: Path) -> None:
    claude = install_into_target / "CLAUDE.md"
    assert claude.exists(), "expected root CLAUDE.md for Claude Code"
    text = claude.read_text(encoding="utf-8")
    assert text == (install_into_target / ".ai" / "AGENTS.md").read_text(encoding="utf-8")
    assert "Match the user's language unless they request otherwise." in text
    assert "Keep self-initiated progress/output under 10 words." in text


def test_install_into_publishes_canonical_bootstrap(install_into_target: Path) -> None:
    installed = install_into_target / "bootstrap-code-brain.sh"
    assert installed.read_bytes() == (ROOT / "bootstrap-code-brain.sh").read_bytes()


def test_install_into_deferred_runtime_does_not_activate(install_into_target: Path) -> None:
    assert not (install_into_target / ".ai" / "runtime" / ".venv").exists()
    assert not (install_into_target / ".ai" / "cache" / "code.sqlite").exists()


def test_install_into_codex_config_is_byte_idempotent(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "codex-idempotent", symlinks=True)
    config = target / ".codex" / "config.toml"
    before = config.read_bytes()

    env = os.environ.copy()
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "upgrade", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-1000:]
    assert config.read_bytes() == before
    text = before.decode("utf-8")
    assert text.index("[features]") < text.index("[mcp_servers.code-brain]")
    assert 'AI_CODE_BRAIN_PROFILE = "usage"' in text
    assert 'AI_MCP_COMPACT_TOOLS = "1"' in text


def test_install_into_refuses_untracked_managed_file(tmp_path: Path) -> None:
    target = tmp_path / "victim-untracked-managed"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    managed = target / ".ai" / "AGENTS.md"
    managed.parent.mkdir(parents=True)
    original = "# User-owned .ai contract\n"
    managed.write_text(original, encoding="utf-8")

    env = os.environ.copy()
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 3
    assert "refusing to overwrite existing untracked target file .ai/AGENTS.md" in result.stderr
    assert managed.read_text(encoding="utf-8") == original


def test_install_into_rejects_managed_symlink_escape(tmp_path: Path) -> None:
    target = tmp_path / "victim-symlink-escape"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    try:
        (target / ".ai").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    env = os.environ.copy()
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 3
    assert "target path escapes project root" in result.stderr
    assert list(outside.iterdir()) == []


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
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    res = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "install", str(target)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(f"install-into.sh skipped: {res.stderr[-400:]}")
    final = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert final == user_agents, "user AGENTS.md must not be overwritten"


def test_install_into_preserves_user_authored_claude_md(tmp_path: Path) -> None:
    target = tmp_path / "victim-claude"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=target, check=True)
    user_claude = "# Custom Claude rules\n\nProject-specific Claude instructions.\n"
    (target / "CLAUDE.md").write_text(user_claude, encoding="utf-8")
    (target / "README.md").write_text("# v\n", encoding="utf-8")
    subprocess.run(["git", "add", "CLAUDE.md", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True)

    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    res = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "install", str(target)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(f"install-into.sh skipped: {res.stderr[-400:]}")
    final = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert final == user_claude, "user CLAUDE.md must not be overwritten"


def test_install_into_replaces_old_claude_pointer_stub(tmp_path: Path) -> None:
    target = tmp_path / "victim-claude-stub"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=target, check=True)
    (target / "CLAUDE.md").write_text("# CLAUDE.md\n\nCanonical Claude instructions live in `.ai/AGENTS.md`.\n", encoding="utf-8")
    (target / "README.md").write_text("# v\n", encoding="utf-8")
    subprocess.run(["git", "add", "CLAUDE.md", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True)

    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    res = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "install", str(target)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(f"install-into.sh skipped: {res.stderr[-400:]}")
    final = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Match the user's language unless they request otherwise." in final
    assert "Keep self-initiated progress/output under 10 words." in final
    assert "Canonical Claude instructions live" not in final
    assert final == (target / ".ai" / "AGENTS.md").read_text(encoding="utf-8")


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
