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


def test_lessons_add_redacts_and_appends_jsonl(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    secret = "AKIA" + "A" * 16

    result = run_ai(
        repo,
        "lessons",
        "add",
        "--source",
        "/Users/example/project",
        "--failure",
        f"command leaked {secret}",
        "--cause",
        "raw output was stored",
        "--fix",
        "redact before append",
        "--tag",
        "redaction",
        "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    record = payload["record"]
    assert record["id"].startswith("lesson-")
    assert record["failure"] == "command leaked [REDACTED]"
    assert record["source"] == "[REDACTED]project"
    assert record["cause"] == "raw output was stored"
    assert record["fix"] == "redact before append"
    assert record["tags"] == ["redaction"]
    assert record["created_at"].endswith("Z")

    lines = (repo / ".ai" / "memory" / "lessons.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record
    assert secret not in lines[0]


def test_lessons_list_returns_latest_first(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    for name in ("first", "second"):
        result = run_ai(
            repo,
            "lessons",
            "add",
            "--source",
            "test",
            "--failure",
            f"{name} failure",
            "--cause",
            f"{name} cause",
            "--fix",
            f"{name} fix",
            "--json",
        )
        assert result.returncode == 0, result.stdout + result.stderr

    result = run_ai(repo, "lessons", "list", "--limit", "1", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["items"][0]["failure"] == "second failure"


def test_lessons_summary_groups_by_source_and_tag(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    cases = [
        ("hook", "pretool blocked", ["routing", "hook"]),
        ("hook", "duplicate surfaced", ["routing"]),
        ("cli", "bad output", ["json"]),
    ]
    for source, failure, tags in cases:
        args = [
            "lessons",
            "add",
            "--source",
            source,
            "--failure",
            failure,
            "--cause",
            "cause",
            "--fix",
            "fix",
            "--json",
        ]
        for tag in tags:
            args.extend(["--tag", tag])
        result = run_ai(repo, *args)
        assert result.returncode == 0, result.stdout + result.stderr

    result = run_ai(repo, "lessons", "summary", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["total"] == 3
    assert payload["by_source"] == {"cli": 1, "hook": 2}
    assert payload["by_tag"] == {"hook": 1, "json": 1, "routing": 2}


def test_add_lesson_rejects_blank_required_fields(tmp_path: Path) -> None:
    from ai_core.lessons import add_lesson

    repo = make_repo(tmp_path)
    payload = add_lesson(repo, source="test", failure="", cause="cause", fix="fix")

    assert payload == {"ok": False, "reason": "missing_required_field"}
    assert not (repo / ".ai" / "memory" / "lessons.jsonl").exists()
