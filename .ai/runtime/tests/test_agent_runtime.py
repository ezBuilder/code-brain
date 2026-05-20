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


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".ai" / "memory").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return repo


def test_runtime_context_policy_exposes_selected_thresholds(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    result = run_ai(repo, "runtime", "context-policy", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "source" not in payload
    assert payload["policy"]["compression_threshold"] == 0.50
    assert payload["policy"]["gateway_hygiene_threshold"] == 0.85
    assert payload["policy"]["cache_breakpoints"] == "stable-system-plus-last-3"


def test_runtime_insights_links_eval_lessons_and_release_signals(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    eval_dir = repo / ".ai" / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / "cases.jsonl").write_text(
        json.dumps(
            {
                "id": "case-1",
                "kind": "swe",
                "command": "pytest tests",
                "outcome": "pass",
                "duration_ms": 10,
                "created_at": "2026-05-20T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / ".ai" / "memory" / "lessons.jsonl").write_text(
        json.dumps(
            {
                "id": "lesson-1",
                "source": "test",
                "failure": "old failure",
                "cause": "missing context",
                "fix": "persist lesson",
                "tags": ["runtime"],
                "created_at": "2026-05-20T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_ai(repo, "runtime", "insights", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["signals"]["eval"]["total"] == 1
    assert payload["signals"]["lessons"]["total"] == 1
    statuses = {item["id"]: item["status"] for item in payload["applied_patterns"]}
    assert statuses["closed_learning_loop"] == "active"
    assert statuses["context_hygiene"] == "active"
    assert any("persistent memory" in reason for reason in payload["reasons"])
