"""Consolidated HOT-tier cache for memory_tier page-in (T30 step C).

A small, salience-ranked, byte-bounded cache written by the offline sleep-time
page-in path and read by SessionStart. Keeps `memory_tier.page_in` thin and
matches the repo's one-concern-per-module style (cf. audit_fold, memory_staleness).

Pure stdlib, deterministic, fail-soft. No network. The cache lives under
.ai/cache/ so it is disposable and never source-of-truth.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .private_write import atomic_write_private_text, read_root_confined_text

# Default ceiling on the serialized cache so SessionStart injection stays small
# (the "fewer tokens" win). Env-overridable. ~4 KB is well under any hook budget.
HOT_CACHE_BYTES_DEFAULT = 4096
# Default count of consolidated HOT items (top WARM+HOT by retention + recency).
HOT_LIMIT_DEFAULT = 12


def hot_cache_path(root: Path) -> Path:
    """Location of the consolidated HOT cache file."""
    return root / ".ai" / "cache" / "memory-hot.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def consolidate_hot_items(
    root: Path, *, limit: int, max_bytes: int
) -> list[dict[str, Any]]:
    """Build a compact, salience-ranked HOT set from durable memory scoring.

    Reuses `memory_tier.scored_durable_items` (decay + reinforcement + per-type
    weight) as the single source of truth for ranking — without inflating the
    public `retention_report` output. Promotes the top hot/warm durable items,
    ranked by (score desc, recency, kind, ref) so the ordering is byte-stable
    across identical runs. Caps both by `limit` and by serialized `max_bytes`.
    Read-only; never writes.
    """
    from . import memory_tier as mt

    scored = mt.scored_durable_items(root)

    # Keep only items already in the durable HOT/WARM band — those are the
    # consolidation candidates the next SessionStart should page in.
    candidates = [
        it for it in scored
        if isinstance(it, dict) and str(it.get("tier")) in ("hot", "warm")
    ]

    # Deterministic ranking: highest retention first, then freshest (smallest
    # age_days), then kind/ref for a stable tie-break → byte-identical output.
    def _sort_key(it: dict[str, Any]) -> tuple[float, float, str, str]:
        score = float(it.get("score") or 0.0)
        age = float(it.get("age_days") or 0.0)
        return (-score, age, str(it.get("kind") or ""), str(it.get("ref") or ""))

    candidates.sort(key=_sort_key)

    lim = max(0, int(limit))
    out: list[dict[str, Any]] = []
    for it in candidates:
        if len(out) >= lim:
            break
        entry = {
            "kind": str(it.get("kind") or "")[:32],
            "ref": str(it.get("ref") or "")[:80],
            "score": round(float(it.get("score") or 0.0), 4),
            "tier": str(it.get("tier") or ""),
        }
        # Byte-bound: only append while the serialized list stays within budget.
        trial = out + [entry]
        if len(json.dumps(trial, ensure_ascii=False).encode("utf-8")) > max(0, int(max_bytes)):
            break
        out = trial
    return out


def write_hot_cache(
    root: Path,
    items: list[dict[str, Any]],
    *,
    counts: dict[str, int],
    limit: int,
) -> dict[str, Any]:
    """Atomically persist the consolidated HOT cache (tmp + os.replace)."""
    path = hot_cache_path(root)
    payload = {
        "ok": True,
        "generated_at": _now_iso(),
        "items": items,
        "counts": dict(counts),
        "limit": int(limit),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_private_text(path, json.dumps(payload, ensure_ascii=False), root=root)
    return payload


def read_hot_cache(
    root: Path, *, max_age_seconds: float | None = None
) -> dict[str, Any] | None:
    """Read the consolidated HOT cache, or None when missing/stale/corrupt.

    Fully fail-soft — any error returns None so callers degrade gracefully.
    """
    import time

    path = hot_cache_path(root)
    try:
        text, state = read_root_confined_text(
            path,
            root=root,
            max_bytes=max(HOT_CACHE_BYTES_DEFAULT * 4, 65536),
            require_private=True,
        )
        if max_age_seconds is not None:
            age = time.time() - state.st_mtime
            if age > float(max_age_seconds):
                return None
        payload = json.loads(text)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    if not isinstance(payload.get("items"), list):
        return None
    return payload
