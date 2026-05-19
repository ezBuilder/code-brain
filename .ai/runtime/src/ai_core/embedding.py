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

# Xenova publishes an ONNX-quantized export of MiniLM-L6 specifically for
# offline/local consumption (~25MB quantized vs ~80MB fp32). Single explicit
# download — never reached at query time.
_MODEL_URL = "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model_quantized.onnx"
_TOKENIZER_URL = "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer.json"
_CONFIG_URL = "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/config.json"
_MODEL_FILES = {
    "model.onnx": _MODEL_URL,
    "tokenizer.json": _TOKENIZER_URL,
    "config.json": _CONFIG_URL,
}


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


def install_model(root: Path, *, verify_only: bool = False) -> dict[str, Any]:
    """One-shot model fetch from Hugging Face Hub.

    This is the ONLY function in this module that touches the network. It is
    intended to be called once via `ai embedding install` — after success, all
    subsequent calls are fully offline.

    Returns {"ok": bool, "downloaded": [...], "skipped": [...], "errors": [...]}.
    `verify_only=True` reports state without downloading.
    """
    import urllib.request
    import urllib.error

    cache = model_cache_dir(root)
    cache.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "ok": True,
        "cache_dir": str(cache),
        "downloaded": [],
        "skipped": [],
        "errors": [],
    }

    if verify_only:
        for name in _MODEL_FILES:
            target = cache / name
            if target.exists() and target.stat().st_size > 0:
                result["skipped"].append(name)
            else:
                result["errors"].append({"file": name, "reason": "missing"})
        result["ok"] = not result["errors"]
        return result

    for name, url in _MODEL_FILES.items():
        target = cache / name
        if target.exists() and target.stat().st_size > 0:
            result["skipped"].append(name)
            continue
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    out.write(block)
            os.replace(tmp, target)
            result["downloaded"].append({"file": name, "bytes": target.stat().st_size})
        except (urllib.error.URLError, OSError) as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            result["errors"].append({"file": name, "reason": str(exc)[:200]})
            result["ok"] = False

    return result


def uninstall_model(root: Path) -> dict[str, Any]:
    """Delete the cached model dir. Safe even if absent."""
    import shutil

    cache = model_cache_dir(root)
    existed = cache.exists()
    if existed:
        try:
            shutil.rmtree(cache)
        except OSError as exc:
            return {"ok": False, "reason": str(exc)[:200]}
    return {"ok": True, "removed": existed, "cache_dir": str(cache)}
