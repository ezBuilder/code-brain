"""Model-tier complexity router tests (Stage 4 §7.2) — deterministic routing suggestion."""
from __future__ import annotations

from ai_core.autoresearch import complexity_router as router


def test_simple_lookup_routes_local():
    r = router.classify("rrf definition")
    assert r["tier"] == "local" and r["complexity"] == "low" and r["signals"] == []


def test_reasoning_multihop_routes_frontier():
    r = router.classify("why does reranking improve MAP and how does it compare to dense retrieval?")
    assert r["tier"] == "frontier" and r["complexity"] == "high"
    assert "reasoning" in r["signals"]


def test_very_long_query_routes_frontier():
    r = router.classify(" ".join(["token"] * 70))
    assert r["tier"] == "frontier" and "long" in r["signals"]


def test_single_signal_stays_local():
    # one reasoning signal, short → medium/local (cost: most calls cheap per §7.2)
    r = router.classify("how to index")
    assert r["tier"] == "local" and r["complexity"] == "medium"


def test_route_mcp_dispatch(tmp_path):
    from ai_core import mcp_server
    out = mcp_server._dispatch_tool(tmp_path, "autoresearch_route",
                                    {"query": "compare bm25 versus dense and analyze the tradeoff"})
    assert out["tier"] == "frontier"
    assert "autoresearch_route" in mcp_server.TOOL_NAMES
