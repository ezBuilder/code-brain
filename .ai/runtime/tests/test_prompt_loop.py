"""prompt-loop — pending prompt-patch catalog (human-approved, never auto-applies)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_core import prompt_loop as pl

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run_ai(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([PYTHON, "-m", "ai_core.cli", *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


def test_propose_list_accept_flow(tmp_path: Path) -> None:
    p = pl.propose(tmp_path, target="global_claude", rationale="too verbose", patch="keep reports <=50 chars")
    pid = p["patch"]["id"]
    assert p["patch"]["status"] == "pending"
    pending = pl.list_patches(tmp_path, status="pending")["patches"]
    assert any(r["id"] == pid for r in pending)
    assert pl.set_status(tmp_path, patch_id=pid, status="accepted")["patch"]["status"] == "accepted"
    # idempotent guard: cannot re-decide
    again = pl.set_status(tmp_path, patch_id=pid, status="rejected")
    assert again["ok"] is False and again["reason"] == "already_accepted"
    assert pl.list_patches(tmp_path, status="pending")["patches"] == []


def test_propose_validates(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pl.propose(tmp_path, target="bogus", rationale="r", patch="p")
    with pytest.raises(ValueError):
        pl.propose(tmp_path, target="global_claude", rationale="", patch="p")
    with pytest.raises(ValueError):
        pl.propose(tmp_path, target="global_claude", rationale="r", patch="")


def test_set_status_not_found(tmp_path: Path) -> None:
    r = pl.set_status(tmp_path, patch_id="pp-missing", status="accepted")
    assert r["ok"] is False and r["reason"] == "not_found"


def test_measure_tokens_shape(tmp_path: Path) -> None:
    out = pl.measure_tokens(tmp_path)
    # never raises; ok or graceful failure
    assert "ok" in out


def test_cli_propose_accept_roundtrip(tmp_path: Path) -> None:
    (tmp_path / ".ai" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n")
    proposed = _run_ai("prompt-loop", "propose", "--target", "global_claude",
                       "--rationale", "verbose", "--patch", "be terse", "--json", cwd=tmp_path)
    assert proposed.returncode == 0, proposed.stderr
    pid = json.loads(proposed.stdout)["patch"]["id"]
    accepted = _run_ai("prompt-loop", "accept", "--id", pid, "--json", cwd=tmp_path)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["patch"]["status"] == "accepted"


def test_cli_blocked_in_ci(tmp_path: Path) -> None:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n")
    env = os.environ.copy()
    env["CI"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run([PYTHON, "-m", "ai_core.cli", "prompt-loop", "propose",
                           "--target", "global_claude", "--rationale", "r", "--patch", "p", "--json"],
                          cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode != 0  # write blocked under CI read-only policy
