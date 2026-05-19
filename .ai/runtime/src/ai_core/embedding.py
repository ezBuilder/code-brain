"""Dense embedding module — opt-in offline-first semantic search via ONNX MiniLM.

Activated by AI_SEARCH_DENSE=1. Requires the `dense` optional dependency:
  pip install -e ".[dense]"

When the runtime is installed without `[dense]`, all functions here become
no-ops returning None / empty results — ensuring code-brain's no-deps default
keeps working.

Architecture (per T26 PoC plan):
- Model: sentence-transformers/all-MiniLM-L6-v2 (ONNX export), 384-dim
- Runtime: onnxruntime CPUExecutionProvider (no GPU, no network at query time)
- Cache: model files under .ai/cache/embedding-model/
- Schema: chunks.embeddings_vec0 column stores serialized float32 bytes
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

EMBEDDING_DIM = 384
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def is_enabled() -> bool:
    """True only when user opted in via AI_SEARCH_DENSE AND deps importable."""
    if os.environ.get("AI_SEARCH_DENSE", "0").lower() not in {"1", "true", "yes", "on"}:
        return False
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def model_cache_dir(root: Path) -> Path:
    return root / ".ai" / "cache" / "embedding-model"


def is_model_present(root: Path) -> bool:
    cache = model_cache_dir(root)
    return (cache / "model.onnx").exists() and (cache / "tokenizer.json").exists()


def embed(text: str) -> list[float] | None:
    """Return 384-dim embedding for `text`, or None if dense disabled.

    Stub — full implementation lands in step 3.
    """
    if not is_enabled():
        return None
    return None  # not yet implemented


def embed_batch(texts: list[str]) -> list[list[float]] | None:
    """Batched embedding for indexer. Returns list-of-vectors or None if disabled."""
    if not is_enabled():
        return None
    return None  # not yet implemented


def status(root: Path) -> dict[str, Any]:
    """Health snapshot for obs."""
    return {
        "enabled": is_enabled(),
        "model_name": MODEL_NAME,
        "embedding_dim": EMBEDDING_DIM,
        "deps_importable": _deps_present(),
        "model_present": is_model_present(root),
        "cache_dir": str(model_cache_dir(root).relative_to(root)) if root else None,
    }


def _deps_present() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False
