"""commit_guard — secret-in-commit gate for the runtime PreToolUse hook (Claude + Codex).

Tokens are assembled at runtime so this file holds no literal secret.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ai_core import commit_guard

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")

_TOKEN = "ghp_" + "b" * 36


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_clean_returns_none(repo: Path) -> None:
    (repo / "clean.txt").write_text("hello\nno secret\n")
    _git(repo, "add", "clean.txt")
    assert commit_guard.commit_secret_reason(repo, "git commit -m x") is None


def test_secret_returns_reason(repo: Path) -> None:
    (repo / "app.conf").write_text(f'token = "{_TOKEN}"\n')
    _git(repo, "add", "app.conf")
    reason = commit_guard.commit_secret_reason(repo, "git commit -m x")
    assert reason and "github_token" in reason


def test_allowlist_returns_none(repo: Path) -> None:
    (repo / "app.conf").write_text(f'token = "{_TOKEN}"\n')
    (repo / ".ai").mkdir()
    (repo / ".ai" / "secret_scan_allowlist.txt").write_text("app.conf\n")
    _git(repo, "add", "app.conf", ".ai/secret_scan_allowlist.txt")
    assert commit_guard.commit_secret_reason(repo, "git commit -m x") is None


def test_non_commit_returns_none(repo: Path) -> None:
    (repo / "app.conf").write_text(f'token = "{_TOKEN}"\n')
    _git(repo, "add", "app.conf")
    assert commit_guard.commit_secret_reason(repo, "git status") is None


def test_dash_a_scans_unstaged(repo: Path) -> None:
    (repo / "f.txt").write_text("v1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "f.txt").write_text(f'token = "{_TOKEN}"\n')  # modified, not staged
    assert commit_guard.commit_secret_reason(repo, "git commit -am up") is not None


def test_handle_hook_emits_deny(repo: Path) -> None:
    from ai_core import hooks

    (repo / ".ai").mkdir(exist_ok=True)
    (repo / ".ai" / "config.yaml").write_text("version: 1\n")
    (repo / "app.conf").write_text(f'token = "{_TOKEN}"\n')
    _git(repo, "add", "app.conf")
    resp = hooks.handle_hook(
        repo, "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "dry": True},
    )
    assert resp.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
