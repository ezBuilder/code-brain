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
