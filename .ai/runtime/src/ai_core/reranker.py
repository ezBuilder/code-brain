"""Lightweight cross-encoder reranker — opt-in post-retrieval relevance scoring.

Activated by AI_SEARCH_RERANK=1. Requires the `dense` optional dependency:
  pip install -e ".[dense]"

Model: Xenova/ms-marco-MiniLM-L-6-v2 (ONNX quantized), ~23MB.
Architecture: cross-encoder (query + document pair → single relevance score).

When the runtime is installed without `[dense]`, all functions here become
no-ops returning None — ensuring code-brain's no-deps default keeps working.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

MODEL_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"

# Xenova publishes an ONNX-quantized export of ms-marco-MiniLM specifically for
# offline/local consumption (~23MB quantized). Single explicit download —
# never reached at query time.
_MODEL_URL = "https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2/resolve/main/onnx/model_quantized.onnx"
_TOKENIZER_URL = "https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2/resolve/main/tokenizer.json"
_CONFIG_URL = "https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2/resolve/main/config.json"
_MODEL_FILES = {
    "model.onnx": _MODEL_URL,
    "tokenizer.json": _TOKENIZER_URL,
    "config.json": _CONFIG_URL,
}


def is_active_for(root: Path) -> bool:
    """True when reranking should fire for `root`.

    Default policy = ON whenever the system can support it. When deps are
    present but the ONNX model is missing, we trigger a one-shot background
    install (idempotent via .install-lock marker) and return False for this
    call only — the next session will find the model and light up.

    Decision tree:
      AI_SEARCH_RERANK=1/true   → on iff deps importable
      AI_SEARCH_RERANK=0/false  → off (explicit opt-out)
      AI_SEARCH_RERANK unset    → ON iff deps + model present;
                                  if deps present but model missing AND
                                  AI_SEARCH_RERANK_AUTO_INSTALL != 0 →
                                  spawn one-shot background download
                                  (~23MB), return False for THIS call.
    """
    raw = os.environ.get("AI_SEARCH_RERANK", "").lower()
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
    if os.environ.get("AI_SEARCH_RERANK_AUTO_INSTALL", "1").lower() in {"1", "true", "yes", "on"}:
        _maybe_spawn_background_install(root)
    return False


def _maybe_spawn_background_install(root: Path) -> None:
    """Spawn `ai reranker install` in the background, idempotent per-root.

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
            cmd = [str(ai_bin), "reranker", "install", "--json"]
            proc = subprocess.Popen(
                cmd,
                stdout=devnull, stderr=devnull, stdin=subprocess.DEVNULL,
                **detached_popen_kwargs(),
            )
        register_child(root, pid=proc.pid, kind="reranker_install", command=cmd)
    except Exception:
        pass


def model_cache_dir(root: Path) -> Path:
    return root / ".ai" / "cache" / "reranker-model"


def is_model_present(root: Path) -> bool:
    cache = model_cache_dir(root)
    return (cache / "model.onnx").exists() and (cache / "tokenizer.json").exists()


# Process-level runtime cache so we don't re-create the ONNX session
# (slow: ~100ms cold) or tokenizer for every query.
#
# Bounded LRU: each ONNX session holds ~30-50 MB. Long-running callers (MCP
# server, test harness) can rotate across many roots; an unbounded dict would
# leak that footprint per distinct root.
_RUNTIME_CACHE: dict[str, Any] = {}
_RUNTIME_CACHE_CAP = 2
_MAX_SEQ_LEN = 512


def _evict_to_cap() -> None:
    """Drop oldest entries until ``_RUNTIME_CACHE`` is within capacity."""
    while len(_RUNTIME_CACHE) > _RUNTIME_CACHE_CAP:
        _RUNTIME_CACHE.pop(next(iter(_RUNTIME_CACHE)), None)


def _get_runtime(root: Path):
    """Lazily load (onnx_session, tokenizer). Cached per cache_dir.

    Returns (session, tokenizer) or None if model files are missing or any
    optional dep import fails. Never raises — callers expect None on failure.
    """
    cache = model_cache_dir(root)
    key = str(cache)
    if key in _RUNTIME_CACHE:
        _RUNTIME_CACHE[key] = _RUNTIME_CACHE.pop(key)  # LRU touch
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
    _evict_to_cap()
    return _RUNTIME_CACHE[key]


def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    root: Path,
    *,
    top_k: int | None = None,
) -> list[dict[str, Any]] | None:
    """Rerank candidates using cross-encoder relevance scoring.

    Input candidates: list of dicts with at least {'path', 'snippet', ...}.
    Output: same dicts with new 'rerank_score' field, sorted desc by score,
            limited to top_k. Returns None if reranking inactive/unavailable.

    Never raises; returns None on any error (deps missing, model absent, etc).
    """
    if not is_active_for(root):
        return None
    if not candidates:
        return []
    if top_k is None:
        try:
            top_k = int(os.environ.get("AI_SEARCH_RERANK_TOP_K", "20"))
        except ValueError:
            top_k = 20
    runtime = _get_runtime(root)
    if runtime is None:
        return None
    sess, tok = runtime
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        # Build list of (query, document_text) pairs for cross-encoder.
        # Extract snippet as the primary relevance signal.
        pairs = [(query, cand.get("snippet", "")) for cand in candidates]
        if not pairs:
            return None
        # Encode all pairs in one batch: [CLS] query [SEP] doc [SEP]
        encodings = tok.encode_batch(pairs)
        ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
        # ms-marco-MiniLM may require token_type_ids; check model inputs.
        feed = {"input_ids": ids, "attention_mask": mask}
        try:
            input_names = {i.name for i in sess.get_inputs()}
        except Exception:
            input_names = set()
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        # Run forward pass: outputs[0] is logits shape (batch, 1)
        outputs = sess.run(None, feed)
        logits = outputs[0]  # shape: (batch, 1)
        # Extract relevance score (single float per pair).
        scores = logits.flatten().astype(np.float32).tolist()
        # Attach scores and sort desc.
        scored = []
        for cand, score in zip(candidates, scores):
            cand_copy = dict(cand)
            cand_copy["rerank_score"] = float(score)
            scored.append(cand_copy)
        scored.sort(key=lambda x: -x["rerank_score"])
        return scored[:top_k]
    except Exception:
        return None


def reset_runtime_cache() -> None:
    """Test helper: drop the process-level session cache."""
    _RUNTIME_CACHE.clear()


def status(root: Path) -> dict[str, Any]:
    """Health snapshot for obs."""
    return {
        "active": is_active_for(root),
        "model_name": MODEL_NAME,
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
    intended to be called once via `ai reranker install` — after success, all
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
