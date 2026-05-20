from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def run_ai(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_kit_status_reports_missing_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    (repo / "kits" / "global-agent-kit").mkdir(parents=True)

    result = run_ai(repo, "kit", "status", "--json")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["name"] == "code-brain-global-kit"
    assert payload["present"] is True
    assert "README.md" in payload["missing"]
