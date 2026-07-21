"""Bounded dense candidate retrieval over Code Brain's SQLite vector cache."""
from __future__ import annotations

import heapq
import os
import struct
import time
import sqlite3
from typing import Any


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def policy() -> dict[str, int]:
    """Runtime limits for exact vector scan before ANN support is available."""
    return {
        "max_rows": _bounded_env_int(
            "AI_SEARCH_DENSE_SCAN_MAX_ROWS", 10_000, minimum=1, maximum=250_000
        ),
        "max_ms": _bounded_env_int(
            "AI_SEARCH_DENSE_SCAN_MAX_MS", 500, minimum=10, maximum=10_000
        ),
        "max_candidates": _bounded_env_int(
            "AI_SEARCH_DENSE_MAX_CANDIDATES", 200, minimum=1, maximum=1_000
        ),
    }


def _vector(blob: object, dimension: int) -> tuple[float, ...] | None:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(blob)
    if len(raw) != dimension * 4:
        return None
    try:
        return struct.unpack(f"<{dimension}f", raw)
    except (struct.error, ValueError):
        return None


def collect(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    model_name: str,
    bm25_candidate_ids: list[int],
    top_k: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect independent dense candidates, degrading to a BM25 shortlist.

    Exact global scan is used only while compatible vectors are within the
    configured row/time bounds. Larger indexes retain bounded behavior by
    scoring only the lexical shortlist until an ANN backend is configured.
    Corrupt or model-incompatible vectors are skipped, never fatal.
    """
    limits = policy()
    dimension = len(query_vector)
    metadata: dict[str, Any] = {
        "scope": "none",
        "reason": "no_query_vector",
        "vector_rows": 0,
        "compatible_rows": 0,
        "scanned_rows": 0,
        "corrupt_vectors": 0,
        "partial": False,
        "policy": limits,
    }
    if dimension <= 0:
        return [], metadata

    aggregate = conn.execute(
        """
        select count(*) as vector_rows,
               sum(case when model_name = ? and vector_dim = ? then 1 else 0 end) as compatible_rows
        from embeddings_vec0
        where vector is not null
        """,
        (model_name, dimension),
    ).fetchone()
    vector_rows = int(aggregate["vector_rows"] or 0)
    compatible_rows = int(aggregate["compatible_rows"] or 0)
    metadata.update({"vector_rows": vector_rows, "compatible_rows": compatible_rows})
    if compatible_rows <= 0:
        metadata["reason"] = "no_compatible_vectors"
        return [], metadata

    global_scan = compatible_rows <= limits["max_rows"]
    params: list[Any] = [model_name, dimension]
    where = "e.vector is not null and e.model_name = ? and e.vector_dim = ?"
    if global_scan:
        metadata.update({"scope": "all_vectors", "reason": "complete"})
    else:
        candidate_ids = list(dict.fromkeys(int(item) for item in bm25_candidate_ids if int(item) > 0))
        if not candidate_ids:
            metadata.update(
                {
                    "scope": "none",
                    "reason": "global_scan_row_limit_no_lexical_candidates",
                    "partial": True,
                }
            )
            return [], metadata
        placeholders = ",".join("?" for _ in candidate_ids)
        where += f" and e.chunk_id in ({placeholders})"
        params.extend(candidate_ids)
        metadata.update(
            {
                "scope": "bm25_candidates",
                "reason": "global_scan_row_limit",
                "partial": True,
            }
        )

    cursor = conn.execute(
        f"""
        select c.id, c.path, c.sha256, c.summary,
               m.kind, m.source_path, m.start_line, m.end_line,
               p.processor,
               p.model_hash, p.prompt_version, p.chunker_version, p.confidence,
               e.vector
        from embeddings_vec0 e
        join chunks c on c.id = e.chunk_id
        join chunk_meta m on m.chunk_id = c.id
        left join provenance p on p.path = c.path
        where {where}
        order by e.chunk_id
        """,
        params,
    )
    wanted = min(max(1, int(top_k)), limits["max_candidates"])
    heap: list[tuple[float, int, dict[str, Any]]] = []
    started = time.monotonic()
    query_values = tuple(float(value) for value in query_vector)

    for row in cursor:
        if global_scan and metadata["scanned_rows"] and metadata["scanned_rows"] % 64 == 0:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms > limits["max_ms"]:
                metadata.update({"partial": True, "reason": "time_limit"})
                break
        metadata["scanned_rows"] += 1
        values = _vector(row["vector"], dimension)
        if values is None:
            metadata["corrupt_vectors"] += 1
            continue
        score = sum(left * right for left, right in zip(query_values, values))
        chunk_id = int(row["id"])
        item = {key: row[key] for key in row.keys() if key != "vector"}
        item["_dense_score"] = float(score)
        entry = (float(score), -chunk_id, item)
        if len(heap) < wanted:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)

    ranked = [entry[2] for entry in sorted(heap, key=lambda entry: (entry[0], entry[1]), reverse=True)]
    if not ranked and metadata["scanned_rows"] and metadata["corrupt_vectors"] == metadata["scanned_rows"]:
        metadata["reason"] = "no_valid_vectors"
    metadata["returned_candidates"] = len(ranked)
    return ranked, metadata


__all__ = ["collect", "policy"]
