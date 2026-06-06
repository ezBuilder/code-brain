"""autoresearch_verify tests (Stage 3) — deterministic faithfulness scoring."""
from __future__ import annotations

from ai_core.autoresearch import verify, ingest, storage


def test_verify_exact_claim(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    st = ingest.stage_source(ar, content="the quick brown fox jumps over the lazy dog")
    sid = st["source_id"]
    res = verify.verify_claims(ar, [{"quote": "quick brown fox", "sources": [sid]}])
    c = res["claims"][0]
    assert c["faithfulness"] == 1.0 and c["kind"] == "exact" and c["matched_source"] == sid
    assert res["overall_faithfulness"] == 1.0


def test_verify_missing_source_scores_zero(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    res = verify.verify_claims(ar, [{"quote": "anything", "sources": ["src_not_in_manifest"]}])
    assert res["claims"][0]["faithfulness"] == 0.0 and res["claims"][0]["kind"] == "no_source"


def test_verify_unsupported_quote_scores_low(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    st = ingest.stage_source(ar, content="the quick brown fox")
    res = verify.verify_claims(ar, [{"quote": "totally fabricated unrelated claim", "sources": [st["source_id"]]}])
    assert res["claims"][0]["faithfulness"] == 0.0


def test_verify_mcp_dispatch(tmp_path):
    from ai_core import mcp_server
    proj = tmp_path
    ar = storage.data_root(proj)
    storage.ensure_tree(ar)
    st = ingest.stage_source(ar, content="dense retrieval beats sparse on semantic queries")
    out = mcp_server._dispatch_tool(proj, "autoresearch_verify",
                                    {"claims": [{"quote": "dense retrieval beats sparse", "sources": [st["source_id"]]}]})
    assert out["claims"][0]["faithfulness"] == 1.0
    assert "autoresearch_verify" in mcp_server.TOOL_NAMES
