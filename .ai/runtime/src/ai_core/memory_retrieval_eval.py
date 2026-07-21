"""Deterministic qrel evaluation for production durable-memory recall."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory_recall import recall_memory
from .ranking_metrics import evaluate_ranked_retrieval


def memory_snapshot_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    memory_root = root / ".ai" / "memory"
    if not memory_root.exists():
        return digest.hexdigest()
    for path in sorted(memory_root.glob("*.jsonl"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate(
    root: Path,
    golden: list[dict[str, Any]],
    *,
    k: int = 5,
    now: datetime | None = None,
    types: list[str] | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}

    def ranked_search(query_text: str, requested_k: int) -> list[str]:
        payload = recall_memory(
            root,
            query=query_text,
            limit=requested_k,
            types=types,
            now=now,
        )
        diagnostics[query_text] = payload.get("scan") or {}
        return [
            str(item.get("ref"))
            for item in payload.get("items") or []
            if isinstance(item, dict) and item.get("ref")
        ]

    report = evaluate_ranked_retrieval(golden, ranked_search, k=k)
    report.update(
        {
            "memory_sha256": memory_snapshot_sha256(root),
            "query_diagnostics": diagnostics,
        }
    )
    return report


__all__ = ["evaluate", "memory_snapshot_sha256"]
