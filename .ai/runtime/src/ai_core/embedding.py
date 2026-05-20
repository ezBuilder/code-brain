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
import time
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
    """Legacy: env-only check. Kept for backward compat; prefer is_active_for(root)."""
    raw = os.environ.get("AI_SEARCH_DENSE", "").lower()
    if raw in {"1", "true", "yes", "on"}:
        return _deps_present()
    if raw in {"0", "false", "no", "off"}:
        return False
    return False  # unset → off when no root context available


def is_active_for(root: Path) -> bool:
    """True when dense search should fire for `root`.

    Default policy = ON whenever the system can support it. When deps are
    present but the ONNX model is missing, we trigger a one-shot background
    install (idempotent via .install-lock marker) and return False for this
    call only — the next session will find the model and light up.

    Decision tree:
      AI_SEARCH_DENSE=1/true   → on iff deps importable
      AI_SEARCH_DENSE=0/false  → off (explicit opt-out)
      AI_SEARCH_DENSE unset    → ON iff deps + model present;
                                  if deps present but model missing AND
                                  AI_SEARCH_DENSE_AUTO_INSTALL != 0 →
                                  spawn one-shot background download
                                  (~25MB), return False for THIS call.
    """
    raw = os.environ.get("AI_SEARCH_DENSE", "").lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return _deps_present()
    # default: opportunistic activation
    if not _deps_present():
        return False
    if is_model_present(root):
        return True
    # deps ready, model missing — fire-and-forget background install (once)
    if os.environ.get("AI_SEARCH_DENSE_AUTO_INSTALL", "1").lower() in {"1", "true", "yes", "on"}:
        _maybe_spawn_background_install(root)
    return False


def _maybe_spawn_background_install(root: Path) -> None:
    """Spawn `ai embedding install` in the background, idempotent per-root.

    Uses a lock file so concurrent SessionStart calls don't pile up multiple
    downloads.
    """
    import subprocess
    import sys
    lock = model_cache_dir(root) / ".install-lock"
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
            if age < 3600:  # another install attempted within the last hour
                return
        except OSError:
            return
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("running", encoding="utf-8")
    except OSError:
        return
    ai_bin = root / ".ai" / "bin" / "ai"
    if not ai_bin.exists():
        return
    try:
        from .portable import detached_popen_kwargs
        from .process_janitor import cleanup_children, register_child
        cleanup_children(root, ttl_seconds=3600)

        with open(os.devnull, "wb") as devnull:
            cmd = [str(ai_bin), "embedding", "install", "--json"]
            proc = subprocess.Popen(
                cmd,
                stdout=devnull, stderr=devnull, stdin=subprocess.DEVNULL,
                **detached_popen_kwargs(),
            )
        register_child(root, pid=proc.pid, kind="embedding_install", command=cmd)
    except Exception:
        pass


def model_cache_dir(root: Path) -> Path:
    return root / ".ai" / "cache" / "embedding-model"


def is_model_present(root: Path) -> bool:
    cache = model_cache_dir(root)
    return (cache / "model.onnx").exists() and (cache / "tokenizer.json").exists()


# Process-level runtime cache so we don't re-create the ONNX session
# (slow: ~300ms cold) or tokenizer for every query.
_RUNTIME_CACHE: dict[str, Any] = {}
_MAX_SEQ_LEN = 256


def _get_runtime(root: Path):
    """Lazily load (onnx_session, tokenizer). Cached per cache_dir.

    Returns (session, tokenizer) or None if model files are missing or any
    optional dep import fails. Never raises — callers expect None on failure.
    """
    cache = model_cache_dir(root)
    key = str(cache)
    if key in _RUNTIME_CACHE:
        return _RUNTIME_CACHE[key]
    if not is_model_present(root):
        return None
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except ImportError:
        return None
    try:
        sess = ort.InferenceSession(
            str(cache / "model.onnx"),
            providers=["CPUExecutionProvider"],
        )
        tok = Tokenizer.from_file(str(cache / "tokenizer.json"))
        tok.enable_truncation(max_length=_MAX_SEQ_LEN)
        tok.enable_padding(length=None, pad_id=0)
    except Exception:
        return None
    _RUNTIME_CACHE[key] = (sess, tok)
    return _RUNTIME_CACHE[key]


def embed(text: str, root: Path) -> list[float] | None:
    """384-dim embedding for `text`. Returns None when dense disabled / model absent."""
    out = embed_batch([text], root)
    if not out:
        return None
    return out[0]


def embed_batch(texts: list[str], root: Path) -> list[list[float]] | None:
    """Batched embeddings. None if dense disabled or model unavailable.

    Implements the standard sentence-transformers recipe:
      1. tokenize → input_ids, attention_mask
      2. onnx forward → last_hidden_state
      3. mean-pool with attention mask
      4. L2-normalize → unit vectors
    """
    if not is_active_for(root):
        return None
    if not texts:
        return []
    runtime = _get_runtime(root)
    if runtime is None:
        return None
    sess, tok = runtime
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        encodings = tok.encode_batch(list(texts))
        ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
        # Some MiniLM ONNX exports also require token_type_ids; supply zeros.
        feed = {"input_ids": ids, "attention_mask": mask}
        try:
            input_names = {i.name for i in sess.get_inputs()}
        except Exception:
            input_names = set()
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        outputs = sess.run(None, feed)
        last_hidden = outputs[0]  # (batch, seq, dim)
        mask_f = mask.astype(np.float32)[..., None]
        summed = (last_hidden * mask_f).sum(axis=1)
        counts = np.clip(mask_f.sum(axis=1), a_min=1.0, a_max=None)
        pooled = summed / counts
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normalized = (pooled / norms).astype(np.float32)
        return normalized.tolist()
    except Exception:
        return None


def reset_runtime_cache() -> None:
    """Test helper: drop the process-level session cache."""
    _RUNTIME_CACHE.clear()


def status(root: Path) -> dict[str, Any]:
    """Health snapshot for obs."""
    return {
        "enabled": is_enabled(),                # legacy env-only check
        "active": is_active_for(root),          # actual decision used by embed()
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

    # macOS Python often can't find the system CA bundle (Apple ships its own
    # which Python 3.11+ uses via the framework, but homebrew/pyenv builds
    # commonly fail with CERTIFICATE_VERIFY_FAILED). Wire in certifi explicitly.
    import ssl
    try:
        import certifi  # listed in pyproject dependencies
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_ctx = ssl.create_default_context()

    for name, url in _MODEL_FILES.items():
        target = cache / name
        if target.exists() and target.stat().st_size > 0:
            result["skipped"].append(name)
            continue
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            with urllib.request.urlopen(url, timeout=120, context=ssl_ctx) as resp, open(tmp, "wb") as out:
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
