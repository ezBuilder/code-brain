from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.security_findings import list_records, record  # noqa: E402


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
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return repo


def test_security_finding_record_is_deterministic_and_redacted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    secret = "AKIA" + "A" * 16

    first = record(
        repo,
        affected_path="src/config.py",
        finding_type="secret_scan.aws_access_key",
        detail_summary=f"secret_scan matched {secret}",
        repro_command=f"ai doctor --strict # {secret}",
        verification_command="ai doctor --strict",
    )
    second = record(
        repo,
        affected_path="src/config.py",
        finding_type="secret_scan.aws_access_key",
        detail_summary=f"secret_scan matched {secret}",
        repro_command=f"ai doctor --strict # {secret}",
        verification_command="ai doctor --strict",
    )

    assert first["record"]["id"] == second["record"]["id"]
    assert first["record"]["status"] == "open"
    assert first["record"]["detail_summary"] == "secret_scan matched [REDACTED]"
    assert first["record"]["evidence_hash"]

    ledger_text = (repo / ".ai" / "memory" / "security-findings.jsonl").read_text(encoding="utf-8")
    assert secret not in ledger_text
    line = json.loads(ledger_text.splitlines()[0])
    assert line["affected_path"] == "src/config.py"
    assert line["repro_command"] == "ai doctor --strict # [REDACTED]"


def test_security_finding_cli_record_list_and_update(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    added = run_ai(
        repo,
        "security",
        "finding",
        "record",
        "--affected-path",
        "src/config.py",
        "--type",
        "secret_scan.generic_secret",
        "--detail-summary",
        "masked generic secret in config",
        "--repro-command",
        "ai doctor --strict",
        "--verification-command",
        "ai doctor --strict",
        "--json",
    )
    assert added.returncode == 0, added.stdout + added.stderr
    finding_id = json.loads(added.stdout)["record"]["id"]

    updated = run_ai(
        repo,
        "security",
        "finding",
        "update",
        "--id",
        finding_id,
        "--status",
        "verified_fixed",
        "--verification-command",
        "ai doctor --strict",
        "--json",
    )
    assert updated.returncode == 0, updated.stdout + updated.stderr

    listed = run_ai(repo, "security", "finding", "list", "--status", "verified_fixed", "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["count"] == 1
    assert payload["records"][0]["id"] == finding_id
    assert payload["records"][0]["status"] == "verified_fixed"

    latest = list_records(repo)
    assert latest["count"] == 1
    assert latest["records"][0]["status"] == "verified_fixed"


def test_security_finding_mcp_record_list_update(tmp_path: Path) -> None:
    from ai_core import mcp_server

    repo = make_repo(tmp_path)
    recorded = mcp_server.handle_request(
        repo,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "security_finding_record",
                "arguments": {
                    "affected_path": "src/config.py",
                    "finding_type": "secret_scan.generic_secret",
                    "detail_summary": "masked generic secret",
                    "repro_command": "ai doctor --strict",
                    "verification_command": "ai doctor --strict",
                },
            },
        },
    )
    assert recorded is not None
    assert recorded["result"]["isError"] is False
    finding_id = recorded["result"]["structuredContent"]["record"]["id"]

    updated = mcp_server.handle_request(
        repo,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "security_finding_update",
                "arguments": {
                    "id": finding_id,
                    "status": "false_positive",
                    "verification_command": "ai doctor --strict",
                },
            },
        },
    )
    assert updated is not None
    assert updated["result"]["isError"] is False

    listed = mcp_server.handle_request(
        repo,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "security_finding_list", "arguments": {"status": "false_positive"}},
        },
    )
    assert listed is not None
    assert listed["result"]["structuredContent"]["count"] == 1
    assert listed["result"]["structuredContent"]["records"][0]["id"] == finding_id
