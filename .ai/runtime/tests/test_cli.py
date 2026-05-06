from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable


def run_ai(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    if env:
        merged.update(env)
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=ROOT,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


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
