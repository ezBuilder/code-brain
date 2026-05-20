from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def run_ai(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def init_git_repo(repo: Path) -> None:
    (repo / ".ai").mkdir(parents=True, exist_ok=True)
    (repo / ".ai" / "config.yaml").write_text("features:\n  embeddings: false\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "release-gate@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Release Gate"], cwd=repo, check=True)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)


def append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def test_release_gate_summary_aggregates_dirty_eval_and_recommendations(tmp_path: Path) -> None:
    from ai_core.release_gate import summary

    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "eval-summary.json").write_text(
        json.dumps({"ok": True, "passed": 3, "failed": 0}),
        encoding="utf-8",
    )
    append_jsonl(repo / ".ai" / "skills" / "catalog.jsonl", {
        "id": "sk-1",
        "slug": "release-runbook",
        "status": "installed",
        "draft": {"description": "x"},
        "created_at": "2026-05-20T00:00:00Z",
        "installed_paths": ["/Users/tester/secret/path.md"],
    })
    append_jsonl(repo / ".ai" / "agents_catalog" / "catalog.jsonl", {
        "id": "ag-1",
        "slug": "release-helper",
        "status": "pending",
        "description": "x",
        "created_at": "2026-05-20T00:00:00Z",
    })
    append_jsonl(repo / ".ai" / "precall_rules" / "catalog.jsonl", {
        "id": "pc-1",
        "kind": "compound_pipeline",
        "pattern": "^pytest\\b.*\\|",
        "status": "active",
        "created_at": "2026-05-20T00:00:00Z",
    })

    payload = summary(repo)

    assert payload["mode"] == {"read_only": True, "network": "disabled"}
    assert payload["git"]["dirty"] is True
    assert payload["git"]["unstaged"] == 1
    assert payload["eval"]["present"] is True
    assert payload["eval"]["passed"] == 3
    assert payload["recommendations"]["skills"]["by_status"] == {"installed": 1}
    assert payload["recommendations"]["agents"]["by_status"] == {"pending": 1}
    assert payload["recommendations"]["precall"]["by_status"] == {"active": 1}
    assert "/Users/" not in json.dumps(payload, sort_keys=True)


def test_release_gate_summary_cli_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    (repo / "dist").mkdir()
    (repo / "dist" / "eval-summary.json").write_text('{"ok": true}\n', encoding="utf-8")

    result = run_ai("release-gate", "summary", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert set(payload) == {
        "doctor",
        "eval",
        "gates",
        "generated_at",
        "git",
        "mode",
        "ok",
        "recommendations",
        "runtime_version",
        "schema_version",
    }
    assert payload["mode"]["read_only"] is True
    assert payload["mode"]["network"] == "disabled"
