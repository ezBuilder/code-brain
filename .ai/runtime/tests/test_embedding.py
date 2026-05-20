"""embedding module — guards backward compat (zero impact when dense off)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import embedding as emb  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_runtime():
    emb.reset_runtime_cache()
    yield
    emb.reset_runtime_cache()


def test_is_enabled_default_off(monkeypatch):
    """Without explicit AI_SEARCH_DENSE=1, dense is OFF — no behavior change."""
    monkeypatch.delenv("AI_SEARCH_DENSE", raising=False)
    assert emb.is_enabled() is False


def test_is_enabled_requires_both_env_and_deps(monkeypatch):
    monkeypatch.setenv("AI_SEARCH_DENSE", "1")
    # When deps are present (CI usually has them or doesn't), result depends.
    # Test that is_enabled() returns a bool — never raises.
    assert isinstance(emb.is_enabled(), bool)
    monkeypatch.setenv("AI_SEARCH_DENSE", "0")
    assert emb.is_enabled() is False


def test_embed_returns_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SEARCH_DENSE", "0")
    assert emb.embed("hello world", tmp_path) is None
    assert emb.embed_batch(["a", "b"], tmp_path) is None


def test_embed_returns_none_when_model_missing(monkeypatch, tmp_path):
    """Even with AI_SEARCH_DENSE=1, missing model files → None, never crash."""
    monkeypatch.setenv("AI_SEARCH_DENSE", "1")
    # tmp_path has no embedding-model dir
    assert emb.is_model_present(tmp_path) is False
    result = emb.embed("query", tmp_path)
    assert result is None


def test_empty_batch_returns_empty_list(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SEARCH_DENSE", "1")
    assert emb.embed_batch([], tmp_path) == []


def test_status_shape(tmp_path):
    s = emb.status(tmp_path)
    assert set(s.keys()) >= {"enabled", "model_name", "embedding_dim", "model_present"}
    assert s["embedding_dim"] == 384
    assert s["model_name"].startswith("sentence-transformers/")


def test_install_verify_reports_missing(tmp_path):
    """install_model(verify_only=True) does not download; reports gaps."""
    result = emb.install_model(tmp_path, verify_only=True)
    assert result["ok"] is False
    files = {e["file"] for e in result["errors"]}
    assert {"model.onnx", "tokenizer.json", "config.json"} <= files


def test_uninstall_safe_when_absent(tmp_path):
    """uninstall_model on never-installed cache_dir is a no-op."""
    result = emb.uninstall_model(tmp_path)
    assert result["ok"] is True
    assert result["removed"] is False


def test_runtime_cache_has_bounded_cap():
    """ONNX session cache must be bounded — prevents per-root memory leak."""
    assert isinstance(emb._RUNTIME_CACHE_CAP, int)
    assert emb._RUNTIME_CACHE_CAP >= 1


def test_runtime_cache_lru_evicts_oldest():
    """Filling past cap evicts oldest entries (LRU); newest survive."""
    emb.reset_runtime_cache()
    cap = emb._RUNTIME_CACHE_CAP
    sentinel = ("sess", "tok")
    for i in range(cap + 2):
        emb._RUNTIME_CACHE[f"/fake/root-{i}"] = sentinel
        emb._evict_to_cap()
    assert len(emb._RUNTIME_CACHE) == cap
    assert "/fake/root-0" not in emb._RUNTIME_CACHE
    assert "/fake/root-1" not in emb._RUNTIME_CACHE
    assert f"/fake/root-{cap + 1}" in emb._RUNTIME_CACHE
    emb.reset_runtime_cache()
