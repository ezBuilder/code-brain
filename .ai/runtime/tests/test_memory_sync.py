"""Tests for opt-in cross-machine memory auto-sync (P4, ai_core.memory_sync).

Uses a real bare "remote" + two clones (Mac / VPS) to exercise commit-only-memory,
fast-forward push, behind+clean rebase, behind+dirty skip, and conflict abort.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.memory_sync import peer_sync_summary, sync_enabled, sync_once  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _gok(repo: Path, *args: str) -> str:
    p = _git(repo, *args)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout


def _config(repo: Path) -> None:
    _gok(repo, "config", "user.email", "t@t.test")
    _gok(repo, "config", "user.name", "t")
    _gok(repo, "config", "commit.gpgsign", "false")


def _mem(repo: Path) -> Path:
    d = repo / ".ai" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _set_machine_id(repo: Path, mid: str) -> None:
    """Pin a distinct machine_id per clone — two real machines (Mac/VPS) have different
    hostnames; the test clones share one host, so set it explicitly to mirror reality."""
    d = repo / ".ai" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    (d / "machine_id").write_text(mid, encoding="utf-8")


def _origin_with_mac(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _gok(remote, "init", "--bare", "-q")
    mac = tmp_path / "mac"
    mac.mkdir()
    _gok(mac, "init", "-q")
    _config(mac)
    _gok(mac, "remote", "add", "origin", str(remote))
    _mem(mac)
    _set_machine_id(mac, "mac-test")
    (mac / ".ai" / "memory" / "decisions.jsonl").write_text('{"id":"d1"}\n', encoding="utf-8")
    (mac / "code.py").write_text("print(1)\n", encoding="utf-8")
    _gok(mac, "add", "-A")
    _gok(mac, "commit", "-q", "-m", "init")
    _gok(mac, "branch", "-M", "develop")
    _gok(mac, "push", "-q", "-u", "origin", "develop")
    return remote, mac


def _clone(tmp_path: Path, remote: Path, name: str) -> Path:
    dst = tmp_path / name
    _gok(tmp_path, "clone", "-q", str(remote), str(dst))
    _config(dst)
    _gok(dst, "checkout", "-q", "develop")
    _set_machine_id(dst, f"{name}-test")
    return dst


def test_sync_commits_only_memory_not_code_and_pushes(tmp_path: Path) -> None:
    remote, mac = _origin_with_mac(tmp_path)
    # change BOTH a memory file and a code file (uncommitted)
    (mac / ".ai" / "memory" / "decisions.jsonl").write_text('{"id":"d1"}\n{"id":"d2"}\n', encoding="utf-8")
    (mac / "code.py").write_text("print(2)\n", encoding="utf-8")
    res = sync_once(mac, agent="claude")
    assert res["committed"] and res["pushed"], res
    files = _gok(mac, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert any("decisions.jsonl" in f for f in files), files
    assert "code.py" not in files  # code is never committed by the sync
    # the code edit stays uncommitted in the working tree
    assert "code.py" in _gok(mac, "status", "--porcelain")


def test_sync_rebases_when_behind_and_clean(tmp_path: Path) -> None:
    remote, mac = _origin_with_mac(tmp_path)
    vps = _clone(tmp_path, remote, "vps")
    # VPS advances a DIFFERENT memory file and pushes
    (vps / ".ai" / "memory" / "todos.jsonl").write_text('{"id":"t1"}\n', encoding="utf-8")
    assert sync_once(vps, agent="codex")["pushed"]
    # Mac changes its memory file; tree otherwise clean → sync rebases + pushes
    (mac / ".ai" / "memory" / "decisions.jsonl").write_text('{"id":"d1"}\n{"id":"d2"}\n', encoding="utf-8")
    res = sync_once(mac, agent="claude")
    assert res["behind_before"] == 1 and res["rebased"] and res["pushed"], res
    # remote now has both changes
    _gok(mac, "fetch", "-q")
    head_files = _gok(mac, "show", "--name-only", "--pretty=format:", "origin/develop").split()
    assert any("decisions.jsonl" in f for f in head_files)


def test_sync_skips_rebase_when_code_dirty(tmp_path: Path) -> None:
    remote, mac = _origin_with_mac(tmp_path)
    vps = _clone(tmp_path, remote, "vps")
    (vps / ".ai" / "memory" / "todos.jsonl").write_text('{"id":"t1"}\n', encoding="utf-8")
    assert sync_once(vps, agent="codex")["pushed"]
    # Mac has an uncommitted CODE change → rebase is unsafe, must be skipped
    (mac / "code.py").write_text("print(99)\n", encoding="utf-8")
    (mac / ".ai" / "memory" / "decisions.jsonl").write_text('{"id":"d1"}\n{"id":"d2"}\n', encoding="utf-8")
    res = sync_once(mac, agent="claude")
    assert res["behind_before"] == 1 and res["skipped_rebase"] and not res["pushed"], res
    # nothing got rebased/merged; code change still present
    assert "code.py" in _gok(mac, "status", "--porcelain")


def test_sync_aborts_on_conflict_and_leaves_clean_tree(tmp_path: Path) -> None:
    remote, mac = _origin_with_mac(tmp_path)
    # seed a non-union file both sides will edit at the same line
    (mac / ".ai" / "memory" / "session-current.md").write_text("base\n", encoding="utf-8")
    _gok(mac, "add", "--", ".ai/memory/session-current.md")
    _gok(mac, "commit", "-q", "-m", "seed note")
    _gok(mac, "push", "-q")
    vps = _clone(tmp_path, remote, "vps")
    (vps / ".ai" / "memory" / "session-current.md").write_text("vps-line\n", encoding="utf-8")
    assert sync_once(vps, agent="codex")["pushed"]
    (mac / ".ai" / "memory" / "session-current.md").write_text("mac-line\n", encoding="utf-8")
    res = sync_once(mac, agent="claude")
    assert res["conflict"] and not res["pushed"], res
    # rebase was aborted → no rebase in progress, tree usable
    assert not (mac / ".git" / "rebase-merge").exists() and not (mac / ".git" / "rebase-apply").exists()


def test_sync_enabled_reads_config(tmp_path: Path) -> None:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / ".ai" / "config.yaml"
    cfg.write_text("version: 1\nmemory_sync:\n  enabled: false\n", encoding="utf-8")
    assert sync_enabled(tmp_path) is False
    cfg.write_text("version: 1\nmemory_sync:\n  enabled: true\n", encoding="utf-8")
    assert sync_enabled(tmp_path) is True


def test_sync_no_crash_on_non_git_dir(tmp_path: Path) -> None:
    # A project that is not a git repo at all must degrade gracefully (no crash).
    _mem(tmp_path)
    res = sync_once(tmp_path, agent="claude")
    assert res["ok"] is False and "not-a-git-repo" in res["errors"]
    assert res["pushed"] is False and res["committed"] is False


def test_sync_git_repo_without_remote_commits_locally_no_push(tmp_path: Path) -> None:
    # A git repo with NO remote: commit memory locally, but no upstream → no push, no error.
    repo = tmp_path / "r"
    repo.mkdir()
    _gok(repo, "init", "-q")
    _config(repo)
    _mem(repo)
    (repo / ".ai" / "memory" / "decisions.jsonl").write_text('{"id":"d1"}\n', encoding="utf-8")
    res = sync_once(repo, agent="claude")
    assert res["ok"] is True and res["committed"] is True
    assert res["pushed"] is False and "no-upstream" in res["errors"]


def test_peer_sync_summary_lists_other_machines(tmp_path: Path) -> None:
    d = tmp_path / ".ai" / "memory" / "sync"
    d.mkdir(parents=True, exist_ok=True)
    (d / "heartbeat-vps-abc123.json").write_text(
        '{"machine_id":"vps-abc123","agent":"codex","synced_at":"2026-05-29T13:00:00Z"}', encoding="utf-8"
    )
    summary = peer_sync_summary(tmp_path)
    assert "vps-abc123" in summary and "cb-sync" in summary
