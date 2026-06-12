from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.evidence import append_candidate_results, evidence_path, list_evidence, record_evidence, set_evidence_status  # noqa: E402
from ai_core.search import context_pack, query, rebuild  # noqa: E402


def _init_repo(path: Path) -> Path:
    repo = path / "repo"
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("project_name: t\n", encoding="utf-8")
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


def _read_records(repo: Path) -> list[dict]:
    return [json.loads(line) for line in evidence_path(repo).read_text(encoding="utf-8").splitlines()]


def test_candidate_records_are_deterministic_redacted_and_path_safe(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    openai_like = "sk-" + "12345678901234567890"
    github_like = "gh" + "p_" + "123456789012345678901234"
    result = {
        "path": "src/app.py:build_index",
        "snippet": f"open /Users/alice/project with {openai_like}",
        "provenance": {"processor": "local", "note": github_like},
    }

    first = append_candidate_results(repo, query=f"token {github_like}", results=[result])
    second = append_candidate_results(repo, query=f"token {github_like}", results=[result])

    assert first["appended"] == 1
    assert second["appended"] == 0
    records = _read_records(repo)
    assert len(records) == 1
    record = records[0]
    assert record["id"] == first["ids"][0]
    assert record["status"] == "candidate"
    assert record["path"] == "src/app.py"
    assert record["symbol"] == "build_index"
    serialized = json.dumps(record, ensure_ascii=False)
    assert "/Users/" not in serialized
    assert openai_like not in serialized
    assert github_like not in serialized

    invalid = append_candidate_results(
        repo,
        query="q",
        results=[
            {"path": "/Users/alice/repo/src/app.py", "snippet": "absolute"},
            {"path": "../outside.py", "snippet": "parent"},
            {"path": ".env", "snippet": "secret"},
            {"path": "src/app.py:/Users/alice/project", "snippet": "symbol path leak"},
        ],
    )
    assert invalid["appended"] == 0
    assert invalid["skipped"] == 4
    assert len(_read_records(repo)) == 1


def test_status_lifecycle_is_append_only(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    created = append_candidate_results(repo, query="q", results=[{"path": "src/app.py", "snippet": "needle"}])
    eid = created["ids"][0]

    curated = set_evidence_status(repo, evidence_id_value=eid, status="curated", note="looks relevant")
    verified = set_evidence_status(repo, evidence_id_value=eid, status="verified", source="test")
    invalid = set_evidence_status(repo, evidence_id_value=eid, status="curated")

    assert curated["ok"] is True and curated["changed"] is True
    assert verified["ok"] is True and verified["record"]["status"] == "verified"
    assert invalid["ok"] is False
    assert invalid["reason"] == "invalid_transition"
    assert [record["status"] for record in _read_records(repo)] == ["candidate", "curated", "verified"]
    listed = list_evidence(repo, status="verified")
    assert listed["count"] == 1
    assert listed["evidence"][0]["id"] == eid


def test_search_and_context_pack_append_deduplicated_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    repo = _init_repo(tmp_path)
    doc = repo / "docs" / "search.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("ledger needle\n", encoding="utf-8")
    rebuild(repo)

    search_result = query(repo, "ledger", limit=1, evidence_source="search")
    context_result = context_pack(repo, "ledger", limit=1)

    assert search_result["ok"] is True
    assert context_result["ok"] is True
    records = _read_records(repo)
    assert [record["source"] for record in records] == ["search"]
    assert all(record["status"] == "candidate" for record in records)
    assert all(record["path"] == "docs/search.md" for record in records)

    repo_context = _init_repo(tmp_path / "context-only")
    doc_context = repo_context / "docs" / "context.md"
    doc_context.parent.mkdir(parents=True)
    doc_context.write_text("context ledger needle\n", encoding="utf-8")
    rebuild(repo_context)

    assert context_pack(repo_context, "context", limit=1)["ok"] is True
    context_records = _read_records(repo_context)
    assert [record["source"] for record in context_records] == ["context_pack"]


def test_explicit_record_evidence_api_and_cli_share_ledger(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    created = record_evidence(
        repo,
        query="install path",
        path="docs/install.md:Install",
        snippet="follow the one-command installer",
        status="curated",
        source="test",
    )
    assert created["ok"] is True
    eid = created["record"]["id"]
    assert created["record"]["path"] == "docs/install.md"
    assert created["record"]["symbol"] == "Install"

    listed = _run_ai(repo, "evidence", "list", "--status", "curated", "--query", "install", "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert json.loads(listed.stdout)["records"][0]["id"] == eid

    updated = _run_ai(repo, "evidence", "update", "--id", eid, "--status", "verified", "--json")
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert json.loads(updated.stdout)["record"]["status"] == "verified"

    duplicate = _run_ai(
        repo,
        "evidence",
        "record",
        "--query",
        "install path",
        "--path",
        "docs/install.md:Install",
        "--snippet",
        "follow the one-command installer",
        "--json",
    )
    assert duplicate.returncode == 0, duplicate.stdout + duplicate.stderr
    assert json.loads(duplicate.stdout)["record"]["id"] == eid


def test_cli_lists_and_sets_evidence_status(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    created = append_candidate_results(repo, query="q", results=[{"path": "src/app.py", "snippet": "needle"}])
    eid = created["ids"][0]

    listed = _run_ai(repo, "memory", "evidence", "list", "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert json.loads(listed.stdout)["evidence"][0]["id"] == eid

    rejected = _run_ai(
        repo,
        "memory",
        "evidence",
        "set-status",
        "--id",
        eid,
        "--status",
        "rejected",
        "--note",
        "bad /Users/alice/project",
        "--json",
    )
    assert rejected.returncode == 0, rejected.stdout + rejected.stderr
    payload = json.loads(rejected.stdout)
    assert payload["record"]["status"] == "rejected"
    assert "/Users/" not in json.dumps(payload, ensure_ascii=False)

    filtered = _run_ai(repo, "memory", "evidence", "list", "--status", "rejected", "--json")
    assert filtered.returncode == 0, filtered.stdout + filtered.stderr
    assert json.loads(filtered.stdout)["count"] == 1


def test_mcp_lists_and_sets_evidence_status(tmp_path: Path) -> None:
    from ai_core import mcp_server

    repo = _init_repo(tmp_path)
    created = append_candidate_results(repo, query="q", results=[{"path": "src/app.py", "snippet": "needle"}])
    eid = created["ids"][0]

    response = mcp_server.handle_request(
        repo,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "evidence_set_status", "arguments": {"id": eid, "status": "curated"}},
        },
    )
    assert response is not None
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["record"]["status"] == "curated"

    listed = mcp_server.handle_request(
        repo,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "evidence_list", "arguments": {"status": "curated"}},
        },
    )
    assert listed is not None
    assert listed["result"]["structuredContent"]["count"] == 1
