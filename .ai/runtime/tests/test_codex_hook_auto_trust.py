from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.codex_hook_contract import managed_codex_hooks  # noqa: E402

HELPER = ROOT / "scripts" / "trust-codex-hooks.py"
INSTALLER = ROOT / "scripts" / "install-into.sh"


def _write_fake_codex(path: Path, *, project_trusted: bool = True) -> None:
    """Write a fake Codex app-server.

    Mirrors real Codex behavior: when the project cwd is not yet trusted
    (projects.<cwd>.trust_level != "trusted"), hooks/list omits every
    "source": "project" hook for that cwd entirely, independent of any
    individual hook's hash-trust status. config/batchWrite on the
    projects."<cwd>".trust_level keyPath flips that gate; config/batchWrite
    on hooks.state hash-trusts individual hooks as before.
    """
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

if sys.argv[1:] != ["app-server", "--listen", "stdio://"]:
    raise SystemExit(2)

hooks = json.loads(os.environ["FAKE_HOOKS"])
trusted = set(json.loads(os.environ.get("FAKE_INITIAL_TRUSTED", "[]")))
project_trusted = os.environ.get("FAKE_PROJECT_TRUSTED", "1") == "1"
log_path = os.environ.get("FAKE_BATCH_LOG")
project_trust_key = 'projects.\"' + os.environ["FAKE_CWD"] + '\".trust_level'

for raw in sys.stdin:
    message = json.loads(raw)
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {"serverInfo": {"name": "fake", "version": "1"}}
    elif method == "hooks/list":
        current = []
        for hook in hooks:
            if hook.get("source") == "project" and not project_trusted:
                continue
            item = dict(hook)
            if item["key"] in trusted:
                item["trustStatus"] = "trusted"
            current.append(item)
        result = {"data": [{"cwd": os.environ["FAKE_CWD"], "hooks": current}]}
    elif method == "config/batchWrite":
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, sort_keys=True) + "\\n")
        for edit in message["params"]["edits"]:
            key_path = edit.get("keyPath")
            if key_path == project_trust_key:
                if edit["value"] == "trusted":
                    project_trusted = True
            elif edit.get("value") is None and key_path.startswith("hooks.state."):
                trusted.discard(json.loads(key_path.removeprefix("hooks.state.")))
            else:
                trusted.update(edit["value"])
        result = {"ok": True}
    else:
        response = {"id": request_id, "error": {"message": "unsupported"}}
        print(json.dumps(response), flush=True)
        continue
    print(json.dumps({"id": request_id, "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _write_policy(
    path: Path,
    *,
    project_root: Path,
    user_hooks: list[Path] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "trust_project_code_brain_hooks": True,
                "trusted_project_roots": [str(project_root)],
                "trusted_user_hook_paths": [str(item) for item in user_hooks or []],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


MANAGED_PROJECT_HOOK_FILES = (".codex/hooks.json", ".ai/bin/ai-hook", ".ai/bin/ai-hook.ps1")


def _seed_matching_managed_files(repo: Path) -> None:
    """Seed canonical hooks plus byte-identical runtime routers."""
    for relative in MANAGED_PROJECT_HOOK_FILES:
        source = ROOT / relative
        dest = repo / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        dest.chmod(source.stat().st_mode & 0o777)


def _seed_matching_managed_config(repo: Path) -> None:
    source = ROOT / ".codex" / "config.toml"
    dest = repo / ".codex" / "config.toml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    dest.chmod(source.stat().st_mode & 0o777)


def _tamper_managed_file(repo: Path, relative: str) -> None:
    target = repo / relative
    target.write_bytes(target.read_bytes() + b"\n# tampered\n")


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    main = workspace / "main"
    linked = workspace / "linked"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True)
    (main / "README.md").write_text("# main\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-q", "-m", "initial"], check=True)
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "linked", str(linked)],
        check=True,
    )
    _seed_matching_managed_files(main)
    _seed_matching_managed_files(linked)
    return main.resolve(), linked.resolve()


def _project_hook(
    repo: Path,
    *,
    trust_status: str = "modified",
    source_repo: Path | None = None,
) -> dict[str, str]:
    source_repo = source_repo or repo
    return {
        "key": f"{source_repo}/.codex/hooks.json:pre_tool_use:0:0",
        "currentHash": "sha256:project",
        "command": (
            'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '
            '"$ROOT/.ai/bin/ai-hook" PreToolUse'
        ),
        "eventName": "preToolUse",
        "source": "project",
        "sourcePath": str(source_repo / ".codex" / "hooks.json"),
        "trustStatus": trust_status,
    }


def _project_hooks(repo: Path, *, trust_status: str = "untrusted") -> list[dict[str, str]]:
    payload = json.loads((repo / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    hooks: list[dict[str, str]] = []
    for event, groups in payload["hooks"].items():
        for group_index, group in enumerate(groups):
            for handler_index, handler in enumerate(group.get("hooks", [])):
                command = handler.get("command")
                if not isinstance(command, str):
                    continue
                hooks.append(
                    {
                        "key": (
                            f"{repo}/.codex/hooks.json:{event}:"
                            f"{group_index}:{handler_index}"
                        ),
                        "currentHash": f"sha256:{event.lower()}-{group_index}-{handler_index}",
                        "command": command,
                        "eventName": event[0].lower() + event[1:],
                        "source": "project",
                        "sourcePath": str(repo / ".codex" / "hooks.json"),
                        "trustStatus": trust_status,
                    }
                )
    return hooks


def _run_helper(
    *,
    repo: Path,
    policy: Path | None,
    codex_home: Path,
    fake_codex: Path,
    hooks: list[dict[str, str]],
    log_path: Path,
    project_trusted: bool = True,
    managed_target_default: bool = False,
    fallback_managed_target: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CODEX_BIN": str(fake_codex),
        "CODEX_HOME": str(codex_home),
        "FAKE_BATCH_LOG": str(log_path),
        "FAKE_CWD": str(repo.resolve()),
        "FAKE_HOOKS": json.dumps(hooks),
        "FAKE_PROJECT_TRUSTED": "1" if project_trusted else "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [sys.executable, str(HELPER), "--cwd", str(repo)]
    if managed_target_default:
        assert policy is None
        command.append("--trust-managed-target")
    else:
        assert policy is not None
        command.extend(("--policy", str(policy)))
        if fallback_managed_target:
            command.append("--fallback-managed-target")
    command.append("--json")
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _init_target_repo(path: Path) -> Path:
    target = path.resolve()
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(target), "config", "user.name", "Test"],
        check=True,
    )
    (target / "README.md").write_text("# target\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "initial"], check=True)
    return target


def test_helper_trusts_only_exact_allowlisted_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    project_hooks = repo / ".codex" / "hooks.json"

    codex_home = tmp_path / "codex-home"
    user_hook_dir = codex_home / "hooks"
    user_hook_dir.mkdir(parents=True)
    (codex_home / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    allowed_user_hook = user_hook_dir / "allowed.sh"
    allowed_user_hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    allowed_user_hook.chmod(0o700)
    other_user_hook = user_hook_dir / "other.sh"
    other_user_hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    other_user_hook.chmod(0o700)

    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent, user_hooks=[allowed_user_hook])
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    hooks = [
        _project_hook(repo),
        {
            **_project_hook(repo),
            "key": f"{project_hooks}:pre_tool_use:1:0",
            "currentHash": "sha256:foreign-project",
            "command": "echo not-code-brain",
            "trustStatus": "untrusted",
        },
        {
            "key": f"{codex_home}/hooks.json:pre_tool_use:0:0",
            "currentHash": "sha256:user-allowed",
            "command": f"'{allowed_user_hook}'",
            "eventName": "preToolUse",
            "source": "user",
            "sourcePath": str(codex_home / "hooks.json"),
            "trustStatus": "untrusted",
        },
        {
            "key": f"{codex_home}/hooks.json:pre_tool_use:1:0",
            "currentHash": "sha256:user-other",
            "command": f"'{other_user_hook}'",
            "eventName": "preToolUse",
            "source": "user",
            "sourcePath": str(codex_home / "hooks.json"),
            "trustStatus": "untrusted",
        },
    ]
    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=hooks,
        log_path=log_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 0,
        "eligible": 2,
        "ok": True,
        "trusted": 2,
    }
    request = json.loads(log_path.read_text(encoding="utf-8"))
    edit = request["params"]["edits"][0]
    assert edit["keyPath"] == "hooks.state"
    assert edit["mergeStrategy"] == "upsert"
    assert edit["value"] == {
        hooks[0]["key"]: {"trusted_hash": "sha256:project"},
        hooks[2]["key"]: {"trusted_hash": "sha256:user-allowed"},
    }


def test_helper_managed_target_default_bootstraps_without_policy(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    _seed_matching_managed_config(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    hooks = _project_hooks(repo)
    hooks.append(
        {
            "key": f"{codex_home}/hooks.json:pre_tool_use:0:0",
            "currentHash": "sha256:user-hook",
            "command": str(codex_home / "hooks" / "user-owned.sh"),
            "eventName": "preToolUse",
            "source": "user",
            "sourcePath": str(codex_home / "hooks.json"),
            "trustStatus": "untrusted",
        }
    )
    result = _run_helper(
        repo=repo,
        policy=None,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=hooks,
        log_path=log_path,
        project_trusted=False,
        managed_target_default=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 0,
        "eligible": len(hooks) - 1,
        "ok": True,
        "trusted": len(hooks) - 1,
    }
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    key_paths = [request["params"]["edits"][0]["keyPath"] for request in requests]
    expected_project_key = 'projects."' + str(repo.resolve()) + '".trust_level'
    assert key_paths == [expected_project_key, "hooks.state"]
    hook_state = requests[1]["params"]["edits"][0]["value"]
    assert set(hook_state) == {hook["key"] for hook in hooks if hook["source"] == "project"}


def test_helper_trusts_version_gated_session_end_and_interrupt(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    (repo / ".codex" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": managed_codex_hooks(
                    session_end_enabled=True,
                    interrupt_enabled=True,
                )
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_matching_managed_config(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    hooks: list[dict[str, str]] = []
    for wire_name, runtime_name in (("sessionEnd", "SessionEnd"), ("interrupt", "Interrupt")):
        hook = _project_hook(repo, trust_status="untrusted")
        hook.update(
            {
                "key": f"{repo}/.codex/hooks.json:{wire_name}:0:0",
                "currentHash": f"sha256:{wire_name}",
                "command": (
                    'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '
                    f'"$ROOT/.ai/bin/ai-hook" {runtime_name}'
                ),
                "eventName": wire_name,
            }
        )
        hooks.append(hook)

    result = _run_helper(
        repo=repo,
        policy=None,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=hooks,
        log_path=log_path,
        managed_target_default=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 0,
        "eligible": 2,
        "ok": True,
        "trusted": 2,
    }
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    trusted = requests[-1]["params"]["edits"][0]["value"]
    assert set(trusted) == {hook["key"] for hook in hooks}


def test_helper_remove_managed_target_preserves_foreign_and_user_trust(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    managed = _project_hooks(repo)
    foreign = _project_hook(repo, trust_status="untrusted")
    foreign.update(
        {
            "key": f"{repo}/.codex/hooks.json:pre_tool_use:foreign:0",
            "currentHash": "sha256:foreign-project",
            "command": "printf foreign-project-hook",
        }
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    user = {
        "key": f"{codex_home}/hooks.json:user:0:0",
        "currentHash": "sha256:user-hook",
        "command": str(codex_home / "hooks" / "user.sh"),
        "eventName": "preToolUse",
        "source": "user",
        "sourcePath": str(codex_home / "hooks.json"),
        "trustStatus": "untrusted",
    }
    hooks = [*managed, foreign, user]
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    env = {
        **os.environ,
        "CODEX_BIN": str(fake_codex),
        "CODEX_HOME": str(codex_home),
        "FAKE_BATCH_LOG": str(log_path),
        "FAKE_CWD": str(repo.resolve()),
        "FAKE_HOOKS": json.dumps(hooks),
        "FAKE_INITIAL_TRUSTED": json.dumps([hook["key"] for hook in hooks]),
        "FAKE_PROJECT_TRUSTED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    result = subprocess.run(
        [sys.executable, str(HELPER), "--cwd", str(repo), "--remove-managed-target", "--json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"ok": True, "removed": len(managed)}
    request = json.loads(log_path.read_text(encoding="utf-8"))
    edits = request["params"]["edits"]
    removed = {
        json.loads(edit["keyPath"].removeprefix("hooks.state."))
        for edit in edits
        if edit["keyPath"].startswith("hooks.state.") and edit["value"] is None
    }
    assert removed == {hook["key"] for hook in managed}
    assert foreign["key"] not in removed
    assert user["key"] not in removed


def test_helper_managed_target_default_skips_custom_project_config(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    _seed_matching_managed_config(repo)
    with (repo / ".codex" / "config.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[mcp_servers.foreign]\ncommand = "foreign-command"\n')
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=None,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
        managed_target_default=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["skipped"] == "managed_target_not_auto_trusted"
    assert "unmodified Code Brain project config" in payload["reason"]
    assert not log_path.exists()


def test_helper_managed_target_default_skips_tampered_hook_router(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    _seed_matching_managed_config(repo)
    _tamper_managed_file(repo, ".ai/bin/ai-hook")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=None,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
        managed_target_default=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["skipped"] == "managed_target_not_auto_trusted"
    assert ".ai/bin/ai-hook" in payload["reason"]
    assert not log_path.exists()


def test_helper_managed_target_default_skips_unusable_codex(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    _seed_matching_managed_config(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=None,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=_project_hooks(repo),
        log_path=log_path,
        project_trusted=False,
        managed_target_default=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["skipped"] == "managed_target_not_auto_trusted"
    assert "app-server" in payload["reason"]
    assert not log_path.exists()


def test_helper_managed_target_default_skips_unspawnable_codex(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    _seed_matching_managed_config(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    fake_codex.write_bytes(b"not-an-executable-format")
    fake_codex.chmod(0o700)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=None,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=_project_hooks(repo),
        log_path=log_path,
        project_trusted=False,
        managed_target_default=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["skipped"] == "managed_target_not_auto_trusted"
    assert payload["reason"] == "failed to start Codex app-server"
    assert not log_path.exists()


def test_helper_skips_cwd_outside_allowlisted_roots_before_starting_codex(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = tmp_path / "other" / "repo"
    repo.mkdir(parents=True)
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=allowed)
    result = subprocess.run(
        [sys.executable, str(HELPER), "--cwd", str(repo), "--policy", str(policy), "--json"],
        cwd=ROOT,
        env={**os.environ, "CODEX_BIN": str(tmp_path / "missing-codex")},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["skipped"] == "cwd_not_allowlisted"


def test_helper_default_policy_falls_back_to_exact_managed_target(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = tmp_path / "other" / "repo"
    _seed_matching_managed_files(repo)
    _seed_matching_managed_config(repo)
    codex_home = tmp_path / "codex-home"
    user_hook_dir = codex_home / "hooks"
    user_hook_dir.mkdir(parents=True)
    allowed_user_hook = user_hook_dir / "allowed.sh"
    allowed_user_hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    allowed_user_hook.chmod(0o700)
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=allowed, user_hooks=[allowed_user_hook])
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    hooks = _project_hooks(repo)
    hooks.append(
        {
            "key": f"{codex_home}/hooks.json:pre_tool_use:0:0",
            "currentHash": "sha256:user-allowed",
            "command": f"'{allowed_user_hook}'",
            "eventName": "preToolUse",
            "source": "user",
            "sourcePath": str(codex_home / "hooks.json"),
            "trustStatus": "untrusted",
        }
    )

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=hooks,
        log_path=log_path,
        project_trusted=False,
        fallback_managed_target=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 0,
        "eligible": len(hooks),
        "ok": True,
        "trusted": len(hooks),
    }
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert requests[0]["params"]["edits"][0]["keyPath"] == (
        'projects."' + str(repo.resolve()) + '".trust_level'
    )
    trusted = requests[1]["params"]["edits"][0]["value"]
    assert set(trusted) == {hook["key"] for hook in hooks}


def test_helper_default_policy_fallback_safely_skips_custom_target_config(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = tmp_path / "other" / "repo"
    _seed_matching_managed_files(repo)
    _seed_matching_managed_config(repo)
    with (repo / ".codex" / "config.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[mcp_servers.foreign]\ncommand = "foreign-command"\n')
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=allowed)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=_project_hooks(repo),
        log_path=log_path,
        project_trusted=False,
        fallback_managed_target=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["skipped"] == "managed_target_not_auto_trusted"
    assert "unmodified Code Brain project config" in payload["reason"]
    assert not log_path.exists()


def test_helper_does_not_rewrite_already_trusted_state(tmp_path: Path) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="trusted")],
        log_path=log_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 1,
        "eligible": 1,
        "ok": True,
        "trusted": 0,
    }
    assert not log_path.exists()


def test_helper_bootstraps_missing_project_trust_before_hash_trust(tmp_path: Path) -> None:
    """Regression: without projects.<cwd>.trust_level=trusted, real Codex
    omits every project-sourced hook from hooks/list, so a helper that only
    hash-trusts existing entries would silently see zero eligible project
    hooks. The helper must bootstrap project trust first.
    """
    repo = tmp_path / "workspace" / "repo"
    project_hooks = repo / ".codex" / "hooks.json"
    project_hooks.parent.mkdir(parents=True)
    _seed_matching_managed_files(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 0,
        "eligible": 1,
        "ok": True,
        "trusted": 1,
    }
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    key_paths = [request["params"]["edits"][0]["keyPath"] for request in requests]
    expected_project_key = 'projects."' + str(repo.resolve()) + '".trust_level'
    assert expected_project_key in key_paths
    assert key_paths.index(expected_project_key) < key_paths.index("hooks.state")
    project_edit = next(
        request["params"]["edits"][0]
        for request in requests
        if request["params"]["edits"][0]["keyPath"] == expected_project_key
    )
    assert project_edit["value"] == "trusted"


def test_helper_does_not_write_project_trust_outside_allowlisted_roots(tmp_path: Path) -> None:
    """No app-server call, and therefore no projects.<cwd>.trust_level write,
    may happen for a cwd outside every trusted_project_roots entry.
    """
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = tmp_path / "other" / "repo"
    repo.mkdir(parents=True)
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=allowed)
    marker_codex = tmp_path / "codex-should-not-run"

    result = subprocess.run(
        [sys.executable, str(HELPER), "--cwd", str(repo), "--policy", str(policy), "--json"],
        cwd=ROOT,
        env={**os.environ, "CODEX_BIN": str(marker_codex)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["skipped"] == "cwd_not_allowlisted"
    assert not marker_codex.exists()


def test_helper_rejects_malformed_policy_without_bootstrapping_project_trust(tmp_path: Path) -> None:
    """A malformed opt-in policy must fail closed: no app-server is started,
    so no projects.<cwd>.trust_level write can happen either.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema": 1, "trust_project_code_brain_hooks": "yes"}), encoding="utf-8")
    policy.chmod(0o600)
    marker_codex = tmp_path / "codex-should-not-run"

    for extra in ([], ["--fallback-managed-target"]):
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--cwd",
                str(repo),
                "--policy",
                str(policy),
                *extra,
                "--json",
            ],
            cwd=ROOT,
            env={**os.environ, "CODEX_BIN": str(marker_codex)},
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "trust_project_code_brain_hooks must be boolean" in payload["error"]
    assert not marker_codex.exists()


def test_helper_project_trust_bootstrap_is_idempotent(tmp_path: Path) -> None:
    """When Codex already reports project-sourced hooks for this exact cwd,
    the helper must not attempt to rewrite projects.<cwd>.trust_level again.
    """
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="trusted")],
        log_path=log_path,
        project_trusted=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 1,
        "eligible": 1,
        "ok": True,
        "trusted": 0,
    }
    assert not log_path.exists()


def test_helper_refuses_project_trust_bootstrap_when_hooks_json_tampered(tmp_path: Path) -> None:
    """A valid-JSON managed-field change must never reach config/batchWrite."""
    repo = tmp_path / "workspace" / "repo"
    project_hooks = repo / ".codex" / "hooks.json"
    project_hooks.parent.mkdir(parents=True)
    _seed_matching_managed_files(repo)
    payload = json.loads(project_hooks.read_text(encoding="utf-8"))
    payload["hooks"]["PreToolUse"][-1]["hooks"][0]["timeout"] = 999
    project_hooks.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "violates the managed Code Brain contract" in payload["error"]
    assert "PreToolUse" in payload["error"]
    assert not log_path.exists()


def test_helper_refuses_project_trust_bootstrap_when_ai_hook_tampered(tmp_path: Path) -> None:
    """Regression: a target whose .ai/bin/ai-hook router byte content
    diverges from the helper's own source tree must never reach
    config/batchWrite, even though .codex/hooks.json itself is untouched.
    """
    repo = tmp_path / "workspace" / "repo"
    project_hooks = repo / ".codex" / "hooks.json"
    project_hooks.parent.mkdir(parents=True)
    _seed_matching_managed_files(repo)
    _tamper_managed_file(repo, ".ai/bin/ai-hook")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "does not match helper source" in payload["error"]
    assert not log_path.exists()


def test_helper_rejects_tampered_ai_hook_even_when_project_is_already_trusted(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace" / "repo"
    _seed_matching_managed_files(repo)
    _tamper_managed_file(repo, ".ai/bin/ai-hook")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="trusted")],
        log_path=log_path,
        project_trusted=True,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert ".ai/bin/ai-hook" in payload["error"]
    assert not log_path.exists()


def test_helper_refuses_project_trust_bootstrap_when_ai_hook_not_executable(tmp_path: Path) -> None:
    """Regression: .ai/bin/ai-hook must be executable on the target even when
    its byte content matches the helper's source tree exactly.
    """
    repo = tmp_path / "workspace" / "repo"
    project_hooks = repo / ".codex" / "hooks.json"
    project_hooks.parent.mkdir(parents=True)
    _seed_matching_managed_files(repo)
    (repo / ".ai" / "bin" / "ai-hook").chmod(0o600)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "must be executable" in payload["error"]
    assert not log_path.exists()


def test_helper_refuses_project_trust_bootstrap_when_managed_file_missing(tmp_path: Path) -> None:
    """Regression: a target missing one of the managed hook files entirely
    (e.g. .ai/bin/ai-hook.ps1 never installed) must fail closed rather than
    silently skipping that file's comparison.
    """
    repo = tmp_path / "workspace" / "repo"
    project_hooks = repo / ".codex" / "hooks.json"
    project_hooks.parent.mkdir(parents=True)
    _seed_matching_managed_files(repo)
    (repo / ".ai" / "bin" / "ai-hook.ps1").unlink()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "is unavailable" in payload["error"]
    assert not log_path.exists()


def test_helper_project_trust_bootstrap_succeeds_when_target_is_helper_source_root(
    tmp_path: Path,
) -> None:
    """source_root == cwd (trusting Code Brain's own checkout) must still
    pass: the managed-file comparison is trivially satisfied because target
    and source paths are the same files, while ownership/permission/exec
    checks still apply.
    """
    repo = ROOT
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=repo,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(repo, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 0,
        "eligible": 1,
        "ok": True,
        "trusted": 1,
    }
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    key_paths = [request["params"]["edits"][0]["keyPath"] for request in requests]
    expected_project_key = 'projects."' + str(repo.resolve()) + '".trust_level'
    assert expected_project_key in key_paths


def test_helper_accepts_main_worktree_project_hook_source_for_linked_worktree(
    tmp_path: Path,
) -> None:
    main, linked = _linked_worktree(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=main.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=linked,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(linked, source_repo=main, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "already_trusted": 0,
        "eligible": 1,
        "ok": True,
        "trusted": 1,
    }
    request = json.loads(log_path.read_text(encoding="utf-8"))
    assert request["params"]["edits"][0]["keyPath"] == "hooks.state"


def test_helper_rejects_foreign_project_hook_source_for_linked_worktree(tmp_path: Path) -> None:
    main, linked = _linked_worktree(tmp_path)
    foreign = main.parent / "foreign"
    _seed_matching_managed_files(foreign)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=main.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"

    result = _run_helper(
        repo=linked,
        policy=policy,
        codex_home=codex_home,
        fake_codex=fake_codex,
        hooks=[_project_hook(linked, source_repo=foreign, trust_status="untrusted")],
        log_path=log_path,
        project_trusted=True,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "unexpected source path" in payload["error"]
    assert not log_path.exists()


def test_helper_rejects_writable_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=repo)
    policy.chmod(0o666)
    result = subprocess.run(
        [sys.executable, str(HELPER), "--cwd", str(repo), "--policy", str(policy), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "group/world writable" in json.loads(result.stdout)["error"]


def test_installer_invokes_hook_trust_after_transaction_cleanup() -> None:
    script = INSTALLER.read_text(encoding="utf-8")
    start = script.index("install_or_upgrade() {")
    end = script.index("\nuninstall_apply()", start)
    install_block = script[start:end]
    assert install_block.index('mark_install_transaction_committed') < install_block.index(
        'auto_trust_codex_hooks'
    )
    assert install_block.index('_INSTALL_TXN_DIR=""') < install_block.index(
        'auto_trust_codex_hooks'
    )


def test_installer_removes_managed_hook_hashes_before_uninstall_mutation() -> None:
    script = INSTALLER.read_text(encoding="utf-8")
    start = script.index("\nuninstall() {")
    end = script.index("\nrecover_interrupted_install_transaction", start)
    uninstall_block = script[start:end]
    assert uninstall_block.index("prepare_runtime_transaction") < uninstall_block.index(
        "remove_codex_hook_trust_before_uninstall"
    )
    assert uninstall_block.index("remove_codex_hook_trust_before_uninstall") < uninstall_block.index(
        "uninstall_apply"
    )


def test_installer_applies_opt_in_trust_policy(tmp_path: Path) -> None:
    target = (tmp_path / "workspace" / "target").resolve()
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(target), "config", "user.name", "Test"],
        check=True,
    )
    (target / "README.md").write_text("# target\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(target), "commit", "-q", "-m", "initial"],
        check=True,
    )

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy = tmp_path / "policy.json"
    _write_policy(policy, project_root=target.parent)
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    env = {
        **os.environ,
        "AI_CODEX_HOOK_TRUST_POLICY": str(policy),
        "AI_INSTALL_DEFER_RUNTIME": "1",
        "CODEX_BIN": str(fake_codex),
        "CODEX_HOME": str(codex_home),
        "FAKE_BATCH_LOG": str(log_path),
        "FAKE_CWD": str(target),
        "FAKE_HOOKS": json.dumps([_project_hook(target, trust_status="untrusted")]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    request = json.loads(log_path.read_text(encoding="utf-8"))
    assert request["params"]["edits"][0]["value"] == {
        _project_hook(target)["key"]: {"trusted_hash": "sha256:project"}
    }


def test_installer_trusts_exact_managed_target_by_default_without_policy(tmp_path: Path) -> None:
    target = _init_target_repo(tmp_path / "workspace" / "target")
    _seed_matching_managed_files(target)
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    hooks = _project_hooks(target)
    env = dict(os.environ)
    env.pop("AI_CODEX_HOOK_TRUST_POLICY", None)
    env.update(
        {
            "AI_CODEX_HOOK_AUTO_TRUST": "1",
            "AI_INSTALL_DEFER_RUNTIME": "1",
            "CODEX_BIN": str(fake_codex),
            "CODEX_HOME": str(codex_home),
            "FAKE_BATCH_LOG": str(log_path),
            "FAKE_CWD": str(target),
            "FAKE_HOOKS": json.dumps(hooks),
            "FAKE_PROJECT_TRUSTED": "0",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    key_paths = [request["params"]["edits"][0]["keyPath"] for request in requests]
    assert key_paths == ['projects."' + str(target) + '".trust_level', "hooks.state"]
    hook_state = requests[1]["params"]["edits"][0]["value"]
    assert set(hook_state) == {hook["key"] for hook in hooks}
    assert len(hook_state) == len(hooks)
    assert not (config_home / "code-brain" / "codex-hook-trust.json").exists()


def test_installer_stale_default_policy_root_does_not_block_new_managed_target(tmp_path: Path) -> None:
    target = _init_target_repo(tmp_path / "workspace" / "target")
    _seed_matching_managed_files(target)
    allowed = tmp_path / "previous-projects"
    # Reproduce an operator deleting a formerly allowlisted workspace. The
    # default policy augments exact managed-target trust, so this stale entry
    # must not disable every later install/upgrade.
    assert not allowed.exists()
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "config"
    policy = config_home / "code-brain" / "codex-hook-trust.json"
    policy.parent.mkdir(parents=True)
    _write_policy(policy, project_root=allowed)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    hooks = _project_hooks(target)
    env = dict(os.environ)
    env.pop("AI_CODEX_HOOK_TRUST_POLICY", None)
    env.update(
        {
            "AI_CODEX_HOOK_AUTO_TRUST": "1",
            "AI_INSTALL_DEFER_RUNTIME": "1",
            "CODEX_BIN": str(fake_codex),
            "CODEX_HOME": str(codex_home),
            "FAKE_BATCH_LOG": str(log_path),
            "FAKE_CWD": str(target),
            "FAKE_HOOKS": json.dumps(hooks),
            "FAKE_PROJECT_TRUSTED": "0",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    key_paths = [request["params"]["edits"][0]["keyPath"] for request in requests]
    assert key_paths == ['projects."' + str(target) + '".trust_level', "hooks.state"]
    assert len(requests[1]["params"]["edits"][0]["value"]) == len(hooks)


def test_installer_trusts_interrupt_but_not_preserved_foreign_hook(tmp_path: Path) -> None:
    """Installer and helper share one contract across merge/version variance."""

    target = _init_target_repo(tmp_path / "workspace" / "target")
    foreign_group = {
        "matcher": "Shell",
        "hooks": [
            {
                "type": "command",
                "command": "printf foreign-hook",
                "timeout": 1,
            }
        ],
    }
    expected = {
        "hooks": managed_codex_hooks(
            session_end_enabled=True,
            interrupt_enabled=True,
        )
    }
    expected["hooks"]["PreToolUse"].insert(0, foreign_group)
    hook_path = target / ".codex" / "hooks.json"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    surfaced_hooks = _project_hooks(target)

    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    env = dict(os.environ)
    env.pop("AI_CODEX_HOOK_TRUST_POLICY", None)
    env.update(
        {
            "AI_CODEX_CLI_VERSION_OVERRIDE": "0.150.0",
            "AI_CODEX_HOOK_AUTO_TRUST": "1",
            "AI_INSTALL_DEFER_RUNTIME": "1",
            "CODEX_BIN": str(fake_codex),
            "CODEX_HOME": str(codex_home),
            "FAKE_BATCH_LOG": str(log_path),
            "FAKE_CWD": str(target),
            "FAKE_HOOKS": json.dumps(surfaced_hooks),
            "FAKE_PROJECT_TRUSTED": "0",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(hook_path.read_text(encoding="utf-8")) == expected
    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [request["params"]["edits"][0]["keyPath"] for request in requests] == [
        'projects."' + str(target) + '".trust_level',
        "hooks.state",
    ]
    trusted = requests[1]["params"]["edits"][0]["value"]
    foreign = next(hook for hook in surfaced_hooks if hook["command"] == "printf foreign-hook")
    interrupt = next(hook for hook in surfaced_hooks if hook["eventName"] == "interrupt")
    assert foreign["key"] not in trusted
    assert interrupt["key"] in trusted
    assert len(trusted) == len(surfaced_hooks) - 1


def test_installer_allows_default_hook_trust_opt_out(tmp_path: Path) -> None:
    target = _init_target_repo(tmp_path / "workspace" / "target")
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    env = dict(os.environ)
    env.pop("AI_CODEX_HOOK_TRUST_POLICY", None)
    env.update(
        {
            "AI_CODEX_HOOK_AUTO_TRUST": "0",
            "AI_INSTALL_DEFER_RUNTIME": "1",
            "CODEX_BIN": str(fake_codex),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "FAKE_BATCH_LOG": str(log_path),
            "FAKE_CWD": str(target),
            "FAKE_HOOKS": json.dumps([_project_hook(target, trust_status="untrusted")]),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not log_path.exists()


def test_installer_defaults_hook_trust_off_in_ci(tmp_path: Path) -> None:
    target = _init_target_repo(tmp_path / "workspace" / "target")
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    fake_codex = tmp_path / "codex"
    _write_fake_codex(fake_codex)
    log_path = tmp_path / "batch.jsonl"
    env = dict(os.environ)
    for key in (
        "AI_CODEX_HOOK_AUTO_TRUST",
        "AI_CODEX_HOOK_TRUST_POLICY",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "AI_CI",
    ):
        env.pop(key, None)
    env.update(
        {
            "AI_INSTALL_DEFER_RUNTIME": "1",
            "CI": "1",
            "CODEX_BIN": str(fake_codex),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "FAKE_BATCH_LOG": str(log_path),
            "FAKE_CWD": str(target),
            "FAKE_HOOKS": json.dumps([_project_hook(target)]),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not log_path.exists()


def test_installer_keeps_install_when_default_trust_app_server_is_unusable(tmp_path: Path) -> None:
    target = _init_target_repo(tmp_path / "workspace" / "target")
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    dead_codex = tmp_path / "codex"
    dead_codex.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    dead_codex.chmod(0o700)
    log_path = tmp_path / "batch.jsonl"
    env = dict(os.environ)
    env.pop("AI_CODEX_HOOK_TRUST_POLICY", None)
    env.update(
        {
            "AI_CODEX_HOOK_AUTO_TRUST": "1",
            "AI_INSTALL_DEFER_RUNTIME": "1",
            "CODEX_BIN": str(dead_codex),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "FAKE_BATCH_LOG": str(log_path),
            "FAKE_CWD": str(target),
            "FAKE_HOOKS": json.dumps([_project_hook(target)]),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "managed target was not eligible; skipped" in result.stdout
    assert not log_path.exists()
