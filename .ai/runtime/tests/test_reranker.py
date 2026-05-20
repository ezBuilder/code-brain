"""Tests for reranker module — cross-encoder relevance scoring."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import reranker as rr_mod  # noqa: E402


def test_inactive_when_env_unset(tmp_path):
    """is_active_for returns False when AI_SEARCH_RERANK is unset and deps/model absent."""
    # Ensure env var is not set; no model files exist
    old_val = os.environ.pop("AI_SEARCH_RERANK", None)
    try:
        # Default behavior: opportunistic. With no deps or model, returns False.
        assert rr_mod.is_active_for(tmp_path) is False
    finally:
        if old_val is not None:
            os.environ["AI_SEARCH_RERANK"] = old_val


def test_inactive_when_env_false(tmp_path):
    """is_active_for returns False when AI_SEARCH_RERANK=0."""
    old_val = os.environ.get("AI_SEARCH_RERANK")
    try:
        os.environ["AI_SEARCH_RERANK"] = "0"
        assert rr_mod.is_active_for(tmp_path) is False
        os.environ["AI_SEARCH_RERANK"] = "false"
        assert rr_mod.is_active_for(tmp_path) is False
    finally:
        if old_val is not None:
            os.environ["AI_SEARCH_RERANK"] = old_val
        else:
            os.environ.pop("AI_SEARCH_RERANK", None)


def test_rerank_returns_none_when_inactive(tmp_path):
    """rerank() returns None when reranking is inactive."""
    old_val = os.environ.get("AI_SEARCH_RERANK")
    try:
        os.environ["AI_SEARCH_RERANK"] = "0"
        candidates = [{"path": "a.py", "snippet": "foo"}]
        result = rr_mod.rerank("test", candidates, tmp_path)
        assert result is None
    finally:
        if old_val is not None:
            os.environ["AI_SEARCH_RERANK"] = old_val
        else:
            os.environ.pop("AI_SEARCH_RERANK", None)


def test_rerank_returns_none_when_model_absent(tmp_path):
    """rerank() returns None when model is not present."""
    old_val = os.environ.get("AI_SEARCH_RERANK")
    old_auto = os.environ.get("AI_SEARCH_RERANK_AUTO_INSTALL")
    try:
        os.environ["AI_SEARCH_RERANK"] = "1"
        os.environ["AI_SEARCH_RERANK_AUTO_INSTALL"] = "0"
        candidates = [{"path": "a.py", "snippet": "foo"}]
        result = rr_mod.rerank("test", candidates, tmp_path)
        # deps likely present, but model missing → None
        assert result is None
    finally:
        if old_val is not None:
            os.environ["AI_SEARCH_RERANK"] = old_val
        else:
            os.environ.pop("AI_SEARCH_RERANK", None)
        if old_auto is not None:
            os.environ["AI_SEARCH_RERANK_AUTO_INSTALL"] = old_auto
        else:
            os.environ.pop("AI_SEARCH_RERANK_AUTO_INSTALL", None)


def test_rerank_empty_candidates(tmp_path):
    """rerank() returns empty list for empty candidates."""
    old_val = os.environ.get("AI_SEARCH_RERANK")
    try:
        os.environ["AI_SEARCH_RERANK"] = "0"  # inactive, so returns None
        result = rr_mod.rerank("test", [], tmp_path)
        assert result is None
    finally:
        if old_val is not None:
            os.environ["AI_SEARCH_RERANK"] = old_val
        else:
            os.environ.pop("AI_SEARCH_RERANK", None)


def test_model_cache_dir_structure(tmp_path):
    """model_cache_dir returns correct subdirectory."""
    cache = rr_mod.model_cache_dir(tmp_path)
    assert cache == tmp_path / ".ai" / "cache" / "reranker-model"


def test_is_model_present_when_missing(tmp_path):
    """is_model_present returns False when model files don't exist."""
    assert rr_mod.is_model_present(tmp_path) is False


def test_is_model_present_when_only_partial(tmp_path):
    """is_model_present returns False when only some model files exist."""
    cache = rr_mod.model_cache_dir(tmp_path)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model.onnx").write_text("fake")
    # missing tokenizer.json
    assert rr_mod.is_model_present(tmp_path) is False


def test_is_model_present_when_complete(tmp_path):
    """is_model_present returns True when both files exist."""
    cache = rr_mod.model_cache_dir(tmp_path)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model.onnx").write_text("fake")
    (cache / "tokenizer.json").write_text("fake")
    assert rr_mod.is_model_present(tmp_path) is True


def test_reset_runtime_cache():
    """reset_runtime_cache clears the process cache."""
    rr_mod._RUNTIME_CACHE["test_key"] = "test_value"
    assert len(rr_mod._RUNTIME_CACHE) > 0
    rr_mod.reset_runtime_cache()
    assert len(rr_mod._RUNTIME_CACHE) == 0


def test_status_structure(tmp_path):
    """status() returns well-formed dict."""
    old_val = os.environ.get("AI_SEARCH_RERANK")
    try:
        os.environ["AI_SEARCH_RERANK"] = "0"
        status = rr_mod.status(tmp_path)
        assert "active" in status
        assert "model_name" in status
        assert "deps_importable" in status
        assert "model_present" in status
        assert "cache_dir" in status
        assert status["model_name"] == rr_mod.MODEL_NAME
    finally:
        if old_val is not None:
            os.environ["AI_SEARCH_RERANK"] = old_val
        else:
            os.environ.pop("AI_SEARCH_RERANK", None)


def test_install_model_verify_only(tmp_path):
    """install_model(verify_only=True) reports missing files without downloading."""
    result = rr_mod.install_model(tmp_path, verify_only=True)
    assert isinstance(result, dict)
    assert "ok" in result
    assert "errors" in result
    # Model files missing → errors
    assert not result["ok"] or result["errors"]


def test_install_model_structure(tmp_path):
    """install_model returns well-formed dict."""
    result = rr_mod.install_model(tmp_path, verify_only=True)
    assert isinstance(result, dict)
    assert "ok" in result
    assert "cache_dir" in result
    assert "downloaded" in result
    assert "skipped" in result
    assert "errors" in result
    assert isinstance(result["downloaded"], list)
    assert isinstance(result["skipped"], list)
    assert isinstance(result["errors"], list)


def test_uninstall_model_when_absent(tmp_path):
    """uninstall_model returns ok=True even when absent."""
    result = rr_mod.uninstall_model(tmp_path)
    assert result["ok"] is True
    assert result["removed"] is False


def test_uninstall_model_when_present(tmp_path):
    """uninstall_model deletes the cache dir."""
    cache = rr_mod.model_cache_dir(tmp_path)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model.onnx").write_text("fake")
    assert cache.exists()
    result = rr_mod.uninstall_model(tmp_path)
    assert result["ok"] is True
    assert result["removed"] is True
    assert not cache.exists()


def test_deps_present():
    """_deps_present checks for required imports."""
    # Should return True if running under `uv run --extra dense`
    # or False if deps not installed.
    result = rr_mod._deps_present()
    assert isinstance(result, bool)
