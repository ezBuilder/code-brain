from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import mcp_server  # noqa: E402
from ai_core import search as search_mod  # noqa: E402


def _build_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".ai" / "cache").mkdir(parents=True)
    (root / ".ai" / "config.yaml").write_text(
        "project_name: context-pack-v2\nsearch:\n  retriever: bm25\n",
        encoding="utf-8",
    )
    (root / "src" / "service.py").write_text(
        "def alpha():\n"
        "    helper()\n"
        "    return 'top-secret-body-must-not-leak'\n\n"
        "def helper():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "src" / "consumer.py").write_text(
        "from service import alpha\n\n"
        "def gamma():\n"
        "    alpha()\n",
        encoding="utf-8",
    )
    assert search_mod.rebuild(root)["ok"] is True
    return root


def test_v2_is_default_and_legacy_remains_explicit_rollback(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)

    default = search_mod.context_pack(root, "alpha", limit=5)
    explicit_v2 = search_mod.context_pack(root, "alpha", limit=5, representation="v2")
    legacy = search_mod.context_pack(root, "alpha", limit=5, representation="legacy")

    assert explicit_v2 == default
    assert default["representation"] == "v2"
    assert default["graph_context"]["ranking_applied"] is True
    assert "context_pack_version" not in legacy
    assert "graph_context" not in legacy


def test_v2_adds_bounded_graph_and_span_contract_without_reranking(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)

    payload = search_mod.context_pack(root, "alpha", limit=10, representation="v2")

    assert payload["ok"] is True
    assert payload["context_pack_version"] == 2
    assert payload["representation"] == "v2"
    assert payload["retrieval_trace"]["ranking_mutated"] is False
    assert payload["retrieval_trace"]["lexical_ranking_mutated"] is False
    assert payload["retrieval_trace"]["graph_ranking_applied"] is True
    assert payload["retrieval_trace"]["graph_ranking_policy"] == "bounded-personalized-pagerank-over-one-hop"
    assert payload["retrieval_trace"]["graph_symbol_seeded"] is True
    assert payload["graph_context"]["ok"] is True
    assert payload["graph_context"]["representation"] == "full"
    assert payload["graph_context"]["count"] > 0
    assert any(item.get("qualname") == "alpha" for item in payload["graph_context"]["results"])
    assert payload["lexical_refs"]
    assert all(not Path(ref["path"]).is_absolute() for ref in payload["lexical_refs"])
    assert "## graph context" in payload["additionalContext"]
    assert len(payload["additionalContext"].encode("utf-8")) <= payload["context_budget"]["max_bytes"]


def test_refs_only_omits_source_bodies_and_emits_path_span_reason(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)

    payload = search_mod.context_pack(root, "alpha", limit=10, representation="refs-only")
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["representation"] == "refs-only"
    assert payload["graph_context"]["representation"] == "refs-only"
    assert "top-secret-body-must-not-leak" not in serialized
    assert payload["results"] == payload["lexical_refs"]
    assert all({"path", "start_line", "end_line", "reason"} <= set(ref) for ref in payload["results"])
    assert all("snippet" not in item and "summary" not in item for item in payload["graph_context"]["results"])
    assert "reason=" in payload["additionalContext"]


def test_v2_receipt_is_deterministic_and_excludes_query_and_source_bodies(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)

    first = search_mod.context_pack(root, "alpha", limit=10, representation="v2")
    second = search_mod.context_pack(root, "alpha", limit=10, representation="v2")
    receipt = first["context_receipt"]
    canonical = {key: value for key, value in receipt.items() if key != "receipt_id"}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    assert receipt == second["context_receipt"]
    assert receipt["receipt_id"] == f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"
    assert receipt["references"]
    assert all(set(ref) == {"source", "rank", "path", "start_line", "end_line", "reason"} for ref in receipt["references"])
    serialized = json.dumps(receipt, sort_keys=True)
    assert "query" not in serialized
    assert "top-secret-body-must-not-leak" not in serialized


def _file_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            body = path.read_bytes()
            snapshot[path.relative_to(root).as_posix()] = (len(body), hashlib.sha256(body).hexdigest())
    return snapshot


def test_default_context_pack_repetition_does_not_grow_or_mutate_files(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    warm = search_mod.context_pack(root, "alpha", limit=10)
    before = _file_snapshot(root)

    for _ in range(50):
        payload = search_mod.context_pack(root, "alpha", limit=10)
        assert payload["representation"] == "v2"
        assert payload["graph_context"]["ranking_parameters"]["max_nodes"] == 2_048
        assert len(payload["additionalContext"].encode("utf-8")) <= payload["context_budget"]["max_bytes"]

    assert payload["context_receipt"] == warm["context_receipt"]
    assert _file_snapshot(root) == before


def test_receipt_preserves_graph_source_trust_without_source_bodies() -> None:
    receipt = search_mod._context_receipt(
        representation="v2",
        mode="balanced",
        limit=5,
        lexical_refs=[],
        graph_payload={
            "schema_version": 2,
            "ranking_policy": "one-hop deterministic",
            "source_generation": "123",
            "results": [
                {
                    "path": "src/service.py",
                    "span": {"start_line": 1, "end_line": 3},
                    "role": "seed",
                    "source_status": "stale",
                    "snippet": "must-not-enter-receipt",
                }
            ],
        },
        retrieval_trace={"lexical_policy": "bm25", "graph_status": "used"},
    )

    reference = next(item for item in receipt["references"] if item["source"] == "graph")
    assert reference["reason"] == "stale_source"
    assert "must-not-enter-receipt" not in json.dumps(receipt, sort_keys=True)


def test_invalid_representation_rejects_before_query_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_mod,
        "query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("query must not run")),
    )

    with pytest.raises(ValueError, match="representation"):
        search_mod.context_pack(Path("."), "alpha", representation="unknown")


def test_mcp_schema_and_dispatch_expose_opt_in_representation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "context_pack")
    representation = tool["inputSchema"]["properties"]["representation"]
    assert representation["enum"] == ["legacy", "v2", "skeleton", "refs-only"]
    assert representation["default"] == "v2"

    captured: dict[str, object] = {}

    def fake_context_pack(root: Path, query: str, **kwargs):
        captured.update({"root": root, "query": query, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "context_pack", fake_context_pack)
    result = mcp_server._dispatch_tool(
        tmp_path,
        "context_pack",
        {"query": "alpha", "limit": 3, "mode": "aggressive", "representation": "skeleton"},
    )

    assert result == {"ok": True}
    assert captured["representation"] == "skeleton"
    assert captured["limit"] == 3
    assert captured["mode"] == "aggressive"

    captured.clear()
    mcp_server._dispatch_tool(tmp_path, "context_pack", {"query": "alpha"})
    assert captured["representation"] == "v2"
