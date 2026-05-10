from __future__ import annotations

import json
import os
from pathlib import Path

from test_cli import copy_repo, run_ai, run_ai_input

CONFIG_INVALID = 10
PERMISSION_DENIED = 16


def _enable_remote_memory(repo: Path, *, inject: bool = False) -> None:
    config = repo / ".ai" / "config.yaml"
    text = config.read_text(encoding="utf-8")
    text = text.replace("  remote_memory: false", "  remote_memory: true")
    text = text.replace("  inject_on_session_start: false", f"  inject_on_session_start: {str(inject).lower()}")
    config.write_text(text, encoding="utf-8")


def test_remote_memory_default_off_status_ok(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    config = repo / ".ai" / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8").replace("  remote_memory: true", "  remote_memory: false"), encoding="utf-8")
    result = run_ai("remote-memory", "status", "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["enabled"] is False
    assert payload["ok"] is False


def test_remote_memory_enabled_missing_token_fails_clearly(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    _enable_remote_memory(repo)
    env = os.environ.copy()
    env.pop("AI_REMOTE_MEMORY_URL", None)
    env.pop("AI_REMOTE_MEMORY_TOKEN", None)
    result = run_ai("remote-memory", "status", "--json", cwd=repo, env=env)
    assert result.returncode == CONFIG_INVALID
    payload = json.loads(result.stdout)
    assert payload["enabled"] is True
    assert payload["configured"] is False
    assert "AI_REMOTE_MEMORY_URL" in payload["reason"]


def test_remote_memory_write_rejected_in_ci_before_network(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    _enable_remote_memory(repo)
    result = run_ai(
        "remote-memory",
        "remember",
        "--text",
        "remember this safe preference",
        "--json",
        cwd=repo,
        env={"AI_CI": "1", "AI_REMOTE_MEMORY_URL": "https://example.invalid", "AI_REMOTE_MEMORY_TOKEN": "x" * 32},
    )
    assert result.returncode == PERMISSION_DENIED
    assert json.loads(result.stdout)["command"] == "remote_memory"


def test_session_start_injects_cached_remote_memory_without_network(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    _enable_remote_memory(repo, inject=True)
    cache = repo / ".ai" / "cache" / "remote-memory" / "summary.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"recent":[{"summary":"Use project-scoped recall by default"}]}\n', encoding="utf-8")
    result = run_ai_input(
        "hook",
        "SessionStart",
        "--json",
        stdin=json.dumps({"agent": "codex", "dry": True}),
        cwd=repo,
        env={"AI_REMOTE_MEMORY_URL": "https://should-not-be-called.invalid", "AI_REMOTE_MEMORY_TOKEN": "x" * 32},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ctx = json.loads(result.stdout)["additionalContext"]
    assert "Remote memory cached summary" in ctx
    assert "Use project-scoped recall by default" in ctx


def test_mcp_lists_remote_memory_tools(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    result = run_ai("mcp", "--once-json", json.dumps(request), cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    names = {tool["name"] for tool in json.loads(result.stdout)["result"]["tools"]}
    assert {"remote_memory_recall", "remote_memory_remember", "remote_memory_list_recent", "remote_memory_forget"} <= names


def test_worker_source_enforces_auth_and_scoping() -> None:
    source = (Path(__file__).resolve().parents[3] / "remote-memory" / "cloudflare-worker" / "src" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert 'headers["Access-Control-Allow-Origin"] = origin' in source
    assert 'Access-Control-Allow-Origin": "*"' not in source
    assert "const unauthorized = rejectUnauthorized(request, env);" in source
    assert 'url.pathname === "/mcp"' in source
    assert "meta.scope === \"global\" || meta.project_id === projectId" in source
    assert "content: c.slice(0, 512)" not in source
