from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable


def run_ai(*args: str, env: dict[str, str] | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        merged.pop(name, None)
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
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        merged.pop(name, None)
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
        ignore=shutil.ignore_patterns(".git", ".claude", ".venv", ".pytest_cache", "__pycache__", "cache"),
    )
    for pattern in (
        ".ai/memory/queue/*.json",
        ".ai/memory/queue/.tmp/*.json*",
        ".ai/memory/queue/processing/*.json",
        ".ai/memory/queue/dead/*.json",
        ".ai/memory/audit/*.jsonl",
        ".ai/memory/events/*.jsonl",
        ".ai/memory/inbox/*.json",
        ".ai/cache/logs/*.jsonl",
        ".ai/cache/diagnostics/*",
        ".ai/cache/run/queue.recovery.json",
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


def test_release_gate_summary_schema_and_redaction(monkeypatch) -> None:
    from ai_core.report import release_gate_summary

    status = {
        "release_ready": True,
        "release_artifacts": {
            "all_current": True,
            "release_notes": {"path": "/Users/builder/workspace/code-brain/dist/code-brain-0.1.0.release-notes.md"},
        },
        "doctor": {"checks": [{"name": "layout", "ok": True, "detail": "ok"}]},
    }
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    summary = release_gate_summary(ROOT, git_sha="deadbeef", status=status)
    assert set(summary) == {"schema_version", "generated_at", "git_sha", "ci", "release_ready", "release_artifacts", "checks"}
    assert summary["schema_version"] == 1
    assert summary["generated_at"].endswith("Z")
    assert summary["git_sha"] == "deadbeef"
    assert summary["ci"] is True
    assert summary["release_ready"] is True
    assert "/Users/" not in json.dumps(summary, sort_keys=True)


def test_release_gate_summary_command_json() -> None:
    result = run_ai("report", "release-gate-summary", "--git-sha", "deadbeef", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["git_sha"] == "deadbeef"
    assert payload["schema_version"] == 1


def test_release_gate_workflow_invariants() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")
    assert re.search(r"permissions:\s*\n\s*contents: read", workflow)
    assert "persist-credentials: false" in workflow
    assert "fetch-depth: 0" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "summary-observe" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "summary-parity.py" in workflow
    assert "dist/dep-advisory.json" in workflow
    assert "ubuntu-latest" in workflow
    assert "macos-latest" in workflow
    assert '[[ "$rc" -eq 16 ]]' in workflow
    assert "retention-days: 14" in workflow
    assert "retention-days: 30" in workflow
    assert not re.search(r"\$\{\{\s*secrets\.", workflow)
    assert "gh pr" not in workflow
    assert "git push" not in workflow
    assert "GITHUB_TOKEN" not in workflow


def test_dep_advisory_offline_skip_emits_schema(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    env = os.environ.copy()
    env["CODE_BRAIN_DEP_ADVISORY_OFFLINE"] = "1"
    result = subprocess.run(
        ["bash", "scripts/dep-advisory.sh"],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offline-skipped" in result.stdout
    payload = json.loads((repo / "dist" / "dep-advisory.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["skipped"] == "offline"
    assert payload["findings"] == []
    assert payload["finding_count"] == 0
    assert payload["tool"] == "pip-audit"
    assert payload["mode"] == "advisory"


def test_dep_advisory_findings_do_not_fail_gate(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    raw = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [
                    {
                        "id": "GHSA-xxxx",
                        "fix_versions": ["1.1"],
                        "description": "x" * 300,
                    }
                ],
            }
        ]
    }
    env = os.environ.copy()
    env["CODE_BRAIN_DEP_ADVISORY_RAW"] = json.dumps(raw)
    result = subprocess.run(
        ["bash", "scripts/dep-advisory.sh"],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads((repo / "dist" / "dep-advisory.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["skipped"] is None
    assert payload["finding_count"] == 1
    assert payload["findings"][0]["package"] == "pkg"
    assert payload["findings"][0]["version"] == "1.0"
    assert payload["findings"][0]["id"] == "GHSA-xxxx"
    assert payload["findings"][0]["fix_versions"] == ["1.1"]
    assert len(payload["findings"][0]["description"]) == 240


def test_dep_advisory_release_gate_integration_invariants() -> None:
    script = (ROOT / "scripts" / "dep-advisory.sh").read_text(encoding="utf-8")
    release_gate = (ROOT / "scripts" / "release-gate.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")
    docs_check = (ROOT / "scripts" / "docs-check.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    assert "pip-audit" in script
    assert "finding_count" in script
    assert "./scripts/dep-advisory.sh >/dev/null" in release_gate
    assert "dist/dep-advisory.json" in workflow
    assert "CODE_BRAIN_DEP_ADVISORY_OFFLINE=1 ./scripts/dep-advisory.sh" in docs_check


def test_summary_parity_canonical_subset_passes_with_different_timestamps(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    payload = {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "git_sha": "abc123",
        "release_ready": True,
        "release_artifacts": {"all_present": True, "all_valid": True, "all_current": True},
        "checks": [{"name": "layout", "ok": True, "detail": "ok"}],
    }
    left.write_text(json.dumps(payload), encoding="utf-8")
    changed = dict(payload, generated_at="2026-01-01T00:01:00Z")
    right.write_text(json.dumps(changed), encoding="utf-8")
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(right), "--json"], cwd=ROOT, text=True, stdout=subprocess.PIPE)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout) == {"mismatches": [], "ok": True}


def test_summary_parity_release_ready_mismatch_fails(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    base = {"schema_version": 1, "git_sha": "abc123", "release_ready": True, "release_artifacts": {}, "checks": []}
    left.write_text(json.dumps(base), encoding="utf-8")
    changed = dict(base, release_ready=False)
    right.write_text(json.dumps(changed), encoding="utf-8")
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(right), "--json"], cwd=ROOT, text=True, stdout=subprocess.PIPE)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["mismatches"][0]["field"] == "release_ready"


def test_summary_parity_check_set_mismatch_fails(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    base = {
        "schema_version": 1,
        "git_sha": "abc123",
        "release_ready": True,
        "release_artifacts": {},
        "checks": [{"name": "layout", "ok": True}, {"name": "queue_age", "ok": True}],
    }
    left.write_text(json.dumps(base), encoding="utf-8")
    changed = dict(base, checks=[{"name": "layout", "ok": True}])
    right.write_text(json.dumps(changed), encoding="utf-8")
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(right)], cwd=ROOT, text=True, stderr=subprocess.PIPE)
    assert result.returncode == 1
    assert "queue_age" in result.stderr


def test_summary_parity_missing_or_invalid_file_returns_two(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    missing = tmp_path / "missing.json"
    left.write_text('{"schema_version":1}', encoding="utf-8")
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(missing)], cwd=ROOT, text=True, stderr=subprocess.PIPE)
    assert result.returncode == 2
    assert str(missing) in result.stderr

    left.write_text("not json", encoding="utf-8")
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(missing)], cwd=ROOT, text=True, stderr=subprocess.PIPE)
    assert result.returncode == 2


def test_render_dry_run_json() -> None:
    result = run_ai("--json", "render", "--dry-run")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["manifest"]["embedding"]["enabled"] is False


def test_ci_write_rejected_before_render() -> None:
    result = run_ai("render", "--json", env={"CI": "true"})
    assert result.returncode == 16
    payload = json.loads(result.stdout)
    assert payload["error"] == "CI_READ_ONLY"
    assert payload["command"] == "render"


def test_doctor_strict_passes_after_render() -> None:
    render_result = run_ai("render")
    assert render_result.returncode == 0, render_result.stderr
    result = run_ai("doctor", "--strict", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    slo_check = next(check for check in payload["checks"] if check["name"] == "hot_path_slo")
    assert slo_check["ok"] is True
    assert "target_ms=200" in slo_check["detail"]
    redaction_check = next(check for check in payload["checks"] if check["name"] == "redaction_self_test")
    assert redaction_check["ok"] is True
    preflight_check = next(check for check in payload["checks"] if check["name"] == "bootstrap_preflight")
    assert preflight_check["ok"] is True


def test_preflight_check_only_json(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = subprocess.run(
        ["./scripts/preflight.sh", "--check-only", "--json"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["check_only"] is True
    assert payload["checks"]["python"]["minimum"] == "3.11"
    assert payload["checks"]["sops"]["required"] is False
    assert payload["checks"]["age"]["required"] is False


def test_redaction_expanded_secret_shapes() -> None:
    from ai_core.redact import redact_value

    samples = [
        "AKIA" + "A" * 16,
        "ghp_" + "a" * 36,
        "gho_" + "b" * 36,
        "github_pat_" + "c" * 28,
        "sk-" + "d" * 32,
        "sk-ant-" + "e" * 32,
        "xoxb-" + "1-2-" + "f" * 24,
        "Authorization: Bearer " + "eyJ" + "a" * 20 + "." + "eyJ" + "b" * 20 + "." + "c" * 20,
        "api_key=" + "g" * 24,
        "-----BEGIN " + "PRIVATE KEY-----\n" + "h" * 32 + "\n-----END " + "PRIVATE KEY-----",
        "/Users/example/project",
        "/home/example/project",
        "C:\\Users\\example\\project",
        "192.168.1.10",
    ]
    payload = {"samples": samples, "nested": [{"token": samples[1]}]}
    redacted = json.dumps(redact_value(payload), sort_keys=True)
    for sample in samples:
        assert sample not in redacted
    assert redacted.count("[REDACTED]") >= len(samples)


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
    assert payload["error"] == "UNAUTHORIZED"


def test_worker_health_envelope_error_matrix(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    seed_result = run_ai("worker", "health", "--json", cwd=repo)
    assert seed_result.returncode == 0, seed_result.stdout + seed_result.stderr
    token = (repo / ".ai" / "cache" / "run" / "worker.token").read_text(encoding="utf-8").strip()
    envelope = {
        "protocol_version": 1,
        "token": token,
        "root_id": repo.name,
        "root_hash": hashlib.sha256(repo.resolve().as_posix().encode("utf-8")).hexdigest(),
        "machine_id_hash": hashlib.sha256(b"").hexdigest(),
        "request_id": "matrix",
    }
    cases = [
        ("protocol_version", None, "INCOMPATIBLE_VERSION"),
        ("protocol_version", 999, "INCOMPATIBLE_VERSION"),
        ("token", None, "UNAUTHORIZED"),
        ("token", "wrong", "UNAUTHORIZED"),
        ("root_hash", None, "UNAUTHORIZED"),
        ("root_hash", "wrong", "UNAUTHORIZED"),
        ("request_id", None, "INVALID_REQUEST"),
    ]
    for key, value, expected in cases:
        candidate = dict(envelope)
        if value is None:
            candidate.pop(key)
        else:
            candidate[key] = value
        result = run_ai("worker", "health", "--envelope-json", json.dumps(candidate), cwd=repo)
        assert result.returncode == 1, (key, result.stdout, result.stderr)
        assert json.loads(result.stdout)["error"] == expected


def test_ci_worker_health_does_not_create_token(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    token_path = repo / ".ai" / "cache" / "run" / "worker.token"
    if token_path.exists():
        token_path.unlink()
    result = run_ai("worker", "health", "--json", env={"CI": "true"}, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not token_path.exists()


def test_ci_worker_stop_rejected(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai("worker", "stop", "--force", "--json", env={"CI": "true"}, cwd=repo)
    assert result.returncode == 16
    assert json.loads(result.stdout)["error"] == "CI_READ_ONLY"


def test_worker_status_reports_singleton_lock(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai("worker", "status", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["lock"]["locked"] is False
    assert payload["lock"]["stale"] is False
    assert payload["lock"]["reason"] == "no_lock"
    assert {"cross_host", "hostname_local", "reason", "error"} <= set(payload["lock"])


def test_worker_singleton_lock_rejects_second_instance(tmp_path: Path) -> None:
    from ai_core.worker.lock import WorkerAlreadyRunning, acquire_worker_lock

    repo = copy_repo(tmp_path)
    first = acquire_worker_lock(repo, owner="test")
    try:
        try:
            acquire_worker_lock(repo, owner="test")
        except WorkerAlreadyRunning as exc:
            assert exc.exit_code == 75
        else:
            raise AssertionError("second worker lock acquisition should fail")
    finally:
        first.release()
    assert not (repo / ".ai" / "cache" / "run" / "worker.lock").exists()


def test_worker_singleton_lock_recovers_stale_pid(tmp_path: Path) -> None:
    from ai_core.worker.lock import acquire_worker_lock

    repo = copy_repo(tmp_path)
    lock_file = repo / ".ai" / "cache" / "run" / "worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(json.dumps({"pid": 99999999, "owner": "dead", "acquired_at": "2026-01-01T00:00:00Z"}) + "\n", encoding="utf-8")
    lock = acquire_worker_lock(repo, owner="test")
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["owner"] == "test"
    finally:
        lock.release()


def test_worker_stop_clears_stale_lock(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    lock_file = repo / ".ai" / "cache" / "run" / "worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps(
            {
                "pid": 99999999,
                "owner": "dead",
                "hostname": __import__("socket").gethostname(),
                "acquired_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = run_ai("worker", "stop", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "cleared"
    assert payload["reason"] == "stale_dead_pid"
    assert not lock_file.exists()


def test_worker_stop_refuses_live_lock_without_force(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    lock_file = repo / ".ai" / "cache" / "run" / "worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps({"pid": os.getpid(), "owner": "live", "hostname": __import__("socket").gethostname(), "acquired_at": "2026-01-01T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    result = run_ai("worker", "stop", "--json", cwd=repo)
    assert result.returncode == 14
    payload = json.loads(result.stdout)
    assert payload["action"] == "refused"
    assert payload["reason"] == "live_local"
    assert lock_file.exists()


def test_worker_stop_force_clears_live_local_lock(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    lock_file = repo / ".ai" / "cache" / "run" / "worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps({"pid": os.getpid(), "owner": "live", "hostname": __import__("socket").gethostname(), "acquired_at": "2026-01-01T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    result = run_ai("worker", "stop", "--force", "--reason", "operator-confirmed", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "force_cleared"
    assert payload["force"] is True
    assert not lock_file.exists()


def test_worker_stop_refuses_cross_host_lock_even_with_force(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    lock_file = repo / ".ai" / "cache" / "run" / "worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps({"pid": 123, "owner": "remote", "hostname": "other-host", "acquired_at": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    result = run_ai("worker", "stop", "--force", "--json", cwd=repo)
    assert result.returncode == 14
    payload = json.loads(result.stdout)
    assert payload["action"] == "refused"
    assert payload["reason"] == "cross_host"
    assert lock_file.exists()


def test_queue_lock_serializes_mutation_file(tmp_path: Path) -> None:
    from ai_core.worker.lock import queue_lock

    repo = copy_repo(tmp_path)
    with queue_lock(repo):
        result = run_ai("worker", "status", "--json", cwd=repo)
        assert result.returncode == 0, result.stdout + result.stderr
    lock_file = repo / ".ai" / "cache" / "run" / "queue.lock"
    assert lock_file.exists()
    assert oct(lock_file.stat().st_mode & 0o777) == "0o600"


def test_doctor_rejects_stale_worker_lock(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    lock_file = repo / ".ai" / "cache" / "run" / "worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(json.dumps({"pid": 99999999, "owner": "dead", "acquired_at": "2026-01-01T00:00:00Z"}) + "\n", encoding="utf-8")
    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode == 10
    payload = json.loads(result.stdout)
    check = next(check for check in payload["checks"] if check["name"] == "worker_singleton_lock")
    assert check["ok"] is False


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

    secret_value = "secret=" + "abcdefghijklmnopqrstuv" + "wxyz"
    bad_request = {"jsonrpc": "2.0", "id": 2, "method": "code_query", "params": {"query": "manifest", "limit": secret_value}}
    bad_result = run_ai("mcp", "--once-json", json.dumps(bad_request), cwd=repo)
    assert bad_result.returncode == 0, bad_result.stdout + bad_result.stderr
    assert secret_value not in bad_result.stdout
    assert "[REDACTED]" in bad_result.stdout


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


def test_queue_jobs_default_max_attempts(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0, enqueue_result.stdout + enqueue_result.stderr
    job = json.loads(enqueue_result.stdout)["job"]
    assert job["attempts"] == 0
    assert job["max_attempts"] == 3


def test_queue_enqueue_accepts_max_attempts(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input(
        "queue",
        "enqueue",
        "--priority",
        "P2",
        "--kind",
        "index",
        "--max-attempts",
        "2",
        "--json",
        stdin="{}",
        cwd=repo,
    )
    assert enqueue_result.returncode == 0, enqueue_result.stdout + enqueue_result.stderr
    assert json.loads(enqueue_result.stdout)["job"]["max_attempts"] == 2

    bad_result = run_ai_input(
        "queue",
        "enqueue",
        "--priority",
        "P2",
        "--kind",
        "index",
        "--max-attempts",
        "0",
        "--json",
        stdin="{}",
        cwd=repo,
    )
    assert bad_result.returncode == 1
    assert "max_attempts must be between" in json.loads(bad_result.stdout)["error"]


def test_recover_expired_requeues_below_max_and_releases_new_lease(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    old_lease_id = leased["lease_id"]
    processing_path = repo / ".ai" / "memory" / "queue" / "processing" / f"{leased['id']}.json"
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing_path.write_text(json.dumps(job, sort_keys=True) + "\n", encoding="utf-8")
    recover_result = run_ai("queue", "recover-expired", "--json", cwd=repo)
    assert recover_result.returncode == 0, recover_result.stdout + recover_result.stderr
    recovered = json.loads(recover_result.stdout)
    assert recovered["recovered"] == 1
    assert recovered["dead_lettered"] == 0
    assert recovered["promoted"] == 0
    assert recovered["recovery"]["last_recovered"] == 1
    next_lease = run_ai("queue", "lease", "--worker-id", "worker-2", "--json", cwd=repo)
    leased_again = json.loads(next_lease.stdout)["job"]
    assert leased_again["id"] == leased["id"]
    assert leased_again["lease_id"] != old_lease_id
    assert leased_again["attempts"] == 2


def test_recover_expired_dead_letters_at_max_attempts(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    processing_path = repo / ".ai" / "memory" / "queue" / "processing" / f"{leased['id']}.json"
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["attempts"] = 3
    job["max_attempts"] = 3
    job["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing_path.write_text(json.dumps(job, sort_keys=True) + "\n", encoding="utf-8")
    recover_result = run_ai("queue", "recover-expired", "--json", cwd=repo)
    assert recover_result.returncode == 0, recover_result.stdout + recover_result.stderr
    recovered = json.loads(recover_result.stdout)
    assert recovered["recovered"] == 0
    assert recovered["dead_lettered"] == 1
    assert recovered["promoted"] == 1
    dead_path = repo / ".ai" / "memory" / "queue" / "dead" / f"{leased['id']}.json"
    dead = json.loads(dead_path.read_text(encoding="utf-8"))
    assert dead["status"] == "dead"
    assert dead["failure_reason"] == "max_attempts_exceeded:3/3"
    assert dead["attempt_history"][-1]["outcome"] == "promoted_dead"


def test_lease_next_triggers_due_recovery_sweep(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    processing_path = repo / ".ai" / "memory" / "queue" / "processing" / f"{leased['id']}.json"
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing_path.write_text(json.dumps(job, sort_keys=True) + "\n", encoding="utf-8")
    (repo / ".ai" / "cache" / "run" / "queue.recovery.json").unlink()

    swept_lease = run_ai("queue", "lease", "--worker-id", "worker-2", "--json", cwd=repo)
    assert swept_lease.returncode == 0, swept_lease.stdout + swept_lease.stderr
    leased_again = json.loads(swept_lease.stdout)["job"]
    assert leased_again["id"] == leased["id"]
    state = json.loads((repo / ".ai" / "cache" / "run" / "queue.recovery.json").read_text(encoding="utf-8"))
    assert state["last_recovered"] == 1


def test_lease_next_skips_recent_recovery_sweep(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    processing_path = repo / ".ai" / "memory" / "queue" / "processing" / f"{leased['id']}.json"
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing_path.write_text(json.dumps(job, sort_keys=True) + "\n", encoding="utf-8")

    empty_lease = run_ai("queue", "lease", "--worker-id", "worker-2", "--json", cwd=repo)
    assert empty_lease.returncode == 0, empty_lease.stdout + empty_lease.stderr
    assert json.loads(empty_lease.stdout)["job"] is None
    assert processing_path.exists()


def test_queue_status_counts_expired_processing(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    processing_path = repo / ".ai" / "memory" / "queue" / "processing" / f"{leased['id']}.json"
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing_path.write_text(json.dumps(job, sort_keys=True) + "\n", encoding="utf-8")
    status_result = run_ai("queue", "status", "--json", cwd=repo)
    assert status_result.returncode == 0, status_result.stdout + status_result.stderr
    status = json.loads(status_result.stdout)
    assert status["expired_processing"] == 1
    assert status["recovery"]["expired_processing"] == 1


def test_queue_age_empty_returns_zero(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    status_result = run_ai("queue", "status", "--json", cwd=repo)
    assert status_result.returncode == 0, status_result.stdout + status_result.stderr
    status = json.loads(status_result.stdout)
    assert status["oldest_pending_age_seconds"] == 0
    assert status["oldest_pending_job_id"] is None
    assert status["oldest_processing_age_seconds"] == 0
    assert status["oldest_processing_job_id"] is None
    assert status["age_stats_skipped"] == 0

    metrics_result = run_ai("obs", "metrics", "--json", cwd=repo)
    assert metrics_result.returncode == 0, metrics_result.stdout + metrics_result.stderr
    metrics = json.loads(metrics_result.stdout)["queue"]
    assert metrics["oldest_pending_age_seconds"] == 0
    assert metrics["oldest_processing_age_seconds"] == 0

    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 0, doctor_result.stdout + doctor_result.stderr
    check = next(check for check in json.loads(doctor_result.stdout)["checks"] if check["name"] == "queue_age")
    assert check["ok"] is True


def test_queue_age_pending_stale_fails_doctor_and_skips_corrupt_jobs(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    job = json.loads(enqueue_result.stdout)["job"]
    queue_dir = repo / ".ai" / "memory" / "queue"
    pending_path = queue_dir / f"{job['id']}.json"
    payload = json.loads(pending_path.read_text(encoding="utf-8"))
    payload["created_at"] = "2000-01-01T00:00:00Z"
    pending_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    (queue_dir / "p2-corrupt.json").write_text("{not-json\n", encoding="utf-8")

    status_result = run_ai("queue", "status", "--json", cwd=repo)
    assert status_result.returncode == 0, status_result.stdout + status_result.stderr
    status = json.loads(status_result.stdout)
    assert status["oldest_pending_age_seconds"] >= 86400
    assert status["oldest_pending_job_id"] == job["id"]
    assert status["age_stats_skipped"] == 1

    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 10
    check = next(check for check in json.loads(doctor_result.stdout)["checks"] if check["name"] == "queue_age")
    assert check["ok"] is False
    assert job["id"] in check["detail"]


def test_queue_age_processing_stale_fails_doctor(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    processing_path = repo / ".ai" / "memory" / "queue" / "processing" / f"{leased['id']}.json"
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["leased_at"] = "2000-01-01T00:00:00Z"
    job["lease_expires_at"] = "2999-01-01T00:00:00Z"
    processing_path.write_text(json.dumps(job, sort_keys=True) + "\n", encoding="utf-8")

    status_result = run_ai("queue", "status", "--json", cwd=repo)
    assert status_result.returncode == 0, status_result.stdout + status_result.stderr
    status = json.loads(status_result.stdout)
    assert status["oldest_processing_age_seconds"] >= 600
    assert status["oldest_processing_job_id"] == leased["id"]

    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 10
    check = next(check for check in json.loads(doctor_result.stdout)["checks"] if check["name"] == "queue_age")
    assert check["ok"] is False
    assert "processing" in check["detail"]


def test_obs_health_summary_clean_returns_ok_true(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai("obs", "health-summary", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["doctor"]["ok"] is True
    assert payload["doctor"]["failed"] == []
    assert payload["worker"]["locked"] is False
    assert payload["queue"]["oldest_pending_age_seconds"] == 0
    assert payload["queue"]["oldest_processing_age_seconds"] == 0


def test_obs_health_summary_reflects_stale_worker_lock_but_exits_zero(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    lock_file = repo / ".ai" / "cache" / "run" / "worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps(
            {
                "pid": 99999999,
                "owner": "dead",
                "hostname": __import__("socket").gethostname(),
                "acquired_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_ai("obs", "health-summary", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["doctor"]["ok"] is False
    assert "worker_singleton_lock" in payload["doctor"]["failed"]
    assert payload["worker"]["stale"] is True
    assert payload["worker"]["reason"] == "stale_dead_pid"


def test_obs_health_summary_reflects_aged_queue_without_release_summary(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    (repo / "dist" / "release-gate.summary.json").unlink(missing_ok=True)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    job = json.loads(enqueue_result.stdout)["job"]
    pending_path = repo / ".ai" / "memory" / "queue" / f"{job['id']}.json"
    payload = json.loads(pending_path.read_text(encoding="utf-8"))
    payload["created_at"] = "2000-01-01T00:00:00Z"
    pending_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    result = run_ai("obs", "health-summary", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["ok"] is False
    assert summary["queue"]["oldest_pending_age_seconds"] >= 86400
    assert summary["release_artifacts"] == {
        "summary_path": None,
        "release_ready": None,
        "all_present": None,
        "all_valid": None,
        "all_current": None,
    }


def test_obs_health_summary_release_artifacts_are_redacted_and_informational(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    summary_path = repo / "dist" / "release-gate.summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "release_ready": False,
                "release_artifacts": {
                    "all_present": True,
                    "all_valid": True,
                    "all_current": False,
                    "release_notes": {"path": "/Users/builder/workspace/code-brain/dist/code-brain-0.1.0.release-notes.md"},
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_ai("obs", "health-summary", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/Users/" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["release_artifacts"] == {
        "summary_path": "dist/release-gate.summary.json",
        "release_ready": False,
        "all_present": True,
        "all_valid": True,
        "all_current": False,
    }


def test_doctor_rejects_expired_processing_lease(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    enqueue_result = run_ai_input("queue", "enqueue", "--priority", "P2", "--kind", "index", "--json", stdin="{}", cwd=repo)
    assert enqueue_result.returncode == 0
    lease_result = run_ai("queue", "lease", "--worker-id", "worker-1", "--json", cwd=repo)
    leased = json.loads(lease_result.stdout)["job"]
    processing_path = repo / ".ai" / "memory" / "queue" / "processing" / f"{leased['id']}.json"
    job = json.loads(processing_path.read_text(encoding="utf-8"))
    job["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing_path.write_text(json.dumps(job, sort_keys=True) + "\n", encoding="utf-8")
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 10
    check = next(check for check in json.loads(doctor_result.stdout)["checks"] if check["name"] == "queue_lease_recovery")
    assert check["ok"] is False


def test_doctor_rejects_stale_queue_recovery_state(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    state_path = repo / ".ai" / "cache" / "run" / "queue.recovery.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"ok": True, "last_run_at": "2000-01-01T00:00:00Z", "last_recovered": 0}) + "\n",
        encoding="utf-8",
    )
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 10
    check = next(check for check in json.loads(doctor_result.stdout)["checks"] if check["name"] == "queue_lease_recovery")
    assert check["ok"] is False
    assert "stale" in check["detail"]


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


def test_queue_dead_empty_returns_zero_count(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai("queue", "dead", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["matched"] == 0
    assert payload["returned"] == 0
    assert payload["skipped"] == 0
    assert payload["items"] == []


def test_queue_dead_sorted_desc_by_failed_at(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    dead_dir = repo / ".ai" / "memory" / "queue" / "dead"
    dead_dir.mkdir(parents=True, exist_ok=True)
    for job_id, failed_at in [
        ("job-old", "2026-01-01T00:00:00Z"),
        ("job-new", "2026-01-01T02:00:00Z"),
        ("job-mid", "2026-01-01T01:00:00Z"),
    ]:
        (dead_dir / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "id": job_id,
                    "priority": "P2",
                    "kind": "index",
                    "status": "dead",
                    "attempts": 2,
                    "max_attempts": 3,
                    "failed_at": failed_at,
                    "failure_reason": "boom",
                    "payload": {"secret": "hidden"},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    result = run_ai("queue", "dead", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    items = json.loads(result.stdout)["items"]
    assert [item["id"] for item in items] == ["job-new", "job-mid", "job-old"]
    assert all(isinstance(item["age_seconds"], int) and item["age_seconds"] >= 0 for item in items)


def test_queue_dead_respects_limit_and_since(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    dead_dir = repo / ".ai" / "memory" / "queue" / "dead"
    dead_dir.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        failed_at = f"2026-01-01T0{index}:00:00Z"
        (dead_dir / f"job-{index}.json").write_text(
            json.dumps({"id": f"job-{index}", "priority": "P3", "kind": "notify", "failed_at": failed_at}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    limited = run_ai("queue", "dead", "--limit", "2", "--json", cwd=repo)
    assert limited.returncode == 0, limited.stdout + limited.stderr
    limited_payload = json.loads(limited.stdout)
    assert limited_payload["count"] == 5
    assert limited_payload["matched"] == 5
    assert limited_payload["returned"] == 2
    assert len(limited_payload["items"]) == 2

    since = run_ai("queue", "dead", "--since", "2026-01-01T02:30:00Z", "--json", cwd=repo)
    assert since.returncode == 0, since.stdout + since.stderr
    since_payload = json.loads(since.stdout)
    assert since_payload["count"] == 5
    assert since_payload["matched"] == 2
    assert [item["id"] for item in since_payload["items"]] == ["job-4", "job-3"]

    too_many = run_ai("queue", "dead", "--limit", "999", "--json", cwd=repo)
    assert too_many.returncode == 1
    assert "limit must be between" in json.loads(too_many.stdout)["error"]


def test_queue_dead_omits_payload_and_counts_skipped(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    dead_dir = repo / ".ai" / "memory" / "queue" / "dead"
    dead_dir.mkdir(parents=True, exist_ok=True)
    secret_value = "secret-token-" + "AKIA" + "0" * 16
    (dead_dir / "safe.json").write_text(
        json.dumps(
            {
                "id": "safe",
                "priority": "P1",
                "kind": "index",
                "failed_at": "2026-01-01T00:00:00Z",
                "failure_reason": "boom",
                "payload": {"token": secret_value},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (dead_dir / "corrupt.json").write_text("{not-json\n", encoding="utf-8")
    result = run_ai("queue", "dead", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["matched"] == 1
    assert payload["returned"] == 1
    assert payload["skipped"] == 1
    assert "payload" not in result.stdout
    assert "secret-token" not in result.stdout
    assert "AKIA" + "0" * 16 not in result.stdout


def test_ci_queue_write_rejected() -> None:
    result = run_ai_input(
        "queue",
        "enqueue",
        "--priority",
        "P1",
        "--kind",
        "test",
        "--json",
        stdin="{}",
        env={"CI": "true"},
    )
    assert result.returncode == 16
    assert json.loads(result.stdout)["error"] == "CI_READ_ONLY"


def test_ci_mutation_commands_rejected(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    commands = [
        ("render",),
        ("queue", "lease", "--worker-id", "ci"),
        ("queue", "recover-expired"),
        ("queue", "archive-dead", "--older-than-days", "0"),
        ("worker", "stop", "--force"),
        ("trust", "revoke", "missing"),
        ("inbox", "approve", "missing"),
        ("inbox", "reject", "missing"),
        ("diagnostics", "prune", "--keep-days", "1"),
        ("migrate",),
        ("upgrade", "rollback", "--backup-path", ".ai/cache/upgrade/missing.json"),
        ("upgrade", "clean-cache"),
        ("index", "rebuild"),
    ]
    for command in commands:
        result = run_ai(*command, env={"CI": "true"}, cwd=repo)
        assert result.returncode == 16, command + (result.stdout, result.stderr)


def test_ci_read_only_commands_allowed(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    commands = [
        ("queue", "dead", "--json"),
        ("queue", "status", "--json"),
        ("trust", "list", "--json"),
        ("secrets", "status", "--json"),
        ("inbox", "list", "--json"),
        ("report", "status", "--json"),
    ]
    for command in commands:
        result = run_ai(*command, env={"CI": "true"}, cwd=repo)
        assert result.returncode == 0, command + (result.stdout, result.stderr)


def test_github_actions_write_policy_matches_ci(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    write_result = run_ai("render", "--json", env={"GITHUB_ACTIONS": "true"}, cwd=repo)
    assert write_result.returncode == 16
    assert json.loads(write_result.stdout)["error"] == "CI_READ_ONLY"
    read_result = run_ai("queue", "status", "--json", env={"GITHUB_ACTIONS": "true"}, cwd=repo)
    assert read_result.returncode == 0, read_result.stdout + read_result.stderr


def test_ci_vendor_and_flag_matrix_reject_writes_before_worker_token(tmp_path: Path) -> None:
    for index, (args, env) in enumerate(
        [
            (("render", "--json"), {"CI": "1"}),
            (("render", "--json"), {"GITHUB_ACTIONS": "true"}),
            (("render", "--json"), {"GITLAB_CI": "true"}),
            (("render", "--json"), {"AI_CI": "true"}),
            (("--ci", "render", "--json"), {}),
        ]
    ):
        repo = copy_repo(tmp_path / f"repo-{index}")
        token_path = repo / ".ai" / "cache" / "run" / "worker.token"
        if token_path.exists():
            token_path.unlink()
        result = run_ai(*args, env=env, cwd=repo)
        assert result.returncode == 16, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["error"] == "CI_READ_ONLY"
        assert payload["command"] == "render"
        assert not token_path.exists()


def test_ci_memory_and_audit_writes_rejected(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    memory_result = run_ai_input("memory", "append-event", "--json", stdin='{"kind":"ci"}', env={"CI": "true"}, cwd=repo)
    assert memory_result.returncode == 16
    audit_result = run_ai_input(
        "audit",
        "append",
        "--action",
        "ci.audit",
        "--json",
        stdin="{}",
        env={"CI": "true"},
        cwd=repo,
    )
    assert audit_result.returncode == 16


def test_audit_append_updates_index_consistently(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai_input(
        "audit",
        "append",
        "--action",
        "test.audit",
        "--category",
        "test",
        "--json",
        stdin='{"note":"audit payload"}',
        cwd=repo,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    index_records = [
        json.loads(line)
        for line in (repo / ".ai" / "memory" / "audit-index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert index_records[-1]["ts"] == record["ts"]
    assert index_records[-1]["action"] == "test.audit"
    assert index_records[-1]["category"] == "test"
    assert index_records[-1]["path"].startswith(".ai/memory/audit/")
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 0, doctor_result.stdout + doctor_result.stderr


def test_doctor_rejects_audit_index_mismatch(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai_input("audit", "append", "--action", "test.audit", "--json", stdin="{}", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    (repo / ".ai" / "memory" / "audit-index.jsonl").write_text(
        '{"ts":"2099-01-01T00:00:00Z","action":"wrong","category":"manual","path":".ai/memory/audit/2099.jsonl"}\n',
        encoding="utf-8",
    )
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 10
    payload = json.loads(doctor_result.stdout)
    audit_check = next(check for check in payload["checks"] if check["name"] == "audit_index")
    assert audit_check["ok"] is False


def test_trust_init_render_and_revoke(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_result = run_ai("trust", "init", "--name", "local-test", "--json", cwd=repo)
    assert init_result.returncode == 0, init_result.stdout + init_result.stderr
    init_payload = json.loads(init_result.stdout)
    assert init_payload["machine_id_hash"]
    assert (repo / init_payload["private_key_path"]).exists()
    assert (repo / init_payload["trust_file"]).exists()
    render_result = run_ai("render", cwd=repo)
    assert render_result.returncode == 0, render_result.stdout + render_result.stderr
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert doctor_result.returncode == 0, doctor_result.stdout + doctor_result.stderr
    list_result = run_ai("trust", "list", "--json", cwd=repo)
    machines = json.loads(list_result.stdout)["machines"]
    assert machines[0]["status"] == "trusted"
    revoke_result = run_ai("trust", "revoke", init_payload["machine_id_hash"], "--json", cwd=repo)
    assert revoke_result.returncode == 0, revoke_result.stdout + revoke_result.stderr
    assert json.loads(revoke_result.stdout)["status"] == "revoked"


def test_secrets_status_and_ci_trust_rejected() -> None:
    status_result = run_ai("secrets", "status", "--json")
    assert status_result.returncode == 0, status_result.stdout + status_result.stderr
    payload = json.loads(status_result.stdout)
    assert payload["ok"] is True
    assert payload["plaintext_tracked"] is False
    ci_result = run_ai("trust", "init", "--name", "ci", env={"CI": "true"})
    assert ci_result.returncode == 16


def test_inbox_approval_lifecycle_and_redaction(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    secret_value = "secret=" + "abcdefghijklmnopqrstuv" + "wxyz"
    request_result = run_ai_input(
        "inbox",
        "request",
        "--gate",
        "remote_enable",
        "--summary",
        "Enable outbound adapter",
        "--json",
        stdin=json.dumps({"token": secret_value}),
        cwd=repo,
    )
    assert request_result.returncode == 0, request_result.stdout + request_result.stderr
    approval = json.loads(request_result.stdout)["approval"]
    assert approval["gate"] == "remote_enable"
    assert secret_value not in request_result.stdout
    list_result = run_ai("inbox", "list", "--json", cwd=repo)
    assert json.loads(list_result.stdout)["approvals"][0]["status"] == "pending"
    approve_result = run_ai("inbox", "approve", approval["approval_id"], "--json", cwd=repo)
    assert approve_result.returncode == 0, approve_result.stdout + approve_result.stderr
    assert json.loads(approve_result.stdout)["approval"]["status"] == "approved"


def test_notify_enqueue_is_p3_redacted_and_ci_rejected(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    secret_value = "secret=" + "abcdefghijklmnopqrstuv" + "wxyz"
    result = run_ai_input(
        "notify",
        "enqueue",
        "--channel",
        "telegram",
        "--json",
        stdin=json.dumps({"summary": "hello", "body": secret_value}),
        cwd=repo,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["queued"]["priority"] == "P3"
    assert secret_value not in result.stdout
    assert "[REDACTED]" in result.stdout
    ci_result = run_ai_input("notify", "enqueue", "--channel", "telegram", stdin="{}", env={"CI": "true"}, cwd=repo)
    assert ci_result.returncode == 16


def test_inbox_rejects_non_gate(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai_input("inbox", "request", "--gate", "trust_change", "--summary", "bad", stdin="{}", cwd=repo)
    assert result.returncode == 1


def test_obs_log_metrics_slo_and_diagnostics(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    secret_value = "secret=" + "abcdefghijklmnopqrstuv" + "wxyz"
    log_result = run_ai_input(
        "obs",
        "log",
        "--event",
        "test.event",
        "--json",
        stdin=json.dumps({"token": secret_value}),
        cwd=repo,
    )
    assert log_result.returncode == 0, log_result.stdout + log_result.stderr
    log_payload = json.loads(log_result.stdout)
    assert secret_value not in log_result.stdout
    assert "[REDACTED]" in log_result.stdout
    assert (repo / log_payload["path"]).exists()
    metrics_result = run_ai("obs", "metrics", "--json", cwd=repo)
    assert metrics_result.returncode == 0, metrics_result.stdout + metrics_result.stderr
    assert json.loads(metrics_result.stdout)["queue"]["pending"] == 0
    slo_result = run_ai("obs", "slo", "--iterations", "2", "--json", cwd=repo)
    assert slo_result.returncode == 0, slo_result.stdout + slo_result.stderr
    assert json.loads(slo_result.stdout)["target_ms"] == 200
    assert not (repo / ".ai" / "memory" / "events" / "events.jsonl").exists()
    dry_result = run_ai("diagnostics", "bundle", "--dry-run", "--json", cwd=repo)
    assert dry_result.returncode == 0, dry_result.stdout + dry_result.stderr
    assert json.loads(dry_result.stdout)["dry_run"] is True
    bundle_result = run_ai("diagnostics", "bundle", "--json", cwd=repo)
    assert bundle_result.returncode == 0, bundle_result.stdout + bundle_result.stderr
    bundle_path = repo / json.loads(bundle_result.stdout)["path"]
    assert bundle_path.exists()
    with zipfile.ZipFile(bundle_path) as archive:
        names = archive.namelist()
        assert len(names) == 1
        assert names[0].startswith("diagnostics-")
        assert names[0].endswith(".json")
        assert not any(name.endswith(".enc.yaml") or name.endswith(".enc.yml") for name in names)
        bundle = json.loads(archive.read(names[0]).decode("utf-8"))
    assert set(bundle) == {"created_at", "doctor", "metrics", "platform", "runtime_version"}
    assert set(bundle["metrics"]) == {"cache", "ok", "queue", "runtime_version"}


def test_ci_diagnostics_write_rejected_but_dry_run_allowed(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    metrics_result = run_ai("obs", "metrics", "--json", env={"CI": "true"}, cwd=repo)
    assert metrics_result.returncode == 0, metrics_result.stdout + metrics_result.stderr
    log_result = run_ai_input("obs", "log", "--event", "ci", stdin="{}", env={"CI": "true"}, cwd=repo)
    assert log_result.returncode == 16
    dry_result = run_ai("diagnostics", "bundle", "--dry-run", "--json", env={"CI": "true"}, cwd=repo)
    assert dry_result.returncode == 0, dry_result.stdout + dry_result.stderr
    write_result = run_ai("diagnostics", "bundle", "--json", env={"CI": "true"}, cwd=repo)
    assert write_result.returncode == 16


def test_migrate_and_upgrade_plan_apply_rollback(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    migrate_result = run_ai("migrate", "--dry-run", "--json", cwd=repo)
    assert migrate_result.returncode == 0, migrate_result.stdout + migrate_result.stderr
    migrate_payload = json.loads(migrate_result.stdout)
    assert migrate_payload["schema_version"] == 1
    plan_result = run_ai("upgrade", "plan", "--target-version", "0.1.1", "--json", cwd=repo)
    assert plan_result.returncode == 0, plan_result.stdout + plan_result.stderr
    assert json.loads(plan_result.stdout)["compatible"] is True
    apply_result = run_ai("upgrade", "apply", "--target-version", "0.1.1", "--json", cwd=repo)
    assert apply_result.returncode == 0, apply_result.stdout + apply_result.stderr
    backup_path = json.loads(apply_result.stdout)["backup_path"]
    assert (repo / backup_path).exists()
    rollback_result = run_ai("upgrade", "rollback", "--backup-path", backup_path, "--json", cwd=repo)
    assert rollback_result.returncode == 0, rollback_result.stdout + rollback_result.stderr
    incompatible_result = run_ai("upgrade", "plan", "--target-version", "9.0.0", "--json", cwd=repo)
    assert incompatible_result.returncode == 1


def test_upgrade_dry_run_does_not_mutate_or_create_backup(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    manifest = repo / ".ai" / "generated" / "manifest.json"
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    before_mtime = manifest.stat().st_mtime_ns

    dry_result = run_ai("upgrade", "apply", "--target-version", "0.1.1", "--dry-run", "--json", cwd=repo)
    assert dry_result.returncode == 0, dry_result.stdout + dry_result.stderr
    payload = json.loads(dry_result.stdout)
    assert payload["dry_run"] is True
    assert not (repo / payload["backup_path"]).exists()
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before
    assert manifest.stat().st_mtime_ns == before_mtime
    assert not (repo / ".ai" / "cache" / "upgrade").exists()


def test_rollback_overwrites_manifest_drift(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    manifest = repo / ".ai" / "generated" / "manifest.json"
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    apply_result = run_ai("upgrade", "apply", "--target-version", "0.1.1", "--json", cwd=repo)
    assert apply_result.returncode == 0, apply_result.stdout + apply_result.stderr
    backup_path = json.loads(apply_result.stdout)["backup_path"]

    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n{\"drift\": true}\n", encoding="utf-8")
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() != before
    rollback_result = run_ai("upgrade", "rollback", "--backup-path", backup_path, "--json", cwd=repo)
    assert rollback_result.returncode == 0, rollback_result.stdout + rollback_result.stderr
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before


def test_rollback_drill_script_does_not_mutate_worktree() -> None:
    manifest = ROOT / ".ai" / "generated" / "manifest.json"
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    before_mtime = manifest.stat().st_mtime_ns
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "rollback-drill.sh")],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rollback drill ok" in result.stdout
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before
    assert manifest.stat().st_mtime_ns == before_mtime
    rollback_script = (ROOT / "scripts" / "rollback-drill.sh").read_text(encoding="utf-8")
    assert "unset CI GITHUB_ACTIONS GITLAB_CI AI_CI" in rollback_script
    assert "rollback-drill.sh" in (ROOT / "scripts" / "release-gate.sh").read_text(encoding="utf-8")


def test_bootstrap_idempotency_integration_invariants() -> None:
    script = (ROOT / "scripts" / "bootstrap-idempotency.sh").read_text(encoding="utf-8")
    release_gate = (ROOT / "scripts" / "release-gate.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    docs_check = (ROOT / "scripts" / "docs-check.sh").read_text(encoding="utf-8")
    assert "COPYFILE_DISABLE=1" in script
    assert "--exclude './.git'" in script
    assert "--exclude './.ai/cache'" in script
    assert "--exclude './dist'" in script
    assert "CI=true GITHUB_ACTIONS=true ./bootstrap.sh" in script
    assert "git status --short" in script
    assert "manifest_sha" in script
    assert "env -u CI -u GITHUB_ACTIONS -u GITLAB_CI -u AI_CI uv run" in bootstrap
    assert "./scripts/bootstrap-idempotency.sh >/dev/null" in release_gate
    assert "bootstrap-idempotency:" in makefile
    assert "make -n bootstrap-idempotency" in docs_check


def test_ci_migrate_upgrade_write_policy(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    dry_migrate = run_ai("migrate", "--dry-run", "--json", env={"CI": "true"}, cwd=repo)
    assert dry_migrate.returncode == 0, dry_migrate.stdout + dry_migrate.stderr
    migrate_write = run_ai("migrate", "--json", env={"CI": "true"}, cwd=repo)
    assert migrate_write.returncode == 16
    dry_upgrade = run_ai("upgrade", "apply", "--target-version", "0.1.1", "--dry-run", "--json", env={"CI": "true"}, cwd=repo)
    assert dry_upgrade.returncode == 0, dry_upgrade.stdout + dry_upgrade.stderr
    upgrade_write = run_ai("upgrade", "apply", "--target-version", "0.1.1", "--json", env={"CI": "true"}, cwd=repo)
    assert upgrade_write.returncode == 16


def test_report_status_and_release_notes(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    status_result = run_ai("report", "status", "--json", cwd=repo)
    assert status_result.returncode == 0, status_result.stdout + status_result.stderr
    payload = json.loads(status_result.stdout)
    assert payload["runtime_version"] == "0.1.0"
    assert payload["protocol_version"] == 1
    assert payload["doctor"]["ok"] is True
    assert isinstance(payload["release_ready"], bool)
    artifacts = payload["release_artifacts"]
    assert isinstance(artifacts["all_current"], bool)
    if artifacts["all_present"]:
        assert artifacts["archive"]["checksum_valid"] is True
        assert artifacts["manifest"]["valid"] is True
        assert artifacts["manifest"]["file_count"] > 0
        assert artifacts["sbom"]["valid"] is True
        assert artifacts["sbom"]["package_count"] > 0
        assert artifacts["provenance"]["valid"] is True
        assert "current" in artifacts["provenance"]
        assert "git_head_matches" in artifacts["provenance"]
        assert artifacts["release_notes"]["valid"] is True
        assert artifacts["release_notes"]["git_head_valid"] is True
        assert artifacts["release_notes"]["git_status_valid"] is True
        assert artifacts["all_valid"] is True
    else:
        assert artifacts["archive"]["archive_exists"] is False
    notes_result = run_ai("report", "release-notes", cwd=repo)
    assert notes_result.returncode == 0, notes_result.stdout + notes_result.stderr
    assert "Code Brain 0.1.0 Release Notes" in notes_result.stdout
    assert "SBOM" in notes_result.stdout
    assert "./scripts/docs-check.sh" in notes_result.stdout
    assert "./scripts/release-gate.sh" in notes_result.stdout


def test_report_status_rejects_release_notes_git_mismatch(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    notes_path = repo / "dist" / "code-brain-0.1.0.release-notes.md"
    if not notes_path.exists():
        return
    text = notes_path.read_text(encoding="utf-8")
    notes_path.write_text(text.replace("- Git status: `clean`", "- Git status: `dirty`", 1), encoding="utf-8")
    status_result = run_ai("report", "status", "--json", cwd=repo)
    assert status_result.returncode == 1, status_result.stdout + status_result.stderr
    payload = json.loads(status_result.stdout)
    assert payload["release_ready"] is False
    assert payload["release_artifacts"]["release_notes"]["valid"] is False
    assert payload["release_artifacts"]["release_notes"]["git_status_valid"] is False
