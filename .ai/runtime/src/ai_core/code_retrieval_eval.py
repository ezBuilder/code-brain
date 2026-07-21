"""Held-out style evaluation for the production Code Brain code retriever."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .ranking_metrics import evaluate_ranked_retrieval
from .search import query

_FUNCTION_CHUNK_RE = re.compile(
    r"^(.+\.(?:py|js|jsx|ts|tsx|go|rs)):{1,2}(.+)$",
    flags=re.IGNORECASE,
)


def canonical_result_path(value: object) -> str:
    """Collapse file/function chunks to the source file used by qrels."""
    path = str(value or "")
    matched = _FUNCTION_CHUNK_RE.match(path)
    return matched.group(1) if matched else path


def corpus_snapshot_sha256(root: Path) -> str:
    """Hash indexed source paths and bytes so eval reports name the corpus."""
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file() and ".ai/cache" not in item.as_posix()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate(root: Path, golden: list[dict[str, Any]], *, k: int = 5) -> dict[str, Any]:
    """Evaluate production ``search.query`` with file-level binary qrels."""
    bounded_k = max(1, int(k))
    retrieval_policies: dict[str, int] = {}

    def ranked_search(query_text: str, requested_k: int) -> list[str]:
        # Function chunks can duplicate their owning file. Pull a bounded wider
        # pool, collapse to source files, then apply the requested file-level K.
        payload = query(root, query_text, limit=max(requested_k * 4, 20))
        policy = str(payload.get("retrieval_policy") or "unknown")
        retrieval_policies[policy] = retrieval_policies.get(policy, 0) + 1
        ranked: list[str] = []
        seen: set[str] = set()
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            path = canonical_result_path(item.get("path"))
            if not path or path in seen:
                continue
            seen.add(path)
            ranked.append(path)
            if len(ranked) >= requested_k:
                break
        return ranked

    report = evaluate_ranked_retrieval(golden, ranked_search, k=bounded_k)
    report.update(
        {
            "corpus_sha256": corpus_snapshot_sha256(root),
            "retrieval_policy_counts": dict(sorted(retrieval_policies.items())),
        }
    )
    return report


__all__ = ["canonical_result_path", "corpus_snapshot_sha256", "evaluate"]
