from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import mcp_server, plan_state  # noqa: E402


def _req(root: Path, method: str, params: dict | None = None, req_id: int = 1) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    resp = mcp_server.handle_request(root, payload)
    assert resp is not None
    return resp


def _seed(root: Path) -> None:
    plan_state.init_plan(root, plan_id="alpha", steps=["one", "two"], title="Alpha")
    session = root / ".ai" / "memory" / "session-current.md"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("# Session\n\n- did a thing\n", encoding="utf-8")


# ---- default OFF: behavior unchanged ----------------------------------------


def test_resources_list_empty_when_disabled(tmp_path: Path, monkeypatch) -> None:
    # default is now ON; explicit AI_MCP_RESOURCES=0 disables.
    monkeypatch.setenv("AI_MCP_RESOURCES", "0")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/list")
    assert resp["result"]["resources"] == []


def test_resources_read_rejected_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "0")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {"uri": "codebrain://plan/alpha"})
    assert resp["error"]["code"] == -32602


# ---- opt-in ON: list ---------------------------------------------------------


def test_resources_list_includes_plan_and_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/list")
    resources = resp["result"]["resources"]
    uris = {r["uri"] for r in resources}
    assert "codebrain://plan/alpha" in uris
    assert "codebrain://session/current" in uris
    assert "codebrain://report/status" in uris
    # Every entry carries the required descriptor fields.
    for r in resources:
        assert set(r) >= {"uri", "name", "description", "mimeType"}
    # handoff.json was not written, so it must not appear.
    assert "codebrain://handoff/current" not in uris


def test_resources_list_includes_handoff_when_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    handoff = tmp_path / ".ai" / "memory" / "handoff.json"
    handoff.write_text('{"goal": "ship it"}', encoding="utf-8")
    resp = _req(tmp_path, "resources/list")
    uris = {r["uri"] for r in resp["result"]["resources"]}
    assert "codebrain://handoff/current" in uris


# ---- opt-in ON: read ---------------------------------------------------------


def test_resources_read_returns_plan_body(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {"uri": "codebrain://plan/alpha"})
    contents = resp["result"]["contents"]
    assert isinstance(contents, list) and len(contents) == 1
    body = contents[0]
    assert body["uri"] == "codebrain://plan/alpha"
    assert body["mimeType"] == "text/markdown"
    assert "- [ ] one" in body["text"]
    assert "- [ ] two" in body["text"]


def test_resources_read_returns_session_body(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {"uri": "codebrain://session/current"})
    body = resp["result"]["contents"][0]
    assert body["mimeType"] == "text/markdown"
    assert "did a thing" in body["text"]


def test_resources_read_report_status_is_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {"uri": "codebrain://report/status"})
    body = resp["result"]["contents"][0]
    assert body["mimeType"] == "application/json"
    import json

    parsed = json.loads(body["text"])
    # status_report may fail in a bare tmp repo (no .ai/config.yaml); either way the
    # resource returns a well-formed JSON body with an "ok" field.
    assert isinstance(parsed, dict)
    assert "ok" in parsed


def test_resources_read_redacts_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    session = tmp_path / ".ai" / "memory" / "session-current.md"
    session.write_text("token: ghp_" + "a" * 36 + "\n", encoding="utf-8")
    resp = _req(tmp_path, "resources/read", {"uri": "codebrain://session/current"})
    text = resp["result"]["contents"][0]["text"]
    assert "ghp_" not in text
    assert "[REDACTED]" in text


# ---- opt-in ON: rejection paths ---------------------------------------------


def test_resources_read_unknown_uri_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {"uri": "codebrain://plan/does-not-exist"})
    assert resp["error"]["code"] == -32602


def test_resources_read_unknown_scheme_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {"uri": "file:///etc/passwd"})
    assert resp["error"]["code"] == -32602


@pytest.mark.parametrize(
    "uri",
    [
        "codebrain://plan/../../../../etc/passwd",
        "codebrain://plan/..%2f..%2fsecrets",
        "codebrain://plan/a/b",
        "codebrain://plan/",
        "codebrain://../generated/manifest.json",
    ],
)
def test_resources_read_path_traversal_rejected(tmp_path: Path, monkeypatch, uri: str) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {"uri": uri})
    assert resp["error"]["code"] == -32602


def test_resources_read_missing_uri_param_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    _seed(tmp_path)
    resp = _req(tmp_path, "resources/read", {})
    assert resp["error"]["code"] == -32602
