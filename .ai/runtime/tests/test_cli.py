from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable


def run_ai(*args: str, env: dict[str, str] | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    if env:
        merged.update(env)
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=cwd,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_ai_input(
    *args: str,
    stdin: str,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    if env:
        merged.update(env)
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=cwd,
        env=merged,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def copy_repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(
        ROOT,
        target,
        ignore=shutil.ignore_patterns(".git", ".venv", ".pytest_cache", "__pycache__", "cache"),
    )
    for pattern in (
        ".ai/memory/queue/*.json",
        ".ai/memory/queue/.tmp/*.json*",
        ".ai/memory/queue/processing/*.json",
        ".ai/memory/queue/dead/*.json",
        ".ai/memory/audit/*.jsonl",
        ".ai/memory/events/*.jsonl",
    ):
        for path in target.glob(pattern):
            path.unlink()
    (target / ".ai" / "memory" / "audit-index.jsonl").write_text("\n", encoding="utf-8")
    return target


def test_version_json() -> None:
    result = run_ai("--json", "version")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["version"] == "0.1.0"
    assert payload["protocol_version"] == 1


def test_render_dry_run_json() -> None:
    result = run_ai("--json", "render", "--dry-run")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["manifest"]["embedding"]["enabled"] is False


def test_ci_write_rejected_before_render() -> None:
    result = run_ai("render", env={"CI": "true"})
    assert result.returncode == 16


def test_doctor_strict_passes_after_render() -> None:
    render_result = run_ai("render")
    assert render_result.returncode == 0, render_result.stderr
    result = run_ai("doctor", "--strict", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_worker_health_validates_envelope() -> None:
    result = run_ai("worker", "health", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["protocol_version"] == 1
    assert "health" in payload["methods"]


def test_worker_health_rejects_bad_envelope() -> None:
    result = run_ai("worker", "health", "--envelope-json", "{\"protocol_version\":1}")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "INVALID_REQUEST"


def test_hook_appends_redacted_event(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    event_path = repo / ".ai" / "memory" / "events" / "events.jsonl"
    before = event_path.read_text(encoding="utf-8") if event_path.exists() else ""
    secret_value = "secret=" + "abcdefghijklmnopqrstuv" + "wxyz"
    result = run_ai_input(
        "hook",
        "SessionStart",
        "--json",
        stdin=json.dumps({"agent": "codex", "token": secret_value}),
        cwd=repo,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["persisted"] is True
    after = event_path.read_text(encoding="utf-8")
    assert len(after) > len(before)
    assert secret_value not in after
    assert "[REDACTED]" in after


def test_hook_ci_fast_path_does_not_persist(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai_input("hook", "SessionStart", "--json", stdin='{"agent":"codex"}', env={"CI": "true"}, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "ci-fast-path"
    assert payload["persisted"] is False


def test_index_rebuild_and_code_query(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["db_path"] == ".ai/cache/code.sqlite"
    assert payload["indexed"] > 0
    query_result = run_ai("code", "query", "worker", "--json", cwd=repo)
    assert query_result.returncode == 0, query_result.stdout + query_result.stderr
    query_payload = json.loads(query_result.stdout)
    assert query_payload["ok"] is True
    assert query_payload["results"]
    provenance = query_payload["results"][0]["provenance"]
    assert {"processor", "model_hash", "prompt_version", "chunker_version", "confidence"} <= set(provenance)


def test_context_pack_and_mcp_once(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    assert run_ai("index", "rebuild", cwd=repo).returncode == 0
    context_result = run_ai("context", "pack", "manifest", "--json", cwd=repo)
    assert context_result.returncode == 0, context_result.stdout + context_result.stderr
    assert json.loads(context_result.stdout)["additionalContext"]
    request = {"jsonrpc": "2.0", "id": 1, "method": "code_query", "params": {"query": "manifest", "limit": 2}}
    mcp_result = run_ai("mcp", "--once-json", json.dumps(request), cwd=repo)
    assert mcp_result.returncode == 0, mcp_result.stdout + mcp_result.stderr
    response = json.loads(mcp_result.stdout)
    assert response["jsonrpc"] == "2.0"
    assert response["result"]["results"]


def test_ci_index_rebuild_rejected() -> None:
    result = run_ai("index", "rebuild", env={"CI": "true"})
    assert result.returncode == 16


def test_queue_lifecycle_complete(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input(
        "queue",
        "enqueue",
        "--priority",
        "P2",
        "--kind",
        "index",
        "--json",
        stdin='{"target":"all"}',
        cwd=repo,
    )
    assert enqueue_result.returncode == 0, enqueue_result.stdout + enqueue_result.stderr
    job = json.loads(enqueue_result.stdout)["job"]
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    assert lease_result.returncode == 0, lease_result.stdout + lease_result.stderr
    leased = json.loads(lease_result.stdout)["job"]
    assert leased["id"] == job["id"]
    complete_result = run_ai(
        "queue",
        "complete",
        "--job-id",
        leased["id"],
        "--lease-id",
        leased["lease_id"],
        "--json",
        cwd=repo,
    )
    assert complete_result.returncode == 0, complete_result.stdout + complete_result.stderr
    status_result = run_ai("queue", "status", "--json", cwd=repo)
    status = json.loads(status_result.stdout)
    assert status["pending"] == 0
    assert status["processing"] == 0
    assert status["dead"] == 0


def test_queue_fail_and_archive(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P3", "--kind", "notify", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    fail_result = run_ai(
        "queue",
        "fail",
        "--job-id",
        leased["id"],
        "--lease-id",
        leased["lease_id"],
        "--reason",
        "boom",
        "--json",
        cwd=repo,
    )
    assert fail_result.returncode == 0, fail_result.stdout + fail_result.stderr
    archive_result = run_ai("queue", "archive-dead", "--older-than-days", "0", "--json", cwd=repo)
    assert archive_result.returncode == 0, archive_result.stdout + archive_result.stderr
    assert json.loads(archive_result.stdout)["archived"] == 1


def test_ci_queue_write_rejected() -> None:
    result = run_ai_input("queue", "enqueue", "--priority", "P1", "--kind", "test", stdin="{}", env={"CI": "true"})
    assert result.returncode == 16
