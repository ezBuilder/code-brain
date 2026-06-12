"""block-secret-commit.sh — blocks `git commit` of staged secrets (kit PreToolUse hook).

Tokens are assembled at runtime so this test file contains no literal secret (it must not
trip secret_scan or the hook itself when committed).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]  # .ai/runtime/tests -> repo root
HOOK = _REPO / "kits" / "global-agent-kit" / ".claude" / "hooks" / "block-secret-commit.sh"

def _bash_usable() -> bool:
    bash = shutil.which("bash")
    if bash is None:
        return False
    try:
        proc = subprocess.run(
            [bash, "-lc", "printf ok"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return proc.returncode == 0 and proc.stdout == "ok"


pytestmark = pytest.mark.skipif(
    not HOOK.exists() or shutil.which("git") is None or not _bash_usable(),
    reason="hook, git, or usable bash unavailable",
)

_FAKE_TOKEN = "ghp_" + "b" * 36  # not a literal secret pattern in source (assembled)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _run_hook(cwd: Path, command: str) -> str:
    payload = json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)})
    bash = shutil.which("bash") or "bash"
    proc = subprocess.run([bash, str(HOOK)], input=payload, capture_output=True, text=True, timeout=20)
    return proc.stdout


def _denies(out: str) -> bool:
    if not out.strip():
        return False
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_clean_commit_allowed(repo: Path) -> None:
    (repo / "clean.txt").write_text("hello\nno secret here\n")
    _git(repo, "add", "clean.txt")
    assert _run_hook(repo, "git commit -m x").strip() == ""


def test_staged_secret_blocked(repo: Path) -> None:
    (repo / "app.conf").write_text(f'token = "{_FAKE_TOKEN}"\n')
    _git(repo, "add", "app.conf")
    assert _denies(_run_hook(repo, "git commit -m x"))


def test_allowlisted_path_not_blocked(repo: Path) -> None:
    (repo / "app.conf").write_text(f'token = "{_FAKE_TOKEN}"\n')
    (repo / ".ai").mkdir()
    (repo / ".ai" / "secret_scan_allowlist.txt").write_text("app.conf\n")
    _git(repo, "add", "app.conf", ".ai/secret_scan_allowlist.txt")
    assert _run_hook(repo, "git commit -m x").strip() == ""


def test_non_commit_ignored(repo: Path) -> None:
    (repo / "app.conf").write_text(f'token = "{_FAKE_TOKEN}"\n')
    _git(repo, "add", "app.conf")
    assert _run_hook(repo, "git status").strip() == ""


def test_commit_dash_a_scans_unstaged(repo: Path) -> None:
    (repo / "f.txt").write_text("v1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "f.txt").write_text(f'token = "{_FAKE_TOKEN}"\n')  # modified, not staged
    assert _denies(_run_hook(repo, "git commit -am update"))
