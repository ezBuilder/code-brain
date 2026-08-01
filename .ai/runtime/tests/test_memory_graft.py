"""Tests for the memanto-inspired graft: decision filter, unified recall, conflict sidecar."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import memory  # noqa: E402
from ai_core import memory_conflicts as mc  # noqa: E402
from ai_core import memory_recall as mr  # noqa: E402
from ai_core import mcp_server  # noqa: E402


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --- G3: read_decisions_filtered ---------------------------------------------

def test_filter_by_kind_and_retired_exclusion(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="plain alpha", tags=["arch"])
    f = memory.append_decision(root, text="beta broke", kind="failure",
                               observed_versions={"torch": "2.4.0"})["record"]
    memory.append_decision(root, text="beta works now", kind="failure",
                           status="refuted", supersedes_id=f["id"])

    only_dec = memory.read_decisions_filtered(root, kind="decision")
    assert [i["decision"] for i in only_dec["items"]] == ["plain alpha"]

    only_fail = memory.read_decisions_filtered(root, kind="failure")
    assert only_fail["count"] == 0  # the single failure was retired (refuted) → excluded

    with_retired = memory.read_decisions_filtered(root, kind="failure", include_retired=True)
    assert with_retired["count"] == 1 and with_retired["items"][0]["status"] == "refuted"


def test_filter_by_tag_and_text(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="use ruff for linting", tags=["lint", "python"])
    memory.append_decision(root, text="use pnpm for js deps", tags=["js"])

    by_tag = memory.read_decisions_filtered(root, tag="python")
    assert by_tag["count"] == 1 and "ruff" in by_tag["items"][0]["decision"]

    by_text = memory.read_decisions_filtered(root, text="pnpm")
    assert by_text["count"] == 1 and "pnpm" in by_text["items"][0]["decision"]


# --- G2: recall_memory -------------------------------------------------------

def test_recall_spans_stores_and_respects_type_filter(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="use tabs for indentation")
    from ai_core.lessons import add_lesson
    add_lesson(root, source="op", failure="mixed indentation broke parser",
               cause="tabs vs spaces", fix="enforce tabs for indentation")

    allres = mr.recall_memory(root, query="indentation tabs")
    kinds = {i["kind"] for i in allres["items"]}
    assert "decision" in kinds and "lesson" in kinds
    assert allres["block"].startswith("### Memory recall:")

    only_lessons = mr.recall_memory(root, query="indentation tabs", types=["lesson"])
    assert {i["kind"] for i in only_lessons["items"]} == {"lesson"}


def test_recall_empty_query_is_safe(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    res = mr.recall_memory(root, query="   ")
    assert res["ok"] and res["count"] == 0


# --- G4: memory_conflicts ----------------------------------------------------

def test_conflict_detects_opposite_polarity(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="use ruff for linting python code")
    memory.append_decision(root, text="never use ruff for linting python code")
    memory.append_decision(root, text="unrelated deploy schedule note")

    dry = mc.scan_conflicts(root, dry_run=True)
    assert dry["written"] == 0
    assert len(dry["candidates"]) == 1
    assert not mc.conflicts_path(root).exists()

    live = mc.scan_conflicts(root)
    assert live["written"] == 1
    listed = mc.list_conflicts(root)
    assert listed["count"] == 1 and listed["items"][0]["overlap"] >= 0.5


def test_conflict_rescan_is_idempotent(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="always cache embeddings on disk")
    memory.append_decision(root, text="do not cache embeddings on disk")
    assert mc.scan_conflicts(root)["written"] == 1
    assert mc.scan_conflicts(root)["written"] == 0  # already recorded → not re-flagged


def test_conflict_same_polarity_not_flagged(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="use ruff for linting python code")
    memory.append_decision(root, text="use ruff for linting python modules")
    assert mc.scan_conflicts(root, dry_run=True)["candidates"] == []


# --- MCP wiring --------------------------------------------------------------

def test_mcp_exposes_new_read_tools(tmp_path: Path) -> None:
    mcp_server._invalidate_tools_list_cache()
    resp = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "memory_recall" in names and "list_decisions" in names


def test_mcp_list_decisions_dispatch(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="use ruff for linting", tags=["lint"])
    resp = mcp_server.handle_request(root, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "arguments": {}, "params": {"name": "list_decisions", "arguments": {"tag": "lint"}},
    })
    payload = resp["result"]["structuredContent"]
    assert payload["ok"] and payload["count"] == 1


def _call_tool(root: Path, name: str, arguments: dict, *, request_id: int = 3) -> dict:
    resp = mcp_server.handle_request(root, {
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return resp["result"]["structuredContent"]


def _last_decision(root: Path) -> dict:
    return memory.read_jsonl_all(memory.decisions_path(root))[-1]


def test_mcp_record_decision_forwards_dag_edges_and_expiry(tmp_path: Path) -> None:
    """append_decision and the CLI already accepted these three; MCP silently dropped them,
    which left expires_at — the only way to time-box a plain decision — unreachable from an
    agent."""
    root = _seed(tmp_path)
    payload = _call_tool(root, "record_decision", {
        "text": "pin torch 2.4 for the trainer",
        "contradicts": "dec-abcd1234",
        "derives_from": "dec-0f0f0f0f",
        "expires_at": "2099-01-01",
    })
    assert payload["ok"] is True

    stored = _last_decision(root)
    assert stored["contradicts"] == "dec-abcd1234"
    assert stored["derives_from"] == "dec-0f0f0f0f"
    # a date-only bound widens to the last instant of that day
    assert stored["expires_at"] == "2099-01-01T23:59:59.999999Z"


def test_mcp_record_decision_expiry_actually_retires_the_row(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    assert _call_tool(root, "record_decision", {
        "text": "ledger cutover freeze", "tags": ["ledger"], "expires_at": "2000-01-01",
    })["ok"] is True
    assert _last_decision(root)["expires_at"] == "2000-01-01T23:59:59.999999Z"

    plain, _failures = memory.read_decisions_for_surface(root, limit=10)
    assert [p["decision"] for p in plain] == []  # never injected again


def test_mcp_record_decision_drops_malformed_edges_fail_soft(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    assert _call_tool(root, "record_decision", {
        "text": "fail-soft edges", "contradicts": "not-an-id", "expires_at": "2026",
    })["ok"] is True
    stored = _last_decision(root)
    assert "contradicts" not in stored and "expires_at" not in stored


def test_mcp_record_decision_without_new_params_stays_legacy_shape(tmp_path: Path) -> None:
    """Byte-identity guard: omitting every new parameter writes exactly the legacy keys."""
    root = _seed(tmp_path)
    assert _call_tool(root, "record_decision", {
        "text": "plain decision via mcp", "tags": ["x"],
    })["ok"] is True
    stored = _last_decision(root)
    assert set(stored.keys()) == {"id", "decided_at", "decision", "tags", "source"}
    assert stored["source"] == "agent"


def test_mcp_list_decisions_honors_include_expired(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="ledger cutover freeze", tags=["ledger"],
                           expires_at="2000-01-01")

    assert _call_tool(root, "list_decisions", {"tag": "ledger"}, request_id=4)["count"] == 0

    with_expired = _call_tool(
        root, "list_decisions", {"tag": "ledger", "include_expired": True}, request_id=5
    )
    assert with_expired["count"] == 1
    assert with_expired["items"][0]["decision"] == "ledger cutover freeze"


def test_mcp_record_decision_schema_exposes_the_new_parameters(tmp_path: Path) -> None:
    mcp_server._invalidate_tools_list_cache()
    resp = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    record_props = set(tools["record_decision"]["inputSchema"]["properties"])
    assert {"contradicts", "derives_from", "expires_at"} <= record_props
    assert "include_expired" in tools["list_decisions"]["inputSchema"]["properties"]
    mcp_server._invalidate_tools_list_cache()
