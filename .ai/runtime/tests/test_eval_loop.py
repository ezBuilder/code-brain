from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def _init_repo(path: Path) -> Path:
    repo = path / "repo"
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return repo


def _run_ai(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        env.pop(name, None)
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_record_case_appends_redacted_minimal_fields(tmp_path: Path) -> None:
    from ai_core.eval_loop import record_case

    repo = _init_repo(tmp_path)
    payload = record_case(
        repo,
        case_id="case-1",
        kind="swe",
        command="pytest /Users/example/project/tests",
        outcome="pass",
        duration_ms=123,
        created_at="2026-05-20T00:00:00Z",
    )

    assert payload["ok"] is True
    path = repo / ".ai" / "eval" / "cases.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    case = json.loads(lines[0])
    assert set(case) == {"id", "kind", "command", "outcome", "duration_ms", "created_at"}
    assert case["id"] == "case-1"
    assert case["command"] == "pytest [REDACTED]project/tests"


def test_summary_counts_pass_rate_and_latest_failures(tmp_path: Path) -> None:
    from ai_core.eval_loop import record_case, summarize_cases

    repo = _init_repo(tmp_path)
    record_case(repo, case_id="pass-1", kind="swe", command="pytest a", outcome="pass", duration_ms=10)
    record_case(repo, case_id="fail-1", kind="swe", command="pytest b", outcome="fail", duration_ms=20)
    record_case(repo, case_id="fail-2", kind="swe", command="pytest c", outcome="error", duration_ms=30)

    summary = summarize_cases(repo, latest_limit=1)
    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 2
    assert summary["pass_rate"] == 0.3333
    assert [case["id"] for case in summary["latest_failures"]] == ["fail-2"]


def test_eval_cli_record_and_summary_json(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    recorded = _run_ai(
        repo,
        "eval",
        "record",
        "--id",
        "cli-1",
        "--kind",
        "swe",
        "--command",
        "pytest .ai/runtime/tests/test_eval_loop.py",
        "--outcome",
        "fail",
        "--duration-ms",
        "44",
        "--json",
    )
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    record_payload = json.loads(recorded.stdout)
    assert record_payload["case"]["id"] == "cli-1"

    summarized = _run_ai(repo, "eval", "summary", "--json")
    assert summarized.returncode == 0, summarized.stdout + summarized.stderr
    summary = json.loads(summarized.stdout)
    assert summary["total"] == 1
    assert summary["failed"] == 1
    assert summary["latest_failures"][0]["id"] == "cli-1"
