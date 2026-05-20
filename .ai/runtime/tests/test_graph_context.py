from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.graph_context import pack_graph_context  # noqa: E402
from ai_core.search import rebuild  # noqa: E402


def _build_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "service.py").write_text(
        "def alpha():\n"
        "    helper()\n"
        "    beta()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (src / "consumer.py").write_text(
        "from service import alpha\n\n"
        "def beta():\n"
        "    return 'beta'\n\n"
        "def gamma():\n"
        "    alpha()\n",
        encoding="utf-8",
    )
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    rebuild(tmp_path)
    return tmp_path


def test_pack_graph_context_expands_symbol_to_callers_callees_and_related_symbols(tmp_path: Path):
    root = _build_repo(tmp_path)

    payload = pack_graph_context(root, symbol_query="alpha", limit=20)

    assert payload["ok"] is True
    assert payload["symbol_query"] == "alpha"
    assert any(item["kind"] == "symbol" and item["qualname"] == "alpha" for item in payload["results"])
    assert any(item["kind"] == "edge" and item["relation"] == "caller" and item["caller"] == "gamma" for item in payload["results"])
    assert any(item["kind"] == "edge" and item["relation"] == "callee" and item["callee"] == "helper" for item in payload["results"])
    assert any(item["kind"] == "symbol" and item["role"] == "related" and item["qualname"] == "helper" for item in payload["results"])
    assert "gamma -> alpha" in payload["additionalContext"]


def test_pack_graph_context_accepts_seed_paths_and_bounds_deterministically(tmp_path: Path):
    root = _build_repo(tmp_path)

    first = pack_graph_context(root, seed_paths=["src/service.py"], limit=3)
    second = pack_graph_context(root, seed_paths=["./src/service.py"], limit=3)

    assert first["count"] == 3
    assert [item["path"] for item in first["results"]] == [item["path"] for item in second["results"]]
    assert [item.get("qualname") or item["callee"] for item in first["results"]] == [
        item.get("qualname") or item["callee"] for item in second["results"]
    ]
    assert first["seed_paths"] == ["src/service.py"]


def test_pack_graph_context_redacts_snippets(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    fake_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    (src / "secretish.py").write_text(
        "def leak():\n"
        f"    token = '{fake_token}'\n"
        "    return token\n",
        encoding="utf-8",
    )
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    rebuild(tmp_path)

    payload = pack_graph_context(tmp_path, symbol_query="leak", limit=5)
    rendered = "\n".join(item["snippet"] for item in payload["results"])

    assert "ghp_" not in rendered
    assert "[REDACTED]" in rendered
