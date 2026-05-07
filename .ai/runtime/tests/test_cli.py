from __future__ import annotations

import json
import os
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
