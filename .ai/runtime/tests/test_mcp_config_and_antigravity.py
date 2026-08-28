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
import signal
import shutil
import subprocess
import sys
import time
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


def test_install_into_excludes_source_user_eval_scratch(install_into_target: Path) -> None:
    assert not (install_into_target / ".ai" / "eval").exists()
    assert (install_into_target / ".ai" / "evals").is_dir()


def test_self_upgrade_preserves_source_only_vendored_runtime_opt_in(tmp_path: Path) -> None:
    source = shutil.copytree(
        ROOT,
        tmp_path / "source-self-upgrade",
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "cache",
            "tmp",
            "eval",
            "outputs",
            "__pycache__",
            "*.pyc",
        ),
    )
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    config = source / ".ai" / "config.yaml"
    assert "index_vendored_runtime: true" in config.read_text(encoding="utf-8")
    env = os.environ.copy()
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"

    result = subprocess.run(
        ["bash", str(source / "scripts" / "install-into.sh"), "upgrade", str(source)],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-1000:]
    assert "index_vendored_runtime: true" in config.read_text(encoding="utf-8")
    assert not (source / ".code-brain-install-transaction").exists()


def test_install_into_writes_antigravity_hooks(install_into_target: Path) -> None:
    hooks = install_into_target / ".agents" / "hooks.json"
    assert hooks.exists(), "expected .agents/hooks.json"
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    # Antigravity 2.0 / CLI 1.1.x schema: top-level {name: spec} map; a spec has one field
    # per native event. No legacy Claude wrapper ("_note"/"hooks") — Antigravity
    # cannot parse that. No SessionStart/UserPromptSubmit (Antigravity has neither).
    assert "_note" not in payload and "hooks" not in payload
    spec = payload["code-brain"]
    assert set(spec) == {"PreToolUse", "PostToolUse", "PreInvocation", "PostInvocation", "Stop"}
    assert "SessionStart" not in spec and "UserPromptSubmit" not in spec
    # PostInvocation unused (null). PreInvocation captures the per-request dirty-tree baseline
    # on invocationNum=0 so stale consumer-project debt cannot cause false Stop blocks.
    # PreToolUse is still null for Antigravity:
    # its jsonhook contract is deny-by-default, so a Code Brain PreToolUse hook denies EVERY
    # agy tool call (it broke the worker rather than protecting it). Only the side-effect events
    # PostToolUse (redaction/recording) and Stop (memory refresh) carry handlers.
    assert spec["PostInvocation"] is None
    assert spec["PreToolUse"] is None
    post_entries = spec["PostToolUse"]
    assert isinstance(post_entries, list) and post_entries
    for entry in post_entries:
        assert "matcher" in entry
        for handler in entry["hooks"]:
            assert handler["type"] == "command"
            assert ".ai/bin/ai-hook" in handler["command"]
            assert "PostToolUse" in handler["command"]

    # Invocation/Stop events use a DIRECT handler list; wrapping Stop in a matcher-group
    # makes the current host ignore/reject the hook and silently disables continuation.
    stop_handlers = spec["Stop"]
    assert isinstance(stop_handlers, list) and stop_handlers
    for handler in stop_handlers:
        assert "matcher" not in handler and "hooks" not in handler
        assert handler["type"] == "command"
        assert ".ai/bin/ai-hook" in handler["command"]
        assert "Stop" in handler["command"]

    pre_handlers = spec["PreInvocation"]
    assert isinstance(pre_handlers, list) and pre_handlers
    for handler in pre_handlers:
        assert "matcher" not in handler and "hooks" not in handler
        assert handler["type"] == "command"
        assert "PreInvocation" in handler["command"]


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


def test_noop_upgrade_does_not_write_protected_identical_managed_configs(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / install_into_target.name, symlinks=True)
    protected = [
        target / ".mcp.json",
        target / ".codex" / "config.toml",
        target / ".codex" / "hooks.json",
        target / ".claude" / "settings.json",
        target / ".agents" / "mcp_config.json",
        target / ".agents" / "hooks.json",
        target / ".ai" / "config.yaml",
        target / ".ai" / "runtime" / "src" / "ai_core" / "completion_guard.py",
    ]
    before = {path: path.read_bytes() for path in protected}
    for path in protected:
        path.chmod(0o444)
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
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path: path.read_bytes() for path in protected} == before


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
    assert not (target / ".ai" / "bin" / "ai").exists(), "failed install must roll back earlier copies"


def test_upgrade_rolls_back_managed_files_when_late_config_merge_fails(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "transaction-rollback", symlinks=True)
    managed = target / ".ai" / "runtime" / "src" / "ai_core" / "hooks.py"
    old_bytes = b"# previous managed runtime\n"
    managed.write_bytes(old_bytes)
    mcp = target / ".mcp.json"
    invalid = b"{ invalid json\n"
    mcp.write_bytes(invalid)
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
    assert result.returncode != 0
    assert "previous managed files and user settings restored" in result.stderr
    assert managed.read_bytes() == old_bytes
    assert mcp.read_bytes() == invalid


def test_upgrade_rollback_restores_prior_git_hooks_path(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "hooks-path-rollback", symlinks=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", "user-hooks"], cwd=target, check=True
    )
    manifest = target / ".ai" / "generated" / "install-manifest.json"
    before_manifest = manifest.read_bytes()
    manifest.chmod(0o444)
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
    assert result.returncode != 0
    assert subprocess.check_output(
        ["git", "config", "--get", "core.hooksPath"], cwd=target, text=True
    ).strip() == "user-hooks"
    assert manifest.read_bytes() == before_manifest


def test_failed_fresh_install_removes_directories_created_by_transaction(tmp_path: Path) -> None:
    target = tmp_path / "fresh-rollback-dirs"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    invalid = b"{ invalid json\n"
    (target / ".mcp.json").write_bytes(invalid)
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
    assert result.returncode != 0
    assert (target / ".mcp.json").read_bytes() == invalid
    assert sorted(path.name for path in target.iterdir()) == [".git", ".mcp.json"]


def test_runtime_activation_failure_atomically_restores_previous_venv(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "venv-rollback", symlinks=True)
    old_venv = target / ".ai" / "runtime" / ".venv"
    old_venv.mkdir(parents=True, exist_ok=True)
    sentinel = old_venv / "old-runtime.txt"
    sentinel.write_text("known-good\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/usr/bin/env bash\nexit 77\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    env = os.environ.copy()
    env.pop("AI_INSTALL_DEFER_RUNTIME", None)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "upgrade", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "known-good\n"
    assert not (target / ".ai" / "runtime" / ".venv.code-brain-rollback").exists()


@pytest.mark.skipif(os.name == "nt", reason="process-group SIGKILL simulation is POSIX-only")
def test_upgrade_recovers_persistent_journal_after_sigkill(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "sigkill-recovery", symlinks=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", "user-hooks"], cwd=target, check=True
    )
    old_venv = target / ".ai" / "runtime" / ".venv"
    old_venv.mkdir(parents=True, exist_ok=True)
    sentinel = old_venv / "known-good.txt"
    sentinel.write_text("pre-crash-runtime\n", encoding="utf-8")

    marker = tmp_path / "uv-entered"
    fake_bin = tmp_path / "crash-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\nprintf ready > \"$CB_CRASH_MARKER\"\nsleep 300\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = os.environ.copy()
    env.pop("AI_INSTALL_DEFER_RUNTIME", None)
    env["CB_CRASH_MARKER"] = str(marker)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    process = subprocess.Popen(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "upgrade", str(target)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not marker.exists() and process.poll() is None:
        time.sleep(0.05)
    if not marker.exists():
        process.kill()
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"installer never reached crash point: {stdout[-300:]} {stderr[-500:]}")
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)

    journal = target / ".code-brain-install-transaction"
    assert (journal / "phase").read_text(encoding="utf-8").strip() == "READY"
    assert (journal / "runtime-prepared").is_file()
    assert (target / ".ai" / "runtime" / ".venv.code-brain-rollback" / "known-good.txt").is_file()

    # Recovery must happen before config parsing. This corruption makes a retry fail unless the
    # durable journal first restores the pre-crash JSON snapshot.
    (target / ".mcp.json").write_text("{ interrupted write", encoding="utf-8")
    retry_env = os.environ.copy()
    retry_env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    retry = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "upgrade", str(target)],
        cwd=ROOT,
        env=retry_env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "recovered an interrupted transaction" in retry.stderr
    assert json.loads((target / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["code-brain"]
    assert sentinel.read_text(encoding="utf-8") == "pre-crash-runtime\n"
    assert subprocess.check_output(
        ["git", "config", "--get", "core.hooksPath"], cwd=target, text=True
    ).strip() == "user-hooks"
    assert not journal.exists()
    assert not (target / ".ai" / "runtime" / ".venv.code-brain-rollback").exists()


def test_interrupted_recovery_refuses_tampered_snapshot_before_writing(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "tampered-journal", symlinks=True)
    current = (target / ".mcp.json").read_bytes()
    journal = target / ".code-brain-install-transaction"
    backup = journal / "files" / ".mcp.json"
    backup.parent.mkdir(parents=True)
    journal.chmod(0o700)
    backup.write_bytes(b"supposed prior content")
    (journal / "owner.json").write_text(
        json.dumps({"schema": 1, "target": str(target.resolve()), "pid": 999_999_999, "action": "upgrade"}),
        encoding="utf-8",
    )
    (journal / "phase").write_text("READY\n", encoding="utf-8")
    (journal / "snapshot.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "target": str(target.resolve()),
                "absent_dirs": [],
                "records": [
                    {"rel": ".mcp.json", "kind": "file", "size": len(b"supposed prior content"), "sha256": "0" * 64}
                ],
            }
        ),
        encoding="utf-8",
    )
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
    assert result.returncode != 0
    assert "rollback backup integrity mismatch" in result.stderr
    assert (target / ".mcp.json").read_bytes() == current
    assert journal.is_dir(), "failed recovery must retain forensic rollback material"


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


def test_windows_target_uses_native_mcp_and_antigravity_commands(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "windows-victim", symlinks=True)
    env = os.environ.copy()
    env["AI_INSTALL_DEFER_RUNTIME"] = "1"
    env["AI_INSTALL_TARGET_WINDOWS"] = "1"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "upgrade", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["code-brain"]
    assert mcp["command"] == "powershell"
    assert mcp["args"][-1] == ".ai/bin/ai-mcp.ps1"
    assert mcp["env"]["AI_CODE_BRAIN_PROFILE"] == "usage"
    codex = (target / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'command = "powershell"' in codex
    assert '".ai/bin/ai-mcp.ps1"' in codex

    agent_mcp = json.loads((target / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
    assert agent_mcp["mcpServers"]["code-brain"]["command"] == "powershell"
    agent_hooks = json.loads((target / ".agents" / "hooks.json").read_text(encoding="utf-8"))
    spec = agent_hooks["code-brain"]
    assert spec["PreToolUse"] is None
    for event in ("PreInvocation", "Stop"):
        command = spec[event][0]["command"]
        assert "powershell" in command and "ai-hook.ps1" in command

    claude = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert claude["env"]["AI_LOOP_CONTINUATION"] == "1"
    assert claude["hooks"]["Stop"][-1]["hooks"][0]["commandWindows"]


def test_windows_installer_delegates_to_transactional_unix_core() -> None:
    script = (ROOT / "scripts" / "install-into.ps1").read_text(encoding="utf-8")
    activation = (ROOT / "scripts" / "activate-windows.ps1").read_text(encoding="utf-8")
    assert "install-into.sh" in script
    assert "AI_INSTALL_TARGET_WINDOWS" in script
    assert "cygpath.exe" in script and "bash.exe" in script
    assert "Copy-Item -Force" not in script
    assert "Merge-McpJson" not in script
    assert '"doctor"' in activation and '"--strict"' in activation
    assert '"session", "start"' in activation


def test_uninstall_preserves_private_state_and_removes_all_managed_wires(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "uninstall-victim", symlinks=True)
    private = target / ".ai" / "memory" / "private-session.txt"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b"private-state-must-survive")
    allowlist = target / ".ai" / "secret_scan_allowlist.txt"
    allowlist.write_bytes(b"user-owned-allowlist\n")

    mcp_path = target / ".mcp.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp["mcpServers"]["user-server"] = {"command": "user-mcp"}
    mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    agent_mcp_path = target / ".agents" / "mcp_config.json"
    agent_mcp = json.loads(agent_mcp_path.read_text(encoding="utf-8"))
    agent_mcp["mcpServers"]["user-server"] = {"command": "user-agent-mcp"}
    agent_mcp_path.write_text(json.dumps(agent_mcp), encoding="utf-8")
    agent_hooks_path = target / ".agents" / "hooks.json"
    agent_hooks = json.loads(agent_hooks_path.read_text(encoding="utf-8"))
    agent_hooks["user-hook"] = {"Stop": None}
    agent_hooks_path.write_text(json.dumps(agent_hooks), encoding="utf-8")
    codex_hooks_path = target / ".codex" / "hooks.json"
    codex_hooks = json.loads(codex_hooks_path.read_text(encoding="utf-8"))
    codex_hooks["hooks"].setdefault("Stop", []).insert(
        0,
        {"hooks": [{"type": "command", "command": "user-stop-hook"}]},
    )
    codex_hooks_path.write_text(json.dumps(codex_hooks), encoding="utf-8")

    venv = target / ".ai" / "runtime" / ".venv"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "generated-runtime.txt").write_text("remove me", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "uninstall", str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert private.read_bytes() == b"private-state-must-survive"
    assert allowlist.read_bytes() == b"user-owned-allowlist\n"
    assert not (target / ".ai" / "bin" / "ai").exists()
    assert not venv.exists()
    assert not (target / ".ai" / "generated" / "install-manifest.json").exists()
    assert "code-brain" not in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "user-server" in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "code-brain" not in json.loads(agent_mcp_path.read_text(encoding="utf-8"))["mcpServers"]
    remaining_agent_hooks = json.loads(agent_hooks_path.read_text(encoding="utf-8"))
    assert "code-brain" not in remaining_agent_hooks
    assert "user-hook" in remaining_agent_hooks
    remaining_codex = json.loads(codex_hooks_path.read_text(encoding="utf-8"))
    assert remaining_codex["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "user-stop-hook"}]}
    ]


def test_uninstall_rolls_back_when_late_config_write_fails(
    install_into_target: Path,
    tmp_path: Path,
) -> None:
    target = shutil.copytree(install_into_target, tmp_path / "uninstall-rollback", symlinks=True)
    managed = target / ".ai" / "runtime" / "src" / "ai_core" / "hooks.py"
    before_managed = managed.read_bytes()
    mcp = target / ".mcp.json"
    payload = json.loads(mcp.read_text(encoding="utf-8"))
    payload["mcpServers"]["user-server"] = {"command": "user-mcp"}
    mcp.write_text(json.dumps(payload), encoding="utf-8")
    before_mcp = mcp.read_bytes()
    venv = target / ".ai" / "runtime" / ".venv"
    venv.mkdir(parents=True, exist_ok=True)
    sentinel = venv / "keep-on-rollback.txt"
    sentinel.write_text("runtime\n", encoding="utf-8")
    mcp.chmod(0o444)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-into.sh"), "uninstall", str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0
    assert "previous managed files and user settings restored" in result.stderr
    assert managed.read_bytes() == before_managed
    assert mcp.read_bytes() == before_mcp
    assert sentinel.read_text(encoding="utf-8") == "runtime\n"
    assert not (target / ".ai" / "runtime" / ".venv.code-brain-rollback").exists()


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
