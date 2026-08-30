from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from ai_core import mcp_server as m


def _search_payload() -> dict[str, object]:
    return {
        "ok": True,
        "query": "needle",
        "retrieval_policy": "bm25",
        "recommended_retrieval_policy": "hybrid",
        "rg_fallback": False,
        "dense_rerank": False,
        "auto_refresh": {"enabled": True, "rebuilt": False, "reason": "current"},
        "results": [
            {
                "path": "src/service.py",
                "chunk_path": "src/service.py::run",
                "scope": "src/service.py › run",
                "qualname": "run",
                "kind": "function",
                "start_line": 10,
                "end_line": 14,
                "snippet": "def run():\n    return 1",
                "provenance": {
                    "processor": "code-brain-local",
                    "model_hash": None,
                    "prompt_version": "extractive-v1",
                    "chunker_version": "1",
                    "confidence": 1.0,
                },
            },
            {
                "path": "src/live.py",
                "scope": "src/live.py",
                "snippet": "VALUE = 1",
                "provenance": {"processor": "ripgrep-live", "confidence": 0.75},
            },
        ],
    }


def _context_payload() -> dict[str, object]:
    search = _search_payload()
    return {
        **search,
        "additionalContext": "- src/service.py: def run(): return 1\n## graph context\n- run -> helper",
        "context_budget": {
            "mode": "balanced",
            "max_bytes": 4096,
            "bytes": 76,
            "truncated": False,
            "representation": "v2",
            "graph_results": 1,
            "graph_truncated": False,
        },
        "context_pack_version": 2,
        "representation": "v2",
        "lexical_refs": [
            {
                "path": "src/service.py",
                "chunk_path": "src/service.py::run",
                "start_line": 10,
                "end_line": 14,
                "qualname": "run",
                "kind": "function",
                "reason": "lexical_symbol_chunk",
                "context_rank": 1,
            }
        ],
        "graph_context": {
            "ok": True,
            "count": 1,
            "additionalContext": "- run -> helper",
            "results": [{"path": "src/service.py", "qualname": "helper", "snippet": "x" * 500}],
            "ranking_policy": "bounded-personalized-pagerank-over-one-hop",
            "ranking_parameters": {"alpha": 0.15, "max_nodes": 2048},
        },
        "retrieval_trace": {
            "schema_version": 1,
            "lexical_policy": "bm25",
            "lexical_results": 1,
            "graph_status": "used",
            "graph_results": 1,
            "fusion": "bounded_context_append",
            "ranking_mutated": False,
        },
        "context_receipt": {
            "schema_version": 1,
            "context_pack_version": 2,
            "representation": "v2",
            "references": [
                {
                    "source": "lexical",
                    "rank": 1,
                    "path": "src/service.py",
                    "start_line": 10,
                    "end_line": 14,
                    "reason": "lexical_symbol_chunk",
                }
            ],
            "receipt_id": "sha256:" + "a" * 64,
        },
    }


def test_hot_tool_schemas_default_to_compact_with_full_escape() -> None:
    for name in ("memory_query", "code_query", "context_pack", "code_read_hashline"):
        tool = next(tool for tool in m.TOOLS if tool["name"] == name)
        detail = tool["inputSchema"]["properties"]["detail"]
        assert detail == {"type": "string", "enum": ["compact", "full"], "default": "compact"}


def test_code_query_compact_keeps_only_agent_decision_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _search_payload()
    monkeypatch.setattr(m, "query", lambda *_args, **_kwargs: deepcopy(payload))

    compact = m._dispatch_tool(tmp_path, "code_query", {"query": "needle"})

    assert compact == {
        "ok": True,
        "hits": [
            {
                "path": "src/service.py",
                "lines": [10, 14],
                "symbol": "run",
                "text": "def run():\n    return 1",
            },
            {
                "path": "src/live.py",
                "text": "VALUE = 1",
                "source": "ripgrep-live",
                "confidence": 0.75,
            },
        ]
    }
    assert m._dispatch_tool(
        tmp_path,
        "code_query",
        {"query": "needle", "detail": "full"},
    ) == payload


def test_context_pack_compact_removes_duplicate_results_graph_and_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _context_payload()
    monkeypatch.setattr(m, "context_pack", lambda *_args, **_kwargs: deepcopy(payload))

    compact = m._dispatch_tool(
        tmp_path,
        "context_pack",
        {"query": "needle", "representation": "v2"},
    )

    assert compact == {
        "ok": True,
        "context": payload["additionalContext"],
        "refs": ["src/service.py:10-14#run"],
        "representation": "v2",
    }
    assert m._dispatch_tool(
        tmp_path,
        "context_pack",
        {"query": "needle", "representation": "v2", "detail": "full"},
    ) == payload


def test_hashline_compact_keeps_hash_anchored_content(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    compact = m._dispatch_tool(
        tmp_path,
        "code_read_hashline",
        {"path": "src.py", "start": 2, "end": 3},
    )
    full = m._dispatch_tool(
        tmp_path,
        "code_read_hashline",
        {"path": "src.py", "start": 2, "end": 3, "detail": "full"},
    )

    assert set(compact) == {"ok", "path", "range", "content"}
    assert compact["range"] == [2, 3]
    assert compact["content"] == full["content"]
    assert full["hash_format"] == "line+sha12|content"


def test_compact_tools_call_uses_one_minified_representation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _context_payload()
    monkeypatch.setattr(m, "context_pack", lambda *_args, **_kwargs: deepcopy(payload))
    base = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "context_pack",
            "arguments": {"query": "needle", "representation": "v2"},
        },
    }

    compact_response = m.handle_request(tmp_path, deepcopy(base))
    full_request = deepcopy(base)
    full_request["params"]["arguments"]["detail"] = "full"
    full_response = m.handle_request(tmp_path, full_request)

    assert compact_response is not None and full_response is not None
    compact_result = compact_response["result"]
    assert "structuredContent" not in compact_result
    compact_text = compact_result["content"][0]["text"]
    assert json.loads(compact_text) == {
        "ok": True,
        "context": payload["additionalContext"],
        "refs": ["src/service.py:10-14#run"],
        "representation": "v2",
    }
    assert '": "' not in compact_text
    assert full_response["result"]["structuredContent"] == payload

    compact_bytes = len(json.dumps(compact_response, ensure_ascii=False, separators=(",", ":")).encode())
    full_bytes = len(json.dumps(full_response, ensure_ascii=False, separators=(",", ":")).encode())
    assert compact_bytes * 4 < full_bytes
