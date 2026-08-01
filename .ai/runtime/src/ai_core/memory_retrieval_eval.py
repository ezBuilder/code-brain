"""Held-out evaluation for production memory recall (-005).

Memory analogue of code_retrieval_eval: golden qrels are ranked against the
real ``recall_memory`` pipeline (decisions + failures + lessons + procedures)
and scored with the shared deterministic ranking metrics. stdlib only, no
network, read-only against the store it is handed.

Determinism trap (research §4.4): ``recall_memory`` pins recency decay via its
``now=`` override, but its decision reader (``read_decisions_filtered``) has no
``now`` passthrough — expires_at liveness folds against the WALL CLOCK inside
``live_decision_records``. Fixtures must therefore place expiry bounds in the
far past/far future; a near-now bound would make the axis flaky.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory_recall import recall_memory
from .ranking_metrics import evaluate_ranked_retrieval

_STORE_FILES = ("decisions.jsonl", "lessons.jsonl", "procedural.jsonl")


def store_snapshot_sha256(root: Path) -> str:
    """Hash the durable stores so eval reports name the exact corpus."""
    digest = hashlib.sha256()
    base = root / ".ai" / "memory"
    for name in _STORE_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update((base / name).read_bytes())
        except OSError:
            digest.update(b"<absent>")
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate(
    root: Path,
    golden: list[dict[str, Any]],
    *,
    k: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate production ``recall_memory`` with record-id binary qrels."""
    bounded_k = max(1, int(k))

    def ranked_search(query_text: str, requested_k: int) -> list[str]:
        payload = recall_memory(root, query=query_text, limit=requested_k, now=now)
        ranked: list[str] = []
        seen: set[str] = set()
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            ranked.append(ref)
            if len(ranked) >= requested_k:
                break
        return ranked

    report = evaluate_ranked_retrieval(golden, ranked_search, k=bounded_k)
    report["store_sha256"] = store_snapshot_sha256(root)
    return report


__all__ = ["evaluate", "store_snapshot_sha256"]
