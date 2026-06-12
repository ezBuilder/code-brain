from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def run_ai(*args: str, env: dict[str, str] | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        merged.pop(name, None)
    merged["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
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
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
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


def _copy_repo_ignore(src: str, names: list[str]) -> set[str]:
    rel = Path(src).resolve()
    blocked = {".git", ".venv", ".pytest_cache", "__pycache__", "cache", "kits"}
    if rel.name == ".claude":
        return {n for n in names if n != "commands"}
    return {n for n in names if n in blocked}


def copy_repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(ROOT, target, ignore=_copy_repo_ignore)
    for pattern in (
        ".ai/memory/queue/*.json",
        ".ai/memory/queue/.tmp/*.json*",
        ".ai/memory/queue/processing/*.json",
        ".ai/memory/queue/dead/*.json",
        ".ai/memory/loop/inbox/*.json",
        ".ai/memory/loop/processing/*.json",
        ".ai/memory/loop/done/*.json",
        ".ai/memory/loop/dead/*.json",
        ".ai/memory/loop/.tmp/*.json*",
        ".ai/memory/audit/*.jsonl",
        ".ai/memory/events/*.jsonl",
        ".ai/memory/inbox/*.json",
        ".ai/memory/decisions.jsonl",
        ".ai/memory/todos.jsonl",
        ".ai/memory/session-current.md",
        ".ai/skills/catalog.jsonl",
        ".ai/agents_catalog/catalog.jsonl",
        ".ai/precall_rules/catalog.jsonl",
        ".ai/cache/logs/*.jsonl",
        ".ai/cache/diagnostics/*",
        ".ai/cache/run/queue.recovery.json",
    ):
        for path in target.glob(pattern):
            path.unlink()
    (target / ".ai" / "memory" / "audit-index.jsonl").write_text("\n", encoding="utf-8")
    return target


def init_package_repo(repo: Path) -> None:
    shutil.rmtree(repo / "dist", ignore_errors=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "package-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Package Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "package-test-baseline"], cwd=repo, check=True)


def test_version_json() -> None:
    result = run_ai("--json", "version")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["version"] == "0.1.0"
    assert payload["protocol_version"] == 1


def test_release_gate_summary_schema_and_redaction(monkeypatch) -> None:
    from ai_core.report import RELEASE_GATE_SUMMARY_FIELDS, RELEASE_GATE_SUMMARY_SCHEMA_VERSION, release_gate_summary

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
    assert set(summary) == RELEASE_GATE_SUMMARY_FIELDS
    assert summary["schema_version"] == RELEASE_GATE_SUMMARY_SCHEMA_VERSION
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
    assert payload["schema_version"] == 2
    assert set(payload["dep_advisory"]) == {"finding_count", "mode", "generated_at", "skipped"}


def test_release_gate_summary_schema_guard_rejects_drift() -> None:
    from ai_core.report import RELEASE_GATE_SUMMARY_SCHEMA_VERSION, assert_release_gate_summary_schema

    payload = {
        "schema_version": RELEASE_GATE_SUMMARY_SCHEMA_VERSION,
        "generated_at": "2026-01-01T00:00:00Z",
        "git_sha": "abc123",
        "ci": True,
        "release_ready": True,
        "release_artifacts": {},
        "dep_advisory": {"finding_count": 0, "mode": "advisory", "generated_at": "2026-01-01T00:00:00Z", "skipped": None},
        "checks": [],
    }
    assert_release_gate_summary_schema(payload)
    with_extra = dict(payload, unexpected=True)
    try:
        assert_release_gate_summary_schema(with_extra)
    except ValueError as exc:
        assert "extra=['unexpected']" in str(exc)
    else:
        raise AssertionError("extra summary field should fail schema guard")
    wrong_version = dict(payload, schema_version=1)
    try:
        assert_release_gate_summary_schema(wrong_version)
    except ValueError as exc:
        assert "schema version mismatch" in str(exc)
    else:
        raise AssertionError("schema version drift should fail schema guard")


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


def test_obs_usage_compact_by_default() -> None:
    result = run_ai("obs", "usage", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    usage = payload["actual_token_usage"]
    assert "sessions" not in usage["claude"]
    assert "sessions" not in usage["codex"]


def test_obs_usage_include_sessions_opt_in() -> None:
    result = run_ai("obs", "usage", "--include-sessions", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    usage = payload["actual_token_usage"]
    assert "sessions" in usage["claude"]
    assert "sessions" in usage["codex"]


def test_uv_lock_check_release_gate_integration_invariants() -> None:
    script = (ROOT / "scripts" / "lockfile-check.sh").read_text(encoding="utf-8")
    release_gate = (ROOT / "scripts" / "release-gate.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    docs_check = (ROOT / "scripts" / "docs-check.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "uv lock --check --project .ai/runtime" in script
    assert "uv lock --project .ai/runtime" in script
    assert "./scripts/lockfile-check.sh >/dev/null" in release_gate
    assert release_gate.find("./scripts/lockfile-check.sh") < release_gate.find("./bootstrap.sh")
    assert "lockfile-check:" in makefile
    assert "lock-check:" in makefile
    assert "./scripts/lockfile-check.sh" in makefile
    assert "make -n lockfile-check" in docs_check
    assert "make -n lock-check" in docs_check
    assert "./scripts/lockfile-check.sh" in docs_check
    assert "scripts/lockfile-check.sh" in readme
    assert "uv lock --check --project .ai/runtime" in readme
    assert "scripts/lockfile-check.sh" in operations


def test_lockfile_check_passes_without_modifying_lock(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    lockfile = repo / ".ai" / "runtime" / "uv.lock"
    before = (lockfile.stat().st_mtime_ns, hashlib.sha256(lockfile.read_bytes()).hexdigest())
    result = subprocess.run(["bash", "scripts/lockfile-check.sh"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    after = (lockfile.stat().st_mtime_ns, hashlib.sha256(lockfile.read_bytes()).hexdigest())
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "lockfile-check ok"
    assert result.stderr == ""
    assert after == before


def test_lockfile_check_handles_missing_lockfile(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    (repo / ".ai" / "runtime" / "uv.lock").unlink()
    result = subprocess.run(["bash", "scripts/lockfile-check.sh"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert result.returncode == 1
    assert "missing .ai/runtime/uv.lock" in result.stderr
    assert "remediation" in result.stderr
    assert "uv lock --project .ai/runtime" in result.stderr


def test_summary_parity_canonical_subset_passes_with_different_timestamps(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    payload = {
        "schema_version": 2,
        "generated_at": "2026-01-01T00:00:00Z",
        "git_sha": "abc123",
        "ci": True,
        "release_ready": True,
        "release_artifacts": {"all_present": True, "all_valid": True, "all_current": True},
        "dep_advisory": {"finding_count": 0, "mode": "advisory", "generated_at": "2026-01-01T00:00:00Z", "skipped": None},
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
    base = {
        "schema_version": 2,
        "generated_at": "2026-01-01T00:00:00Z",
        "git_sha": "abc123",
        "ci": True,
        "release_ready": True,
        "release_artifacts": {},
        "dep_advisory": {"finding_count": 0, "mode": "advisory", "generated_at": "2026-01-01T00:00:00Z", "skipped": None},
        "checks": [],
    }
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
        "schema_version": 2,
        "generated_at": "2026-01-01T00:00:00Z",
        "git_sha": "abc123",
        "ci": True,
        "release_ready": True,
        "release_artifacts": {},
        "dep_advisory": {"finding_count": 0, "mode": "advisory", "generated_at": "2026-01-01T00:00:00Z", "skipped": None},
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
    left.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at": "2026-01-01T00:00:00Z",
                "git_sha": "abc123",
                "ci": True,
                "release_ready": True,
                "release_artifacts": {},
                "dep_advisory": {"finding_count": 0, "mode": "advisory", "generated_at": "2026-01-01T00:00:00Z", "skipped": None},
                "checks": [],
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(missing)], cwd=ROOT, text=True, stderr=subprocess.PIPE)
    assert result.returncode == 2
    assert str(missing) in result.stderr

    left.write_text("not json", encoding="utf-8")
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(missing)], cwd=ROOT, text=True, stderr=subprocess.PIPE)
    assert result.returncode == 2


def test_summary_parity_schema_drift_returns_two(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    payload = {
        "schema_version": 2,
        "generated_at": "2026-01-01T00:00:00Z",
        "git_sha": "abc123",
        "ci": True,
        "release_ready": True,
        "release_artifacts": {},
        "dep_advisory": {"finding_count": 0, "mode": "advisory", "generated_at": "2026-01-01T00:00:00Z", "skipped": None},
        "checks": [],
    }
    left.write_text(json.dumps(dict(payload, unexpected=True)), encoding="utf-8")
    right.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(["python", "scripts/summary-parity.py", str(left), str(right), "--json"], cwd=ROOT, text=True, stdout=subprocess.PIPE)
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False
    assert "release gate summary schema fields mismatch" in json.loads(result.stdout)["error"]


def test_render_dry_run_json() -> None:
    result = run_ai("--json", "render", "--dry-run")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["manifest"]["embedding"]["enabled"] is False


def test_render_manifest_only_preserves_existing_agent_docs(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    agents = repo / "AGENTS.md"
    claude = repo / "CLAUDE.md"
    agents.write_text("existing project instructions\n", encoding="utf-8")
    claude.write_text("existing claude instructions\n", encoding="utf-8")
    manifest = repo / ".ai" / "generated" / "manifest.json"
    manifest.unlink()

    result = run_ai("render", "--manifest-only", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert [item["path"] for item in payload["planned"]] == [".ai/generated/manifest.json"]
    assert manifest.exists()
    assert agents.read_text(encoding="utf-8") == "existing project instructions\n"
    assert claude.read_text(encoding="utf-8") == "existing claude instructions\n"


def test_ci_write_rejected_before_render() -> None:
    result = run_ai("render", "--json", env={"CI": "true"})
    assert result.returncode == 16
    payload = json.loads(result.stdout)
    assert payload["error"] == "CI_READ_ONLY"
    assert payload["command"] == "render"


def test_doctor_strict_passes_after_render() -> None:
    render_result = run_ai("render")
    assert render_result.returncode == 0, render_result.stderr
    rebuild_result = run_ai("index", "rebuild", "--json")
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr
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


def test_preflight_accepts_namespaced_code_brain_bootstrap(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    (repo / "bootstrap-code-brain.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo / "bootstrap.sh").unlink()

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
    assert payload["checks"]["repo_layout"]["ok"] is True


def test_secret_scan_uses_git_baseline_not_local_noise(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    (repo / ".env").write_text("GITHUB_TOKEN=ghp_" + "a" * 36 + "\n", encoding="utf-8")
    node_modules = repo / "node_modules" / "pkg"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("token=ghp_" + "b" * 36 + "\n", encoding="utf-8")

    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    secret_check = next(check for check in payload["checks"] if check["name"] == "secret_scan")
    assert secret_check["ok"] is True


def test_code_index_uses_git_baseline_and_skips_dependencies(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    untracked_needle = "Unique" + "UntrackedNeedle"
    dependency_needle = "Unique" + "DependencyNeedle"
    (repo / "untracked-note.md").write_text(untracked_needle + "\n", encoding="utf-8")
    node_modules = repo / "node_modules" / "pkg"
    node_modules.mkdir(parents=True)
    (node_modules / "README.md").write_text(dependency_needle + "\n", encoding="utf-8")

    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr
    untracked = run_ai("code", "query", untracked_needle, "--json", cwd=repo)
    dependency = run_ai("code", "query", dependency_needle, "--json", cwd=repo)
    # Schema v8 indexes untracked, non-ignored git source files (git ls-files --others);
    # only gitignored deps (node_modules) stay out of the baseline.
    assert json.loads(untracked.stdout)["results"][0]["path"] == "untracked-note.md"
    assert json.loads(dependency.stdout)["results"] == []


def test_code_index_includes_tsx_and_skips_virtualenvs(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    tsx = repo / "extension_chrome" / "src" / "EditorOverlay.tsx"
    tsx.parent.mkdir(parents=True)
    overlay_needle = "Draggable" + "OverlayNeedle"
    virtualenv_needle = "Virtualenv" + "NoiseNeedle"
    tsx.write_text(f"export function EditorOverlay() {{ return '{overlay_needle}'; }}\n", encoding="utf-8")
    venv_file = repo / "tools" / "voice-cli" / "venv" / "lib" / "python3.11" / "site-packages" / "noise.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text(virtualenv_needle + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "index-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Index Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "index-test-baseline"], cwd=repo, check=True)

    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    overlay = run_ai("code", "query", overlay_needle, "--json", cwd=repo)
    virtualenv = run_ai("code", "query", virtualenv_needle, "--json", cwd=repo)
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr
    assert json.loads(overlay.stdout)["results"][0]["path"] == "extension_chrome/src/EditorOverlay.tsx"
    assert json.loads(virtualenv.stdout)["results"] == []


def test_code_index_skips_generated_browser_noise(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    wanted = repo / "src" / "useful.ts"
    wanted.parent.mkdir(parents=True)
    wanted.write_text("export const usefulNeedleForSearch = true;\n", encoding="utf-8")
    noise_prefix = "Generated" + "NoiseNeedle"
    noisy_files = [
        repo / ".playwright-mcp" / "capture.json",
        repo / "backend" / "public" / "assets" / "app.js.map",
        repo / "app" / "js" / "jquery.min.js",
        repo / "package-lock.json",
    ]
    for index, path in enumerate(noisy_files):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{noise_prefix}{index}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "noise-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Noise Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "noise-test-baseline"], cwd=repo, check=True)

    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    wanted_result = run_ai("code", "query", "usefulNeedleForSearch", "--json", cwd=repo)
    noisy_result = run_ai("code", "query", f"{noise_prefix}0", "--json", cwd=repo)
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr
    assert json.loads(wanted_result.stdout)["results"][0]["path"] == "src/useful.ts"
    assert json.loads(noisy_result.stdout)["results"] == []


def test_code_index_schema_v2_does_not_store_full_content(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    second_result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert second_result.returncode == 0, second_result.stdout + second_result.stderr
    db = repo / ".ai" / "cache" / "code.sqlite"
    with sqlite3.connect(db) as conn:
        user_version = conn.execute("pragma user_version").fetchone()[0]
        columns = [row[1] for row in conn.execute("pragma table_info(chunks)").fetchall()]
    assert user_version == 8
    assert "content" not in columns
    query_result = run_ai("code", "query", "worker", "--json", cwd=repo)
    payload = json.loads(query_result.stdout)
    assert payload["results"]
    assert payload["results"][0]["snippet"]


def test_code_query_marks_stale_lazy_snippets(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    indexed = repo / "src" / "stale.ts"
    indexed.parent.mkdir(parents=True, exist_ok=True)
    indexed.write_text("export const staleNeedleForIndex = true;\n", encoding="utf-8")
    init_package_repo(repo)

    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    indexed.write_text("export const changedAfterIndex = true;\n", encoding="utf-8")
    # Disable query auto-refresh so the stale-snippet path (FTS row vs changed source) is
    # exercised; with auto-refresh on (the default) query would re-index the dirty file first.
    query_result = run_ai("code", "query", "staleNeedleForIndex", "--json", cwd=repo, env={"AI_SEARCH_AUTO_REFRESH": "0"})
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    query_payload = json.loads(query_result.stdout)
    doctor_payload = json.loads(doctor_result.stdout)
    freshness = next(check for check in doctor_payload["checks"] if check["name"] == "index_freshness")

    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr
    assert query_result.returncode == 0, query_result.stdout + query_result.stderr
    assert query_payload["results"][0]["path"] == "src/stale.ts"
    assert query_payload["results"][0]["snippet"].startswith("[stale index: source changed")
    assert doctor_result.returncode == 10
    assert freshness["ok"] is False
    assert "src/stale.ts" in freshness["detail"]


def test_code_index_migrates_legacy_content_schema(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    db = repo / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            create table chunks (
              id integer primary key,
              path text not null,
              sha256 text not null,
              content text not null,
              updated_at text default current_timestamp
            );
            create virtual table chunks_fts using fts5(path, content, content='chunks', content_rowid='id');
            """
        )
    result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("pragma table_info(chunks)").fetchall()]
        user_version = conn.execute("pragma user_version").fetchone()[0]
    assert user_version == 8
    assert "content" not in columns
    assert "summary" in columns


def test_code_query_rejects_legacy_schema_without_dropping_it(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    db = repo / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            create table chunks (
              id integer primary key,
              path text not null,
              sha256 text not null,
              content text not null,
              updated_at text default current_timestamp
            );
            create virtual table chunks_fts using fts5(path, content, content='chunks', content_rowid='id');
            """
        )

    query_result = run_ai("code", "query", "worker", "--json", cwd=repo)
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("pragma table_info(chunks)").fetchall()]
    query_payload = json.loads(query_result.stdout)
    doctor_payload = json.loads(doctor_result.stdout)
    freshness = next(check for check in doctor_payload["checks"] if check["name"] == "index_freshness")

    assert query_result.returncode != 0
    assert "legacy search index schema" in query_payload["error"]
    assert "content" in columns
    assert doctor_result.returncode == 10
    assert freshness["ok"] is False
    assert "legacy index schema" in freshness["detail"]


def test_vector_retriever_config_is_explicitly_not_default(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    config = repo / ".ai" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("retriever: bm25", "retriever: vector"),
        encoding="utf-8",
    )

    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    query_result = run_ai("code", "query", "worker", "--json", cwd=repo)
    doctor_payload = json.loads(doctor_result.stdout)
    config_check = next(check for check in doctor_payload["checks"] if check["name"] == "config")
    query_payload = json.loads(query_result.stdout)

    assert doctor_result.returncode == 10
    assert config_check["ok"] is False
    assert "not implemented" in config_check["detail"]
    assert query_result.returncode != 0
    assert query_payload["ok"] is False
    assert "not implemented" in query_payload["error"]


def test_git_baseline_scanners_tolerate_deleted_tracked_files(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    deleted = repo / "README.md"
    deleted.unlink()

    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert doctor_result.returncode == 0, doctor_result.stdout + doctor_result.stderr
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr


def test_code_index_skips_invalid_utf8_tracked_text_candidates(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    bad = repo / "bad.md"
    bad.write_bytes(b"\xa4not-utf8")
    subprocess.run(["git", "add", "bad.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add-invalid-utf8"], cwd=repo, check=True)

    result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr


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


def test_obs_search_reports_cache_and_measured_context_bytes(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    assert run_ai("index", "rebuild", cwd=repo).returncode == 0

    result = run_ai("obs", "search", "--query", "worker", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    query = payload["query"]
    assert payload["ok"] is True
    assert payload["exists"] is True
    assert payload["schema_version"] == 8
    assert payload["retriever"] == "bm25"
    assert payload["sqlite_bytes"] > 0
    assert payload["indexed_files"] > 0
    assert payload["indexed_bytes"] > 0
    assert payload["doctor"]["index_freshness"]["ok"] is True
    assert "search-fast" in payload["doctor"]["index_freshness"]["detail"]
    assert query["result_count"] > 0
    assert query["matched_indexed_bytes"] >= query["context_bytes"]
    assert 0 <= query["context_to_matched_bytes_ratio"] <= 1
    assert query["additionalContext"]
    assert "estimated_context_tokens" not in query


def test_obs_search_does_not_run_full_doctor(tmp_path: Path, monkeypatch) -> None:
    repo = copy_repo(tmp_path)
    assert run_ai("index", "rebuild", cwd=repo).returncode == 0

    import ai_core.doctor as doctor_mod
    from ai_core.obs import search_report

    def _boom(*_args, **_kwargs):
        raise AssertionError("obs search must not run full doctor checks")

    monkeypatch.setattr(doctor_mod, "run_checks", _boom)
    payload = search_report(repo, query_text="worker", limit=5)

    assert payload["ok"] is True
    assert payload["doctor"]["index_freshness"]["ok"] is True
    assert "search-fast" in payload["doctor"]["index_freshness"]["detail"]


def test_obs_usage_reads_actual_claude_transcript_tokens(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    claude_home = tmp_path / "claude-home"
    transcript = claude_home / "projects" / "-Users-test-repo" / "session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    request = {
        "requestId": "req-1",
        "sessionId": "session-1",
        "timestamp": "2026-05-07T00:00:00.000Z",
        "cwd": str(repo),
        "message": {
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 13,
                "cache_creation_input_tokens": 17,
                "cache_read_input_tokens": 19,
            },
        },
    }
    transcript.write_text(json.dumps(request) + "\n" + json.dumps(request) + "\n", encoding="utf-8")

    result = run_ai(
        "obs", "usage", "--json",
        cwd=repo,
        env={"CLAUDE_HOME": str(claude_home), "CODEX_HOME": str(tmp_path / "no-codex-home")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    claude = payload["actual_token_usage"]["claude"]
    assert claude["source"] == "claude_transcript"
    assert claude["sessions_matched"] == 1
    assert claude["messages"] == 1
    assert claude["tokens"]["input_tokens"] == 11
    assert claude["tokens"]["output_tokens"] == 13
    assert claude["tokens"]["cache_creation_input_tokens"] == 17
    assert claude["tokens"]["cache_read_input_tokens"] == 19
    assert claude["total_observed_tokens"] == 60
    assert payload["actual_token_usage"]["codex"]["source"] == "codex_transcript_unavailable"
    assert payload["claims"]["context_reduction"].startswith("measured in bytes")


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


def test_session_start_rebuilds_missing_index_and_records_hook(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    db = repo / ".ai" / "cache" / "code.sqlite"
    db.unlink(missing_ok=True)
    event_path = repo / ".ai" / "memory" / "events" / "events.jsonl"
    before = event_path.read_text(encoding="utf-8") if event_path.exists() else ""
    result = run_ai("session", "start", "--agent", "codex", "--query", "manifest", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["agent"] == "codex"
    assert payload["index"]["rebuilt"] is True
    assert payload["index"]["before"]["reason"] == "missing"
    assert payload["hook"]["hook"] == "SessionStart"
    assert payload["hook"]["persisted"] is True
    assert payload["context"]["additionalContext"]
    assert db.exists()
    assert len(event_path.read_text(encoding="utf-8")) > len(before)


def test_session_start_dry_run_is_ci_safe_and_does_not_write(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    db = repo / ".ai" / "cache" / "code.sqlite"
    db.unlink(missing_ok=True)
    event_path = repo / ".ai" / "memory" / "events" / "events.jsonl"
    before = event_path.read_text(encoding="utf-8") if event_path.exists() else ""
    result = run_ai("session", "start", "--dry-run", "--agent", "codex", "--json", env={"CI": "true"}, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["index"]["dry_run"] is True
    assert payload["index"]["would_rebuild"] is True
    assert payload["hook"]["mode"] == "ci-fast-path"
    assert payload["hook"]["persisted"] is False
    assert not db.exists()
    assert (event_path.read_text(encoding="utf-8") if event_path.exists() else "") == before


def test_session_start_write_is_rejected_in_ci(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    result = run_ai("session", "start", "--json", env={"CI": "true"}, cwd=repo)
    assert result.returncode == 16
    payload = json.loads(result.stdout)
    assert payload["error"] == "CI_READ_ONLY"
    assert payload["command"] == "session"


def test_session_start_non_strict_allows_doctor_warnings(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    leak = repo / "tracked-secret.txt"
    leak.write_text("token=ghp_" + "a" * 36 + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "session-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Session Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "session-test-baseline"], cwd=repo, check=True)

    normal = run_ai("session", "start", "--agent", "codex", "--json", cwd=repo)
    strict = run_ai("session", "start", "--agent", "codex", "--strict", "--json", cwd=repo)
    normal_payload = json.loads(normal.stdout)
    strict_payload = json.loads(strict.stdout)
    assert normal.returncode == 0, normal.stdout + normal.stderr
    assert normal_payload["ok"] is True
    assert normal_payload["doctor"]["ok"] is False
    assert strict.returncode == 10
    assert strict_payload["ok"] is False


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
    failed_names = {entry["name"] for entry in payload["doctor"]["failed"]}
    assert "worker_singleton_lock" in failed_names
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


def test_loop_submit_claim_complete_roundtrip(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    instruction = repo / "loop-task.md"
    rubric = repo / "loop-rubric.md"
    result_file = repo / "loop-result.md"
    instruction.write_text("Build the feature with maker and reviewer separation.\n", encoding="utf-8")
    rubric.write_text("- tests pass\n- reviewer approves\n", encoding="utf-8")
    result_file.write_text("verified locally\n", encoding="utf-8")

    submit = run_ai(
        "loop",
        "submit",
        "--file",
        "loop-task.md",
        "--goal",
        "Loop feature",
        "--rubric-file",
        "loop-rubric.md",
        "--checklist",
        "tests pass",
        "--source-agent",
        "claude",
        "--target-agent",
        "codex",
        "--priority",
        "P1",
        "--json",
        cwd=repo,
    )
    assert submit.returncode == 0, submit.stdout + submit.stderr
    submitted = json.loads(submit.stdout)["request"]
    assert submitted["status"] == "pending"
    assert submitted["source_agent"] == "claude"
    assert submitted["target_agent"] == "codex"
    assert "reviewer approves" in submitted["rubric"]
    assert submitted["checklist"] == ["tests pass"]
    assert submitted["path"].startswith(".ai/memory/loop/inbox/")

    status = run_ai("loop", "status", "--json", cwd=repo)
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)["pending"] == 1

    claim = run_ai("loop", "claim", "--orchestrator-id", "codex-loop", "--agent", "codex", "--json", cwd=repo)
    assert claim.returncode == 0, claim.stdout + claim.stderr
    claimed_payload = json.loads(claim.stdout)
    claimed = claimed_payload["request"]
    assert claimed["id"] == submitted["id"]
    assert claimed["status"] == "processing"
    assert claimed["lease_id"]
    assert claimed_payload["contract"]["reviewer_required"] is True
    handoff_path = repo / ".ai" / "memory" / "handoff.json"
    assert handoff_path.exists()
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff["goal"] == "Loop feature"
    assert "maker" in claimed_payload["contract"]
    assert "checker" in claimed_payload["contract"]
    assert "reviewer approves" in claimed_payload["contract"]["rubric"]

    blocked_complete = run_ai(
        "loop",
        "complete",
        "--request-id",
        claimed["id"],
        "--lease-id",
        claimed["lease_id"],
        "--summary",
        "done",
        "--json",
        cwd=repo,
    )
    assert blocked_complete.returncode == 1
    assert "reviewer verdict pass required" in json.loads(blocked_complete.stdout)["error"]

    verdict = run_ai(
        "loop",
        "verdict",
        "--request-id",
        claimed["id"],
        "--lease-id",
        claimed["lease_id"],
        "--reviewer",
        "checker-1",
        "--verdict",
        "pass",
        "--summary",
        "rubric passed",
        "--rubric-result",
        "tests pass; reviewer approves",
        "--json",
        cwd=repo,
    )
    assert verdict.returncode == 0, verdict.stdout + verdict.stderr
    verdict_payload = json.loads(verdict.stdout)["request"]["reviewer_verdict"]
    assert verdict_payload["verdict"] == "pass"
    assert verdict_payload["reviewer"] == "checker-1"

    complete = run_ai(
        "loop",
        "complete",
        "--request-id",
        claimed["id"],
        "--lease-id",
        claimed["lease_id"],
        "--summary",
        "done",
        "--result-file",
        "loop-result.md",
        "--json",
        cwd=repo,
    )
    assert complete.returncode == 0, complete.stdout + complete.stderr
    completed = json.loads(complete.stdout)["request"]
    assert completed["status"] == "done"
    assert completed["result"] == "verified locally"
    final_status = json.loads(run_ai("loop", "status", "--json", cwd=repo).stdout)
    assert final_status["pending"] == 0
    assert final_status["processing"] == 0
    assert final_status["done"] == 1

    distill = run_ai(
        "loop",
        "distill",
        "--request-id",
        claimed["id"],
        "--text",
        "Loop tasks must record reviewer pass verdict before complete.",
        "--tag",
        "rubric",
        "--json",
        cwd=repo,
    )
    assert distill.returncode == 0, distill.stdout + distill.stderr
    decision = json.loads(distill.stdout)["decision"]
    assert decision["source"] == "loop.distill"
    # outcome tag ("done") distinguishes a verified-success distill from a failure post-mortem
    assert decision["tags"] == ["loop", "distill", "done", "rubric"]
    decisions = (repo / ".ai" / "memory" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "reviewer pass verdict" in decisions


def test_loop_complete_phase_guard_reports_missing_reviewer_verdict(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    submitted = run_ai(
        "loop",
        "submit",
        "--text",
        "task",
        "--goal",
        "g",
        "--rubric",
        "review behavior and tests",
        "--checklist",
        "tests pass",
        "--json",
        cwd=repo,
    )
    assert submitted.returncode == 0, submitted.stdout + submitted.stderr
    claimed = json.loads(run_ai("loop", "claim", "--orchestrator-id", "o", "--json", cwd=repo).stdout)["request"]

    status_payload = json.loads(run_ai("loop", "status", "--json", cwd=repo).stdout)
    assert status_payload["expected_phases"] == {"review": 1}
    assert status_payload["phase_issue_count"] == 1
    issue_entry = status_payload["phase_issues"][0]
    assert issue_entry["out_of_plan"] is False
    assert issue_entry["expected_phase"] == "review"
    assert [issue["code"] for issue in issue_entry["phase_issues"]] == ["missing_reviewer_verdict"]

    blocked = run_ai(
        "loop",
        "complete",
        "--request-id",
        claimed["id"],
        "--lease-id",
        claimed["lease_id"],
        "--summary",
        "done",
        "--json",
        cwd=repo,
    )
    assert blocked.returncode == 1
    payload = json.loads(blocked.stdout)
    assert "reviewer verdict pass required" in payload["error"]
    assert payload["expected_phase"] == "review"
    assert payload["out_of_plan"] is True
    assert payload["phase_issues"][0]["code"] == "missing_reviewer_verdict"
    assert "pass reviewer verdict" in payload["recovery_hint"]


def test_loop_status_reports_failed_and_blocked_verdict_phases(tmp_path: Path) -> None:
    for verdict, expected_phase, issue_code in (
        ("fail", "fix", "reviewer_verdict_failed"),
        ("blocked", "unblock", "reviewer_verdict_blocked"),
    ):
        case_root = tmp_path / verdict
        case_root.mkdir()
        repo = copy_repo(case_root)
        submitted = run_ai(
            "loop",
            "submit",
            "--text",
            "task",
            "--goal",
            "g",
            "--rubric",
            "review behavior and tests",
            "--checklist",
            "tests pass",
            "--json",
            cwd=repo,
        )
        assert submitted.returncode == 0, submitted.stdout + submitted.stderr
        claimed = json.loads(run_ai("loop", "claim", "--orchestrator-id", "o", "--json", cwd=repo).stdout)["request"]
        verdict_result = run_ai(
            "loop",
            "verdict",
            "--request-id",
            claimed["id"],
            "--lease-id",
            claimed["lease_id"],
            "--verdict",
            verdict,
            "--summary",
            f"review {verdict}",
            "--json",
            cwd=repo,
        )
        assert verdict_result.returncode == 0, verdict_result.stdout + verdict_result.stderr

        status_payload = json.loads(run_ai("loop", "status", "--json", cwd=repo).stdout)
        assert status_payload["expected_phases"] == {expected_phase: 1}
        assert status_payload["out_of_plan"] is True
        assert status_payload["phase_issue_count"] == 1
        issue_entry = status_payload["phase_issues"][0]
        assert issue_entry["expected_phase"] == expected_phase
        assert issue_entry["phase_issues"][0]["code"] == issue_code

        blocked = run_ai(
            "loop",
            "complete",
            "--request-id",
            claimed["id"],
            "--lease-id",
            claimed["lease_id"],
            "--summary",
            "done",
            "--json",
            cwd=repo,
        )
        assert blocked.returncode == 1
        payload = json.loads(blocked.stdout)
        assert payload["expected_phase"] == expected_phase
        assert payload["out_of_plan"] is True
        assert payload["phase_issues"][0]["code"] == issue_code


def test_loop_status_reports_missing_required_review_contract(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    submitted = run_ai("loop", "submit", "--text", "task", "--goal", "g", "--json", cwd=repo)
    assert submitted.returncode == 0, submitted.stdout + submitted.stderr
    request = json.loads(submitted.stdout)["request"]
    assert request["expected_phase"] == "claim"
    assert request["out_of_plan"] is True
    assert [issue["code"] for issue in request["phase_issues"]] == ["missing_rubric", "missing_checklist"]

    status_payload = json.loads(run_ai("loop", "status", "--json", cwd=repo).stdout)
    assert status_payload["expected_phases"] == {"claim": 1}
    assert status_payload["out_of_plan"] is True
    assert status_payload["phase_issue_count"] == 2
    issue_entry = status_payload["phase_issues"][0]
    assert issue_entry["expected_phase"] == "claim"
    assert [issue["code"] for issue in issue_entry["phase_issues"]] == ["missing_rubric", "missing_checklist"]


def test_loop_recovers_expired_processing_request(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    submit = run_ai("loop", "submit", "--text", "Fix stale loop", "--goal", "Recover loop", "--json", cwd=repo)
    assert submit.returncode == 0, submit.stdout + submit.stderr
    claim = run_ai("loop", "claim", "--orchestrator-id", "worker-a", "--lease-seconds", "60", "--json", cwd=repo)
    assert claim.returncode == 0, claim.stdout + claim.stderr
    request = json.loads(claim.stdout)["request"]
    processing = repo / ".ai" / "memory" / "loop" / "processing" / f"{request['id']}.json"
    payload = json.loads(processing.read_text(encoding="utf-8"))
    payload["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    status = run_ai("loop", "status", "--json", cwd=repo)
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)["expired_processing"] == 1

    recover = run_ai("loop", "recover-expired", "--json", cwd=repo)
    assert recover.returncode == 0, recover.stdout + recover.stderr
    assert json.loads(recover.stdout)["recovered"] == 1
    assert not processing.exists()
    assert (repo / ".ai" / "memory" / "loop" / "inbox" / f"{request['id']}.json").exists()


def test_loop_submit_rejects_path_escape(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    result = run_ai("loop", "submit", "--file", str(outside), "--goal", "bad", "--json", cwd=repo)
    assert result.returncode == 1
    assert "inside the repository root" in json.loads(result.stdout)["error"]

    rubric_result = run_ai("loop", "submit", "--text", "task", "--rubric-file", str(outside), "--goal", "bad", "--json", cwd=repo)
    assert rubric_result.returncode == 1
    assert "inside the repository root" in json.loads(rubric_result.stdout)["error"]


def test_loop_complete_rejects_wrong_lease_id(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    run_ai("loop", "submit", "--text", "task", "--goal", "g", "--json", cwd=repo)
    claimed = json.loads(run_ai("loop", "claim", "--orchestrator-id", "o", "--json", cwd=repo).stdout)["request"]
    bad = run_ai("loop", "complete", "--request-id", claimed["id"], "--lease-id", "deadbeef",
                 "--summary", "x", "--json", cwd=repo)
    assert bad.returncode == 1
    assert "lease_id mismatch" in json.loads(bad.stdout)["error"]


def test_loop_verdict_rejects_wrong_lease_id(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    run_ai("loop", "submit", "--text", "task", "--goal", "g", "--json", cwd=repo)
    claimed = json.loads(run_ai("loop", "claim", "--orchestrator-id", "o", "--json", cwd=repo).stdout)["request"]
    bad = run_ai(
        "loop",
        "verdict",
        "--request-id",
        claimed["id"],
        "--lease-id",
        "deadbeef",
        "--verdict",
        "pass",
        "--summary",
        "x",
        "--json",
        cwd=repo,
    )
    assert bad.returncode == 1
    assert "lease_id mismatch" in json.loads(bad.stdout)["error"]


def test_loop_complete_rejects_path_traversal_request_id(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    bad = run_ai("loop", "complete", "--request-id", "../inbox/evil", "--lease-id", "x",
                 "--summary", "x", "--json", cwd=repo)
    assert bad.returncode == 1
    assert "invalid request_id" in json.loads(bad.stdout)["error"]


def test_loop_second_claim_returns_none_when_inbox_empty(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    run_ai("loop", "submit", "--text", "task", "--goal", "g", "--json", cwd=repo)
    first = json.loads(run_ai("loop", "claim", "--orchestrator-id", "a", "--json", cwd=repo).stdout)
    assert first["request"] is not None
    second = json.loads(run_ai("loop", "claim", "--orchestrator-id", "b", "--json", cwd=repo).stdout)
    assert second["request"] is None


def test_loop_dead_letters_after_max_attempts(tmp_path: Path) -> None:
    from ai_core.loop_engineering import MAX_ATTEMPTS

    repo = copy_repo(tmp_path)
    run_ai("loop", "submit", "--text", "flaky", "--goal", "g", "--json", cwd=repo)
    claimed = json.loads(run_ai("loop", "claim", "--orchestrator-id", "a", "--json", cwd=repo).stdout)["request"]
    processing = repo / ".ai" / "memory" / "loop" / "processing" / f"{claimed['id']}.json"
    payload = json.loads(processing.read_text(encoding="utf-8"))
    payload["attempts"] = MAX_ATTEMPTS
    payload["lease_expires_at"] = "2000-01-01T00:00:00Z"
    processing.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    recover = json.loads(run_ai("loop", "recover-expired", "--json", cwd=repo).stdout)
    assert recover["recovered"] == 0
    assert (repo / ".ai" / "memory" / "loop" / "dead" / f"{claimed['id']}.json").exists()
    assert not processing.exists()
    final = json.loads(run_ai("loop", "status", "--json", cwd=repo).stdout)
    assert final["dead"] == 1
    assert final["pending"] == 0


def test_loop_distill_allows_failed_task_postmortem(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    run_ai("loop", "submit", "--text", "task", "--goal", "g", "--json", cwd=repo)
    claimed = json.loads(run_ai("loop", "claim", "--orchestrator-id", "o", "--json", cwd=repo).stdout)["request"]
    failed = run_ai("loop", "fail", "--request-id", claimed["id"], "--lease-id", claimed["lease_id"],
                    "--reason", "blocked by X", "--json", cwd=repo)
    assert failed.returncode == 0, failed.stdout + failed.stderr
    # A failed (dead-lettered) task can still be distilled as a post-mortem — no pass verdict required.
    distilled = run_ai("loop", "distill", "--request-id", claimed["id"], "--text", "lesson: avoid X",
                       "--tag", "postmortem", "--json", cwd=repo)
    assert distilled.returncode == 0, distilled.stdout + distilled.stderr
    tags = json.loads(distilled.stdout)["decision"]["tags"]
    assert "dead" in tags and "postmortem" in tags


def _fail_one_loop_task(repo: Path) -> dict:
    run_ai("loop", "submit", "--text", "t", "--goal", "g", "--json", cwd=repo)
    claimed = json.loads(run_ai("loop", "claim", "--orchestrator-id", "o", "--json", cwd=repo).stdout)["request"]
    run_ai("loop", "fail", "--request-id", claimed["id"], "--lease-id", claimed["lease_id"],
           "--reason", "x", "--json", cwd=repo)
    return claimed


def test_loop_distill_blocks_topic_conflict_until_forced(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    run_ai("memory", "decision", "add", "--text",
           "Batch every Postgres write through the queue worker for throughput", cwd=repo)
    claimed = _fail_one_loop_task(repo)
    lesson = "Batch every Postgres write through the queue worker for throughput consistency"

    blocked = run_ai("loop", "distill", "--request-id", claimed["id"], "--text", lesson, "--json", cwd=repo)
    payload = json.loads(blocked.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "potential_contradiction"
    assert payload["conflicts"] and payload["conflicts"][0]["overlap"] >= 0.45

    forced = run_ai("loop", "distill", "--request-id", claimed["id"], "--text", lesson, "--force", "--json", cwd=repo)
    assert json.loads(forced.stdout)["ok"] is True


def test_loop_distill_allows_unrelated_lesson(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    run_ai("memory", "decision", "add", "--text",
           "Batch every Postgres write through the queue worker for throughput", cwd=repo)
    claimed = _fail_one_loop_task(repo)
    ok = run_ai("loop", "distill", "--request-id", claimed["id"], "--text",
                "Frontend overlay components belong under extension_chrome source", "--json", cwd=repo)
    assert json.loads(ok.stdout)["ok"] is True


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
        ("loop", "submit", "--text", "ci"),
        ("loop", "claim", "--orchestrator-id", "ci"),
        ("loop", "verdict", "--request-id", "loop-1-abcd", "--lease-id", "x", "--verdict", "pass", "--summary", "ci"),
        ("loop", "distill", "--request-id", "loop-1-abcd", "--text", "ci"),
        ("loop", "recover-expired"),
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
        ("loop", "status", "--json"),
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


def test_append_audit_chains_prev_sha(tmp_path: Path) -> None:
    from ai_core.memory import append_audit

    repo = copy_repo(tmp_path)
    records = [
        append_audit(repo, action="test.first", category="test", payload={"index": 1}),
        append_audit(repo, action="test.second", category="test", payload={"index": 2}),
        append_audit(repo, action="test.third", category="test", payload={"index": 3}),
    ]
    audit_path = next((repo / ".ai" / "memory" / "audit").glob("*.jsonl"))
    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3
    assert records[0]["prev_sha"] is None
    assert records[1]["prev_sha"] == hashlib.sha256(lines[0].encode("utf-8")).hexdigest()
    assert records[2]["prev_sha"] == hashlib.sha256(lines[1].encode("utf-8")).hexdigest()
    assert set(records[0]) == {"ts", "monotonic_ns", "action", "category", "payload", "prev_sha"}


def test_check_audit_chain_detects_middle_tampering(tmp_path: Path) -> None:
    from ai_core.doctor import check_audit_chain
    from ai_core.memory import append_audit

    repo = copy_repo(tmp_path)
    append_audit(repo, action="test.first", category="test", payload={"value": "one"})
    append_audit(repo, action="test.second", category="test", payload={"value": "two"})
    append_audit(repo, action="test.third", category="test", payload={"value": "three"})
    audit_path = next((repo / ".ai" / "memory" / "audit").glob("*.jsonl"))
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"two"', '"tampered"', 1)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = check_audit_chain(repo)
    assert result.ok is False
    assert "line 3" in result.detail
    assert "prev_sha_mismatch" in result.detail


def test_check_audit_chain_passes_with_legacy_prefix(tmp_path: Path) -> None:
    from ai_core.doctor import check_audit_chain
    from ai_core.memory import append_audit

    repo = copy_repo(tmp_path)
    audit_dir = repo / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "2026.jsonl"
    legacy_lines = [
        json.dumps({"ts": "2026-01-01T00:00:00Z", "action": "legacy.one", "category": "test", "payload": {}}, sort_keys=True),
        json.dumps({"ts": "2026-01-01T00:00:01Z", "action": "legacy.two", "category": "test", "payload": {}}, sort_keys=True),
    ]
    audit_path.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")

    first = append_audit(repo, action="test.first", category="test", payload={"value": "one"})
    assert first["prev_sha"] == hashlib.sha256(legacy_lines[-1].encode("utf-8")).hexdigest()
    assert check_audit_chain(repo).ok is True

    append_audit(repo, action="test.second", category="test", payload={"value": "two"})
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("legacy.one", "legacy.changed", 1)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert check_audit_chain(repo).ok is True

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"one"', '"tampered"', 1)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = check_audit_chain(repo)
    assert result.ok is False
    assert "prev_sha_mismatch" in result.detail


def test_append_audit_concurrent_safe(tmp_path: Path) -> None:
    from ai_core.doctor import check_audit_chain
    from ai_core.memory import append_audit

    repo = copy_repo(tmp_path)

    def write_one(index: int) -> None:
        append_audit(repo, action="test.concurrent", category="test", payload={"index": index})

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(write_one, range(50)))

    audit_path = next((repo / ".ai" / "memory" / "audit").glob("*.jsonl"))
    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 50
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert check_audit_chain(repo).ok is True


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


def test_audit_rebuild_index_repairs_missing_entries(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    first = run_ai_input("audit", "append", "--action", "test.first", "--json", stdin="{}", cwd=repo)
    second = run_ai_input("audit", "append", "--action", "test.second", "--json", stdin="{}", cwd=repo)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    (repo / ".ai" / "memory" / "audit-index.jsonl").write_text("", encoding="utf-8")

    broken = run_ai("doctor", "--strict", "--json", cwd=repo)
    repair = run_ai("audit", "rebuild-index", "--json", cwd=repo)
    fixed = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert broken.returncode == 10
    assert repair.returncode == 0, repair.stdout + repair.stderr
    assert json.loads(repair.stdout)["indexed"] == 2
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr


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
    assert set(bundle["metrics"]) == {"cache", "ok", "queue", "runtime_version", "search", "usage"}


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


def test_package_archive_is_reproducible_and_normalized(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    env_a = os.environ.copy()
    env_a["DIST_OVERRIDE"] = str(out_a)
    env_b = os.environ.copy()
    env_b["DIST_OVERRIDE"] = str(out_b)
    first = subprocess.run(["bash", "scripts/package.sh"], cwd=repo, env=env_a, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    second = subprocess.run(["bash", "scripts/package.sh"], cwd=repo, env=env_b, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    archive_a = Path(first.stdout.splitlines()[0])
    archive_b = Path(second.stdout.splitlines()[0])
    assert hashlib.sha256(archive_a.read_bytes()).hexdigest() == hashlib.sha256(archive_b.read_bytes()).hexdigest()
    commit_time = int(subprocess.check_output(["git", "log", "-1", "--format=%ct"], cwd=repo, text=True).strip())
    with tarfile.open(archive_a, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        assert names == sorted(names)
        for member in members:
            assert member.uid == 0
            assert member.gid == 0
            assert member.uname == ""
            assert member.gname == ""
            assert member.mtime == commit_time
            assert "mtime" not in member.pax_headers
            assert "atime" not in member.pax_headers
            assert "ctime" not in member.pax_headers


def test_reproducibility_check_script_detects_drift(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    package = subprocess.run(["bash", "scripts/package.sh"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert package.returncode == 0, package.stdout + package.stderr
    archive = Path(package.stdout.splitlines()[0])

    ok = subprocess.run(["bash", "scripts/reproducibility-check.sh", str(archive)], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "reproducibility check ok" in ok.stdout

    archive.write_bytes(archive.read_bytes() + b"drift")
    drift = subprocess.run(["bash", "scripts/reproducibility-check.sh", str(archive)], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert drift.returncode == 1
    assert "reproducibility drift" in drift.stderr
    assert "primary=" in drift.stderr
    assert "rebuild=" in drift.stderr

    missing = subprocess.run(["bash", "scripts/reproducibility-check.sh", str(repo / "dist" / "missing.tar.gz")], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert missing.returncode == 2
    assert "no primary archive" in missing.stderr


def test_reproducibility_release_gate_integration_invariants() -> None:
    package_script = (ROOT / "scripts" / "package.sh").read_text(encoding="utf-8")
    repro_script = (ROOT / "scripts" / "reproducibility-check.sh").read_text(encoding="utf-8")
    release_gate = (ROOT / "scripts" / "release-gate.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    docs_check = (ROOT / "scripts" / "docs-check.sh").read_text(encoding="utf-8")
    assert "gzip.GzipFile" in package_script
    assert "mtime=0" in package_script
    assert "tarfile.PAX_FORMAT" in package_script
    assert "uid = 0" in package_script
    assert "gid = 0" in package_script
    assert "DIST_OVERRIDE" in package_script
    assert "reproducibility drift" in repro_script
    assert "./scripts/reproducibility-check.sh \"$ARCHIVE\" >/dev/null" in release_gate
    assert "reproducibility-check:" in makefile
    assert "make -n reproducibility-check" in docs_check


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


def test_obs_search_returns_13_when_index_stale(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    needle = "Unique" + "ObsSearchStaleNeedle"
    target_file = repo / "stale-target.md"
    target_file.write_text(f"{needle} initial body\n", encoding="utf-8")
    subprocess.run(["git", "add", "stale-target.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add stale target"], cwd=repo, check=True)

    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr

    target_file.write_text(f"{needle} edited body\n", encoding="utf-8")

    # Disable auto-refresh so the index stays stale and obs reports it (exit 13); the
    # auto-refresh-on path is covered by test_obs_search_refresh_stale_rebuilds_and_exits_zero.
    stale_result = run_ai("obs", "search", "--query", needle, "--json", cwd=repo, env={"AI_SEARCH_AUTO_REFRESH": "0"})
    assert stale_result.returncode == 13, (stale_result.returncode, stale_result.stdout, stale_result.stderr)
    payload = json.loads(stale_result.stdout)
    query_block = payload["query"]
    assert query_block["stale_results"], query_block
    remediation = query_block["remediation"]
    assert remediation["command"] == "ai index rebuild --json"
    assert remediation["exit_code"] == 13
    assert remediation["stale_count"] >= 1


def test_obs_search_refresh_stale_rebuilds_and_exits_zero(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    needle = "Unique" + "ObsSearchRefreshNeedle"
    target_file = repo / "refresh-target.md"
    target_file.write_text(f"{needle} initial body\n", encoding="utf-8")
    subprocess.run(["git", "add", "refresh-target.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add refresh target"], cwd=repo, check=True)

    run_ai("index", "rebuild", "--json", cwd=repo)
    target_file.write_text(f"{needle} edited body\n", encoding="utf-8")

    refresh_result = run_ai(
        "obs", "search", "--refresh-stale", "--query", needle, "--json", cwd=repo
    )
    assert refresh_result.returncode == 0, refresh_result.stdout + refresh_result.stderr
    payload = json.loads(refresh_result.stdout)
    assert payload["query"].get("stale_results", []) == []


def test_secret_scan_allowlist_acknowledges_known_paths(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    sample = repo / "docs" / "fixture-sample.md"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("token=" + "z" * 32 + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/fixture-sample.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add fixture sample"], cwd=repo, check=True)

    result_no_allowlist = run_ai("doctor", "--strict", "--json", cwd=repo)
    payload_no = json.loads(result_no_allowlist.stdout)
    secret_no = next(c for c in payload_no["checks"] if c["name"] == "secret_scan")
    assert secret_no["ok"] is False
    assert "docs/fixture-sample.md" in secret_no["detail"]

    allowlist = repo / ".ai" / "secret_scan_allowlist.txt"
    allowlist.write_text(
        allowlist.read_text(encoding="utf-8") + "docs/fixture-sample.md\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".ai/secret_scan_allowlist.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "acknowledge fixture"], cwd=repo, check=True)

    result_with_allowlist = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result_with_allowlist.returncode == 0, (
        result_with_allowlist.returncode,
        result_with_allowlist.stdout,
        result_with_allowlist.stderr,
    )
    payload_yes = json.loads(result_with_allowlist.stdout)
    secret_yes = next(c for c in payload_yes["checks"] if c["name"] == "secret_scan")
    assert secret_yes["ok"] is True
    assert "flagged=0" in secret_yes["detail"]
    assert "acknowledged=" in secret_yes["detail"]


def test_secret_scan_skips_known_noisy_paths(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    noisy = [
        ("public/source-maps/bundle.js.map", "token=" + "a" * 32),
        ("firebase_options.dart", "apiKey: 'AIza" + "b" * 32 + "'"),
        ("frontend/dist/assets/bundle.min.js", "secret='" + "c" * 32 + "'"),
        ("yarn.lock", "token \"" + "d" * 32 + "\""),
    ]
    for rel, content in noisy:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add noisy targets"], cwd=repo, check=True)

    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    secret_check = next(c for c in payload["checks"] if c["name"] == "secret_scan")
    assert secret_check["ok"] is True
    assert "firebase_options.dart" not in secret_check["detail"]
    assert ".js.map" not in secret_check["detail"]


def test_no_token_estimates_check_detects_forbidden_keyword(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    obs_path = repo / ".ai" / "runtime" / "src" / "ai_core" / "obs.py"
    text = obs_path.read_text(encoding="utf-8")
    obs_path.write_text(text + "\n# tokens_saved = 0\n", encoding="utf-8")
    subprocess.run(["git", "add", ".ai/runtime/src/ai_core/obs.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "leak forbidden keyword"], cwd=repo, check=True)

    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode != 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    check = next(c for c in payload["checks"] if c["name"] == "no_token_estimates")
    assert check["ok"] is False
    assert "obs.py:tokens_saved" in check["detail"]


def test_no_token_estimates_passes_clean_repo(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    check = next(c for c in payload["checks"] if c["name"] == "no_token_estimates")
    assert check["ok"] is True


def test_mcp_methods_registered_invariant(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr
    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    check = next(c for c in payload["checks"] if c["name"] == "mcp_methods_registered")
    assert check["ok"] is True
    assert "mcp_methods=" in check["detail"]
    assert "claude_commands=5" in check["detail"]
    assert "codex_prompts=5" in check["detail"]


def test_mcp_methods_registered_fails_when_command_file_missing(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    (repo / ".claude" / "commands" / "cb-usage.md").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "remove cb-usage"], cwd=repo, check=True)
    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode != 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    check = next(c for c in payload["checks"] if c["name"] == "mcp_methods_registered")
    assert check["ok"] is False
    assert "cb-usage.md" in check["detail"]


def test_mcp_methods_registered_fails_without_mcp_json(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    (repo / ".mcp.json").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "remove mcp.json"], cwd=repo, check=True)
    result = run_ai("doctor", "--strict", "--json", cwd=repo)
    assert result.returncode != 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    check = next(c for c in payload["checks"] if c["name"] == "mcp_methods_registered")
    assert check["ok"] is False
    assert ".mcp.json" in check["detail"]


def test_mcp_server_obs_usage_method(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert rebuild_result.returncode == 0
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "obs_usage", "params": {}})
    result = run_ai("mcp", "--once-json", request, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == 1
    assert "result" in payload
    assert payload["result"]["actual_token_usage"]["claude"]["source"]


def test_memory_decision_add_appends_jsonl(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    result = run_ai("memory", "decision", "add", "--text", "Adopt sandbox-by-default", "--tag", "policy", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    rec = payload["record"]
    assert rec["decision"] == "Adopt sandbox-by-default"
    assert rec["tags"] == ["policy"]
    assert rec["id"].startswith("dec-")
    log = (repo / ".ai" / "memory" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(log[-1])
    assert last["decision"] == "Adopt sandbox-by-default"


def test_memory_todo_add_then_close(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    add_result = run_ai("memory", "todo", "add", "--title", "Wire cross-session resume injection", "--owner", "claude", "--json", cwd=repo)
    assert add_result.returncode == 0, add_result.stdout + add_result.stderr
    payload = json.loads(add_result.stdout)
    assert payload["ok"] is True
    todo_id = payload["record"]["id"]
    close_result = run_ai("memory", "todo", "close", "--match", "cross-session resume", "--reason", "done in R93", "--json", cwd=repo)
    assert close_result.returncode == 0, close_result.stdout + close_result.stderr
    closed = json.loads(close_result.stdout)
    assert closed["ok"] is True
    assert closed["record"]["id"] == todo_id
    assert closed["record"]["status"] == "done"
    log = (repo / ".ai" / "memory" / "todos.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(log[-1])
    assert last["status"] == "done"
    assert last["close_reason"] == "done in R93"


def test_memory_todo_close_uses_latest_status_per_id(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    todo_path = repo / ".ai" / "memory" / "todos.jsonl"
    todo_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "todo-stale", "title": "stale task", "status": "open"}),
                json.dumps({"id": "todo-stale", "title": "stale task", "status": "done"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = run_ai("memory", "todo", "close", "--match", "stale task", "--json", cwd=repo)
    assert result.returncode != 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "no_match"


def test_memory_session_append_writes_markdown(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    result = run_ai("memory", "session", "append", "--text", "Round 93 memory layer wired", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    text = (repo / ".ai" / "memory" / "session-current.md").read_text(encoding="utf-8")
    assert "Round 93 memory layer wired" in text
    assert text.strip().endswith("Round 93 memory layer wired")


def test_memory_close_todo_no_match_returns_error(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    run_ai("memory", "todo", "add", "--title", "alpha", "--json", cwd=repo)
    result = run_ai("memory", "todo", "close", "--match", "no-such-string-xyz", "--json", cwd=repo)
    assert result.returncode != 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "no_match"


def test_mcp_record_decision_via_tools_call(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    request = {
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "record_decision", "arguments": {"text": "Use sandbox for grep", "tags": ["routing"]}},
    }
    result = run_ai("mcp", "--once-json", json.dumps(request), cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    assert "result" in response
    structured = response["result"].get("structuredContent")
    assert structured is not None
    assert structured["ok"] is True
    assert "Use sandbox for grep" in structured["record"]["decision"]


def test_iter_text_files_skips_memory_and_cache(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))
    from ai_core.search import iter_text_files

    repo = tmp_path / "repo"
    (repo / ".ai" / "memory" / "audit").mkdir(parents=True)
    (repo / ".ai" / "memory" / "events").mkdir(parents=True)
    (repo / ".ai" / "memory" / "sessions" / "s1").mkdir(parents=True)
    (repo / ".ai" / "cache").mkdir(parents=True)
    (repo / "src").mkdir()
    # Files that MUST be skipped (operational state)
    (repo / ".ai" / "memory" / "audit" / "2026.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (repo / ".ai" / "memory" / "audit-index.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (repo / ".ai" / "memory" / "events" / "events.jsonl").write_text('{"hook":"x"}\n', encoding="utf-8")
    (repo / ".ai" / "memory" / "sessions" / "s1" / "resume.json").write_text('{"x":1}\n', encoding="utf-8")
    (repo / ".ai" / "memory" / "decisions.jsonl").write_text('{"d":"x"}\n', encoding="utf-8")
    (repo / ".ai" / "memory" / "session-current.md").write_text('# session\n', encoding="utf-8")
    (repo / ".ai" / "cache" / "code.sqlite").write_bytes(b"sqlite-stub")
    # Files that MUST be indexed
    (repo / "README.md").write_text("# project\n", encoding="utf-8")
    (repo / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    yielded = sorted(p.relative_to(repo).as_posix() for p in iter_text_files(repo))
    # Must contain README.md and src/main.py only.
    assert "README.md" in yielded
    assert "src/main.py" in yielded
    forbidden_prefixes = (".ai/memory/", ".ai/cache/")
    for path in yielded:
        for prefix in forbidden_prefixes:
            assert not path.startswith(prefix), f"{path} should be skipped (matches {prefix})"


def test_session_start_with_rebuild_always_doctor_passes(tmp_path: Path) -> None:
    """End-to-end: session start --rebuild always followed by strict doctor must pass.

    Regression test for the case where audit/events writes after rebuild caused
    index_freshness to fail despite rebuild_mode=always.
    """
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    session_result = run_ai("session", "start", "--agent", "operator", "--rebuild", "always", "--json", cwd=repo)
    assert session_result.returncode == 0, session_result.stdout + session_result.stderr
    session_payload = json.loads(session_result.stdout)
    assert session_payload["index"]["rebuilt"] is True
    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    payload = json.loads(doctor_result.stdout)
    fails = [c["name"] for c in payload["checks"] if not c.get("ok")]
    assert "index_freshness" not in fails, f"index_freshness still failing: {fails}"


def test_session_start_auto_rebuilds_hash_drift_when_mtime_is_older(tmp_path: Path) -> None:
    """Auto rebuild must not rely only on source mtime.

    Git checkout, external tools, or filesystem timestamp behavior can leave a
    changed file with an mtime older than the SQLite index. The doctor catches
    that by hash; session-start auto must catch it too.
    """
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    target = repo / "src" / "mtime-hidden-drift.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export const HiddenDriftNeedle = 'before';\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/mtime-hidden-drift.ts"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add hidden drift fixture"], cwd=repo, check=True)

    rebuild_result = run_ai("index", "rebuild", "--json", cwd=repo)
    assert rebuild_result.returncode == 0, rebuild_result.stdout + rebuild_result.stderr
    db = repo / ".ai" / "cache" / "code.sqlite"
    db_mtime = db.stat().st_mtime

    target.write_text("export const HiddenDriftNeedle = 'after';\n", encoding="utf-8")
    old_time = max(1, db_mtime - 60)
    os.utime(target, (old_time, old_time))

    session_result = run_ai("session", "start", "--agent", "operator", "--rebuild", "auto", "--json", cwd=repo)
    assert session_result.returncode == 0, session_result.stdout + session_result.stderr
    session_payload = json.loads(session_result.stdout)
    assert session_payload["index"]["before"]["reason"] == "hash_mismatch"
    assert session_payload["index"]["rebuilt"] is True
    assert session_payload["index"]["after"]["reason"] == "current"

    doctor_result = run_ai("doctor", "--strict", "--json", cwd=repo)
    payload = json.loads(doctor_result.stdout)
    fails = [c["name"] for c in payload["checks"] if not c.get("ok")]
    assert "index_freshness" not in fails, f"index_freshness still failing: {fails}"


def test_event_observability_breaks_down_hook_and_mcp(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))
    from ai_core.obs import event_observability

    events_dir = tmp_path / ".ai" / "memory" / "events"
    events_dir.mkdir(parents=True)
    log = events_dir / "20260508.jsonl"
    entries = [
        {"timestamp": "2026-05-08T00:00:01Z", "kind": "SessionStart", "payload": {"hook": "SessionStart", "additional_context_bytes": 800}},
        {"timestamp": "2026-05-08T00:00:02Z", "kind": "UserPromptSubmit", "payload": {"hook": "UserPromptSubmit", "additional_context_bytes": 250}},
        {"timestamp": "2026-05-08T00:00:03Z", "kind": "PreToolUse", "payload": {"hook": "PreToolUse", "additional_context_bytes": 180, "precall": {"action": "block", "binary": "grep"}, "decision": "block"}},
        {"timestamp": "2026-05-08T00:00:04Z", "kind": "PreToolUse", "payload": {"hook": "PreToolUse", "additional_context_bytes": 80, "precall": {"action": "allow", "reason": "hatch_detected"}}},
        {"timestamp": "2026-05-08T00:00:05Z", "kind": "PreToolUse", "payload": {"hook": "PreToolUse", "additional_context_bytes": 200, "precall": {"action": "block", "binary": "rg"}, "decision": "block"}},
        {"timestamp": "2026-05-08T00:00:06Z", "kind": "mcp.request", "payload": {"hook": "mcp.request", "method": "tools/call", "tool_name": "code_query", "request_bytes": 80, "response_bytes": 1500}},
        {"timestamp": "2026-05-08T00:00:07Z", "kind": "mcp.request", "payload": {"hook": "mcp.request", "method": "tools/call", "tool_name": "sandbox_execute", "request_bytes": 100, "response_bytes": 600}},
        {"timestamp": "2026-05-08T00:00:08Z", "kind": "mcp.request", "payload": {"hook": "mcp.request", "method": "code_query", "request_bytes": 80, "response_bytes": 1200}},
    ]
    # Add a sandbox.execute event — should NOT count as hook_events.
    entries.append(
        {"timestamp": "2026-05-08T00:00:09Z", "kind": "sandbox.execute", "payload": {"hook": "sandbox.execute", "exec_id": "x", "exit_code": 0, "total_bytes": 100}}
    )
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    result = event_observability(tmp_path)
    assert result["ok"] is True
    assert result["sandbox_executions"] == 1
    assert result["hook_events"] == 5  # SessionStart + UserPromptSubmit + 3 PreToolUse (sandbox excluded)
    assert result["mcp_requests"] == 3
    assert result["pretooluse_blocks"] == 2
    assert result["pretooluse_allows"] == 1

    hook_bd = result["hook_breakdown"]
    assert hook_bd["SessionStart"]["count"] == 1
    assert hook_bd["SessionStart"]["bytes_total"] == 800
    assert hook_bd["UserPromptSubmit"]["count"] == 1
    assert hook_bd["PreToolUse"]["count"] == 3
    assert hook_bd["PreToolUse"]["blocked"] == 2
    assert hook_bd["PreToolUse"]["allowed"] == 1
    assert hook_bd["PreToolUse"]["bytes_total"] == 460  # 180+80+200

    mcp_bd = result["mcp_breakdown"]
    assert mcp_bd["tools/call"]["count"] == 2
    assert mcp_bd["tools/call"]["response_bytes"] == 2100
    assert mcp_bd["code_query"]["count"] == 1
    assert mcp_bd["code_query"]["response_bytes"] == 1200
    mcp_tool_bd = result["mcp_tool_breakdown"]
    assert mcp_tool_bd["code_query"]["count"] == 1
    assert mcp_tool_bd["code_query"]["response_bytes"] == 1500
    assert mcp_tool_bd["sandbox_execute"]["count"] == 1
    assert mcp_tool_bd["sandbox_execute"]["response_bytes"] == 600


def test_event_observability_empty_when_no_events_dir(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))
    from ai_core.obs import event_observability

    result = event_observability(tmp_path)
    assert result["ok"] is True
    assert result["hook_events"] == 0
    assert result["mcp_requests"] == 0
    assert result["pretooluse_blocks"] == 0
    assert result["hook_breakdown"] == {}
    assert result["mcp_breakdown"] == {}


def test_pretooluse_block_event_persisted_with_precall_field(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    payload = json.dumps({"agent": "claude", "tool_name": "Bash", "tool_input": {"command": "grep -rn pattern src/"}})
    result = run_ai_input("hook", "PreToolUse", "--json", stdin=payload, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    assert response["decision"] == "block"
    events_dir = repo / ".ai" / "memory" / "events"
    files = sorted(events_dir.glob("*.jsonl"))
    assert files, "expected at least one event log file"
    last = files[-1].read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(last)
    pl = record["payload"]
    assert pl["hook"] == "PreToolUse"
    assert pl["precall"]["action"] == "block"
    assert pl["precall"]["binary"] == "grep"
    assert pl["decision"] == "block"


def test_codex_transcript_parser_aggregates_token_count_event(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))
    from ai_core.transcripts import codex_usage_summary, parse_codex_session

    home = tmp_path / "codex_home"
    sessions_dir = home / "sessions" / "2026" / "05" / "07"
    sessions_dir.mkdir(parents=True)
    target_cwd = tmp_path / "project"
    target_cwd.mkdir()
    session_path = sessions_dir / "rollout-2026-05-07T10-00-00-019df0a0-aaaa-1111-bbbb-222233334444.jsonl"
    lines = [
        {
            "timestamp": "2026-05-07T10:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": "019df0a0-aaaa-1111-bbbb-222233334444",
                "cwd": str(target_cwd),
                "originator": "Codex Desktop",
                "cli_version": "0.128.0-alpha.1",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-05-07T10:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t1"},
        },
        {
            "timestamp": "2026-05-07T10:00:02.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message"},
        },
        {
            "timestamp": "2026-05-07T10:00:05.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 200,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 1050,
                    },
                    "last_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 200,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 1050,
                    },
                },
            },
        },
        {
            "timestamp": "2026-05-07T10:00:10.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message"},
        },
        {
            "timestamp": "2026-05-07T10:00:15.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 5500,
                        "cached_input_tokens": 1200,
                        "output_tokens": 320,
                        "reasoning_output_tokens": 40,
                        "total_tokens": 5860,
                    },
                    "last_token_usage": {
                        "input_tokens": 4500,
                        "cached_input_tokens": 1000,
                        "output_tokens": 270,
                        "reasoning_output_tokens": 35,
                        "total_tokens": 4810,
                    },
                },
            },
        },
    ]
    session_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    parsed = parse_codex_session(session_path)
    assert parsed is not None
    assert parsed.cli_version == "0.128.0-alpha.1"
    assert parsed.tokens["input_tokens"] == 5500  # last total wins
    assert parsed.tokens["total_tokens"] == 5860
    assert parsed.user_messages == 1
    assert parsed.agent_messages == 1
    assert parsed.turns == 1

    summary = codex_usage_summary(target_cwd, home=home)
    assert summary["source"] == "codex_transcript"
    assert summary["sessions_scanned"] == 1
    assert summary["sessions_matched"] == 1
    assert summary["tokens"]["input_tokens"] == 5500
    assert summary["total_observed_tokens"] >= 5860


def test_codex_transcript_filters_by_cwd(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))
    from ai_core.transcripts import codex_usage_summary

    home = tmp_path / "codex_home"
    sd = home / "sessions" / "2026" / "05" / "07"
    sd.mkdir(parents=True)
    target = tmp_path / "target"; target.mkdir()
    other = tmp_path / "other"; other.mkdir()

    def make(cwd: Path, name: str) -> None:
        (sd / f"rollout-2026-05-07T11-00-00-{name}.jsonl").write_text(
            json.dumps({"timestamp": "2026-05-07T11:00:00Z", "type": "session_meta", "payload": {"id": name, "cwd": str(cwd)}}) + "\n"
            + json.dumps({"timestamp": "2026-05-07T11:00:01Z", "type": "event_msg",
                          "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 110}}}}) + "\n",
            encoding="utf-8",
        )

    make(target, "019df0a0-aaaa-1111-bbbb-aaaa11112222")
    make(other, "019df0a0-bbbb-2222-cccc-bbbb11112222")
    summary = codex_usage_summary(target, home=home)
    assert summary["sessions_scanned"] == 2
    assert summary["sessions_matched"] == 1
    assert summary["tokens"]["input_tokens"] == 100


def test_codex_transcript_unavailable_when_home_missing(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))
    from ai_core.transcripts import codex_usage_summary

    summary = codex_usage_summary(tmp_path, home=tmp_path / "no-codex-home")
    assert summary["source"] == "codex_transcript_unavailable"
    assert summary["sessions_matched"] == 0
    assert summary["total_observed_tokens"] == 0


def test_session_start_hook_injects_decisions_todos_session_tail(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    decisions = repo / ".ai" / "memory" / "decisions.jsonl"
    decisions.write_text(
        json.dumps({"decided_at": "2026-05-08T01:23:45Z", "decision": "Adopt MCP code_query as default search"}) + "\n"
        + json.dumps({"decided_at": "2026-05-08T02:00:00Z", "decision": "Cache contentless FTS5 v2"}) + "\n",
        encoding="utf-8",
    )
    todos = repo / ".ai" / "memory" / "todos.jsonl"
    todos.write_text(
        json.dumps({"title": "Wire SessionStart auto-inject", "status": "open", "owner": "claude"}) + "\n"
        + json.dumps({"title": "Already done", "status": "done"}) + "\n",
        encoding="utf-8",
    )
    session = repo / ".ai" / "memory" / "session-current.md"
    session.write_text(
        "# session 2026-05-08\n\n- ran cb-doctor\n- 인덱스 갱신 완료\n- 검색 라우팅 점검\n",
        encoding="utf-8",
    )
    payload = json.dumps({"agent": "claude", "dry": True})
    result = run_ai_input("hook", "SessionStart", "--json", stdin=payload, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    ctx = response["additionalContext"]
    assert "Search routing" in ctx
    assert "code_query" in ctx
    assert "code_read_hashline" in ctx
    assert "Adopt MCP code_query as default search" in ctx
    assert "Cache contentless FTS5 v2" in ctx
    assert "Wire SessionStart auto-inject" in ctx
    assert "Already done" not in ctx
    assert "검색 라우팅 점검" in ctx
    assert response["additional_context_bytes"] == len(ctx.encode("utf-8"))
    assert response["additional_context_bytes"] <= 12288
    assert response["elapsed_ms"] <= 200


def test_session_start_memory_tier_summary_is_cached(tmp_path: Path, monkeypatch) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    calls = {"classify": 0}

    import ai_core.memory_tier as memory_tier_mod

    def fake_classify(_root: Path) -> dict:
        calls["classify"] += 1
        return {
            "tiers": {
                "hot": {"audit_events": 1},
                "warm": {"audit_events": 2},
                "cold": {"audit_events": 3},
            }
        }

    monkeypatch.setattr(memory_tier_mod, "classify", fake_classify)
    monkeypatch.setattr(memory_tier_mod, "hot_pressure", lambda _root: {"session_md_ratio": 0.25})

    from ai_core.hooks import _memory_tier_summary_context

    first = _memory_tier_summary_context(repo)
    second = _memory_tier_summary_context(repo)

    assert "cb-mem: hot=1 warm=2 cold=3" in first
    assert "cb-mem: hot=1 warm=2 cold=3" in second
    assert calls["classify"] == 1


def test_session_start_codebase_map_is_cached(tmp_path: Path, monkeypatch) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    calls = {"map": 0}

    import ai_core.codebase_map as codebase_map_mod

    def fake_map(_root: Path, *, max_entries: int, include_untracked: bool) -> dict:
        calls["map"] += 1
        return {"additionalContext": f"Codebase map cached call {calls['map']}"}

    monkeypatch.setattr(codebase_map_mod, "build_codebase_map", fake_map)

    from ai_core.hooks import _codebase_map_summary_context

    first = _codebase_map_summary_context(repo)
    second = _codebase_map_summary_context(repo)

    assert "Codebase map cached call 1" in first
    assert "Codebase map cached call 1" in second
    assert "Codebase map cached call 2" not in second
    assert calls["map"] == 1


def test_session_start_hook_injects_prior_session_tail(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    snap_dir = repo / ".ai" / "memory" / "sessions" / "old-session"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "resume.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "old-session",
                "agent": "codex",
                "written_at": "2026-05-08T01:23:45Z",
                "decisions_tail": [],
                "todos_open": [],
                "session_tail": "\n".join(f"- prior milestone {i}" for i in range(12)),
            }
        ),
        encoding="utf-8",
    )

    result = run_ai_input(
        "hook",
        "SessionStart",
        "--json",
        stdin=json.dumps({"agent": "codex", "dry": True, "session_id": "new-session"}),
        cwd=repo,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ctx = json.loads(result.stdout)["additionalContext"]
    assert "Prior session resume" in ctx
    assert "session tail:" in ctx
    assert "prior milestone 11" in ctx
    assert "prior milestone 3" not in ctx


def test_session_start_hook_surfaces_skill_recommendations(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    decisions = repo / ".ai" / "memory" / "decisions.jsonl"
    rows = [
        {"decided_at": f"2026-05-08T00:00:0{i}Z", "decision": f"deploy decision {i}", "tags": ["deploy"]}
        for i in range(4)
    ]
    decisions.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    result = run_ai_input("hook", "SessionStart", "--json", stdin='{"agent":"claude"}', cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    ctx = response["additionalContext"]
    assert "Skill recommendations available" in ctx
    assert "ai recommend skills accept" in ctx
    assert "recall-deploy-decisions" in ctx
    catalog = repo / ".ai" / "skills" / "catalog.jsonl"
    assert catalog.exists()
    catalog_rows = [json.loads(line) for line in catalog.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(row.get("status") == "pending" for row in catalog_rows)


def test_session_start_hook_satisfaction_summary_includes_surfaced_count(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    decisions = repo / ".ai" / "memory" / "decisions.jsonl"
    rows = [
        {"decided_at": f"2026-05-08T00:00:0{i}Z", "decision": f"deploy decision {i}", "tags": ["deploy"]}
        for i in range(4)
    ]
    decisions.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    result = run_ai_input("hook", "SessionStart", "--json", stdin='{"agent":"claude"}', cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    ctx = response["additionalContext"]
    assert "Recommendation satisfaction:" in ctx
    assert "surfaced" in ctx


def test_session_start_hook_compact_mode_collapses_sections(tmp_path: Path, monkeypatch) -> None:
    """AI_RECOMMEND_COMPACT=1 must collapse skill section to a single line — opt-in only."""
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    decisions = repo / ".ai" / "memory" / "decisions.jsonl"
    rows = [
        {"decided_at": f"2026-05-08T00:00:0{i}Z", "decision": f"compact decision {i}", "tags": ["compact"]}
        for i in range(4)
    ]
    decisions.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_RECOMMEND_COMPACT", "1")
    result = run_ai_input("hook", "SessionStart", "--json", stdin='{"agent":"claude"}', cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    ctx = json.loads(result.stdout)["additionalContext"]
    assert "cb-skill" in ctx, "compact mode must use 'cb-skill' prefix line"
    assert "Skill recommendations available" not in ctx, "verbose header must be omitted in compact mode"


def test_session_start_hook_cooldown_suppresses_repeat_surfacing(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    decisions = repo / ".ai" / "memory" / "decisions.jsonl"
    rows = [
        {"decided_at": f"2026-05-08T00:00:0{i}Z", "decision": f"qa decision {i}", "tags": ["qa"]}
        for i in range(4)
    ]
    decisions.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    first = run_ai_input("hook", "SessionStart", "--json", stdin='{"agent":"claude"}', cwd=repo)
    assert first.returncode == 0
    assert "Skill recommendations available" in json.loads(first.stdout)["additionalContext"]
    second = run_ai_input("hook", "SessionStart", "--json", stdin='{"agent":"claude"}', cwd=repo)
    assert second.returncode == 0
    ctx2 = json.loads(second.stdout)["additionalContext"]
    assert "Skill recommendations available" not in ctx2, (
        "cooldown should suppress identical surfacing on the next SessionStart"
    )
    assert "Recommendation satisfaction:" in ctx2


def test_post_tool_use_hook_skips_injection(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    (repo / ".ai" / "memory" / "decisions.jsonl").write_text(
        json.dumps({"decision": "Should NOT appear in PostToolUse"}) + "\n", encoding="utf-8"
    )
    payload = json.dumps({"agent": "codex", "dry": True})
    result = run_ai_input("hook", "PostToolUse", "--json", stdin=payload, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    assert response["additional_context_bytes"] == 0
    assert "additionalContext" not in response
    assert "hookSpecificOutput" not in response


def test_user_prompt_submit_hook_includes_routing_when_memory_empty(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    payload = json.dumps({"agent": "claude", "dry": True})
    result = run_ai_input("hook", "UserPromptSubmit", "--json", stdin=payload, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    ctx = response["additionalContext"]
    assert "Search routing" in ctx
    assert "code_read_hashline" in ctx
    assert "Recent decisions" not in ctx


def test_user_prompt_submit_harness_request_injects_directive(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    payload = json.dumps({"agent": "claude", "dry": True, "prompt": "하네스 적용해서 95%까지 자율 개선해"})
    result = run_ai_input("hook", "UserPromptSubmit", "--json", stdin=payload, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    ctx = json.loads(result.stdout)["additionalContext"]
    assert "Explicit harness request detected" in ctx
    assert "Do not wait for a separate `ai harness` command" in ctx
    assert "target=95%" in ctx


def test_agents_md_documents_search_routing() -> None:
    text = (ROOT / ".ai" / "AGENTS.md").read_text(encoding="utf-8")
    assert "Search Routing" in text
    assert "code_query" in text
    assert "code_read_hashline" in text
    assert "grep" in text


def test_mcp_server_doctor_strict_method(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    init_package_repo(repo)
    run_ai("index", "rebuild", "--json", cwd=repo)
    request = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "doctor_strict", "params": {}})
    result = run_ai("mcp", "--once-json", request, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == 2
    assert "result" in payload
    assert payload["result"]["ok"] is True
    names = {c["name"] for c in payload["result"]["checks"]}
    assert "mcp_methods_registered" in names
