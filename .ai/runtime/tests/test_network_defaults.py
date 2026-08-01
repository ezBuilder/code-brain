"""-006: network-default alignment — the query path can never download models.

`code_query` is advertised over MCP as readOnlyHint/closed-world, yet the old
activation path spawned a background model download whenever the optional
[dense] extras were importable and the model was absent (the reranker variant
even spawned a command that did not exist, retrying hourly via TTL lock).
Guarded here:

  1. is_active_for (both modules) never spawns anything — even with the legacy
     AI_SEARCH_*_AUTO_INSTALL opt-in still exported,
  2. activation semantics survive: explicit env on/off wins, unset requires
     deps + artifacts already present,
  3. doctor's `network_defaults` check flags stale AUTO_INSTALL envs and
     broken-spawn lock residue,
  4. CI read-only policy rejects `embedding`/`reranker` install/uninstall
     before any network or cache-tree mutation.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import cli, doctor, embedding, policy, reranker  # noqa: E402
from ai_core.policy import PERMISSION_DENIED  # noqa: E402

MODULES = [pytest.param(embedding, id="embedding"), pytest.param(reranker, id="reranker")]
_AUTO_ENVS = ("AI_SEARCH_DENSE_AUTO_INSTALL", "AI_SEARCH_RERANK_AUTO_INSTALL")
_SWITCH_ENVS = ("AI_SEARCH_DENSE", "AI_SEARCH_RERANK")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for env in (*_AUTO_ENVS, *_SWITCH_ENVS, "CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        monkeypatch.delenv(env, raising=False)
    yield


def _switch_env(module) -> str:
    return "AI_SEARCH_DENSE" if module is embedding else "AI_SEARCH_RERANK"


def _no_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("query-path activation must never spawn a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)


@pytest.mark.parametrize("module", MODULES)
def test_activation_with_model_missing_is_off_and_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module
) -> None:
    _no_popen(monkeypatch)
    monkeypatch.setattr(module, "_deps_present", lambda: True)
    module.model_cache_dir(tmp_path).mkdir(parents=True)

    assert module.is_active_for(tmp_path) is False
    assert not (module.model_cache_dir(tmp_path) / ".install-lock").exists()


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("env", _AUTO_ENVS)
def test_legacy_auto_install_env_no_longer_enables_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module, env: str
) -> None:
    _no_popen(monkeypatch)
    monkeypatch.setenv(env, "1")
    monkeypatch.setattr(module, "_deps_present", lambda: True)

    assert module.is_active_for(tmp_path) is False


@pytest.mark.parametrize("module", MODULES)
def test_activation_on_when_deps_and_model_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module
) -> None:
    _no_popen(monkeypatch)
    monkeypatch.setattr(module, "_deps_present", lambda: True)
    monkeypatch.setattr(module, "is_model_present", lambda _root: True)

    assert module.is_active_for(tmp_path) is True


@pytest.mark.parametrize("module", MODULES)
def test_env_switch_semantics_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module
) -> None:
    _no_popen(monkeypatch)
    monkeypatch.setattr(module, "_deps_present", lambda: True)
    monkeypatch.setattr(module, "is_model_present", lambda _root: True)

    monkeypatch.setenv(_switch_env(module), "0")
    assert module.is_active_for(tmp_path) is False, "explicit opt-out must win"

    monkeypatch.setenv(_switch_env(module), "1")
    assert module.is_active_for(tmp_path) is True, "explicit opt-in is deps-gated only"


def test_doctor_network_defaults_ok_when_clean(tmp_path: Path) -> None:
    check = doctor.check_network_defaults(tmp_path)
    assert check.ok is True


@pytest.mark.parametrize("env", _AUTO_ENVS)
def test_doctor_network_defaults_flags_stale_auto_install_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setenv(env, "1")
    check = doctor.check_network_defaults(tmp_path)
    assert check.ok is False
    assert env in check.detail


@pytest.mark.parametrize("module", MODULES)
def test_doctor_network_defaults_flags_lock_residue_without_artifacts(
    tmp_path: Path, module
) -> None:
    lock = module.model_cache_dir(tmp_path) / ".install-lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("reranker:dead-token", encoding="utf-8")

    check = doctor.check_network_defaults(tmp_path)
    assert check.ok is False
    assert ".install-lock" in check.detail

    lock.unlink()
    assert doctor.check_network_defaults(tmp_path).ok is True


@pytest.mark.parametrize("command", ["embedding", "reranker"])
def test_policy_lists_model_commands_as_ci_writes(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    assert command in policy.WRITE_COMMANDS
    monkeypatch.setenv("CI", "1")
    with pytest.raises(policy.PolicyDenied):
        policy.reject_ci_write(command)


@pytest.mark.parametrize("command", ["embedding", "reranker"])
def test_cli_rejects_install_and_uninstall_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, command: str
) -> None:
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(cli, "find_repo_root", lambda: tmp_path)

    for sub in ("install", "uninstall"):
        code = cli.main([command, sub, "--json"])
        assert code == PERMISSION_DENIED, f"{command} {sub} must be CI-rejected"
        assert "CI_READ_ONLY" in capsys.readouterr().out

    # read-only status and verify-only install stay usable in CI
    assert cli.main([command, "status", "--json"]) == 0
    capsys.readouterr()
    cli.main([command, "install", "--verify", "--json"])
    assert "CI_READ_ONLY" not in capsys.readouterr().out
