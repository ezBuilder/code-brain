"""PASTE-style speculative tool execution PoC.

Mines past PreToolUse tool-call trajectories from `.ai/memory/audit/<year>.jsonl`,
extracts 2-gram (preceding -> following) patterns above support/confidence
thresholds, predicts the next likely tool, and records hit/miss outcomes for
hit-rate analysis.

Standard-library only. No external deps. Tolerant of multiple audit-event
shapes (action/payload/hook variants). Read errors never raise — public
functions return ``{"ok": False, "reason": ...}`` instead.

Reference: PASTE (arXiv 2603.18897) — "Past-Aware Speculative Tool Execution"
showed -48.5% latency by mining bigram tool sequences with no model retraining.
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "mine_patterns",
    "predict_next",
    "record_speculation",
    "record_outcome",
    "hit_rate",
]

# ---------- paths ----------

def _audit_dir(root: Path) -> Path:
    return root / ".ai" / "memory" / "audit"


def _cache_log_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "speculative.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------- event parsing ----------

def _is_pretooluse(rec: dict[str, Any]) -> bool:
    """Heuristic: detect a PreToolUse-shaped event across multiple log dialects.

    Accept any of:
      * ``rec["hook"] == "PreToolUse"``
      * ``rec["kind"] == "PreToolUse"``
      * ``rec["action"] == "event.append"`` and ``payload["kind"] == "PreToolUse"``
      * ``payload["hook_event_name"] == "PreToolUse"``
    """
    if rec.get("hook") == "PreToolUse" or rec.get("kind") == "PreToolUse":
        return True
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return False
    if payload.get("kind") == "PreToolUse":
        return True
    if payload.get("hook_event_name") == "PreToolUse":
        return True
    if payload.get("hook") == "PreToolUse":
        return True
    return False


def _extract_tool_name(rec: dict[str, Any]) -> str | None:
    """Best-effort tool identifier extraction. Returns None when not present."""
    name = rec.get("tool_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    payload = rec.get("payload")
    if isinstance(payload, dict):
        name = payload.get("tool_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _extract_session_id(rec: dict[str, Any]) -> str | None:
    sid = rec.get("session_id") or rec.get("agent_session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    payload = rec.get("payload")
    if isinstance(payload, dict):
        sid = payload.get("session_id") or payload.get("agent_session_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    return None


def _iter_audit_records(root: Path) -> Iterator[dict[str, Any]]:
    """Stream audit JSONL files line-by-line. Skip malformed lines silently."""
    audit_dir = _audit_dir(root)
    if not audit_dir.is_dir():
        return
    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    yield rec


# ---------- mining ----------

def mine_patterns(
    root: Path,
    *,
    min_support: int = 3,
    min_confidence: float = 0.5,
    window: int = 1,
    limit: int = 100,
) -> dict:
    """Mine top tool-call bigrams from the audit stream.

    Returns ``{"ok": bool, "patterns": [...], "scanned_events": int}`` where
    each pattern is ``{"preceding": str, "following": str, "support": int,
    "confidence": float}``.

    Patterns are within-session whenever an event carries a ``session_id``.
    Events without session_id are joined into one synthetic stream so the
    function still produces signal on legacy logs.

    ``window`` is reserved for future N-gram support; current PoC always
    builds bigrams (window=1).
    """
    try:
        return _mine_patterns_inner(
            root,
            min_support=min_support,
            min_confidence=min_confidence,
            window=window,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — never raise from public API
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "patterns": [],
            "scanned_events": 0,
        }


def _mine_patterns_inner(
    root: Path,
    *,
    min_support: int,
    min_confidence: float,
    window: int,
    limit: int,
) -> dict:
    # group consecutive tool-name events by session
    per_session: dict[str, list[str]] = defaultdict(list)
    scanned = 0
    seen_pretool = 0

    for rec in _iter_audit_records(root):
        scanned += 1
        if not _is_pretooluse(rec):
            continue
        seen_pretool += 1
        tool = _extract_tool_name(rec)
        if not tool:
            continue
        sid = _extract_session_id(rec) or "__default__"
        per_session[sid].append(tool)

    # bigram counts and "preceding" totals (for confidence = P(b|a))
    bigrams: Counter[tuple[str, str]] = Counter()
    preceding_totals: Counter[str] = Counter()
    for seq in per_session.values():
        if len(seq) < 2:
            continue
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            bigrams[(a, b)] += 1
            preceding_totals[a] += 1

    patterns: list[dict[str, Any]] = []
    for (a, b), support in bigrams.items():
        total = preceding_totals[a]
        if total == 0:
            continue
        confidence = support / total
        if support < min_support:
            continue
        if confidence < min_confidence:
            continue
        patterns.append(
            {
                "preceding": a,
                "following": b,
                "support": support,
                "confidence": round(confidence, 6),
            }
        )

    # sort by confidence desc, then support desc, then alpha for stability
    patterns.sort(key=lambda p: (-p["confidence"], -p["support"], p["preceding"], p["following"]))
    if limit > 0:
        patterns = patterns[:limit]

    return {
        "ok": True,
        "patterns": patterns,
        "scanned_events": scanned,
        "pretooluse_events": seen_pretool,
    }


# ---------- prediction ----------

def predict_next(
    root: Path,
    current_tool: str,
    *,
    min_confidence: float = 0.5,
) -> dict:
    """Predict the most likely next tool after ``current_tool``.

    Returns ``{"ok": True, "prediction": {...}}`` or ``{"ok": True,
    "prediction": None}`` when no candidate clears ``min_confidence``.
    """
    if not isinstance(current_tool, str) or not current_tool.strip():
        return {"ok": False, "reason": "current_tool required", "prediction": None}

    mined = mine_patterns(root, min_support=1, min_confidence=min_confidence)
    if not mined.get("ok"):
        return {"ok": False, "reason": mined.get("reason", "mine failed"), "prediction": None}

    target = current_tool.strip()
    best: dict[str, Any] | None = None
    for pat in mined.get("patterns", []):
        if pat.get("preceding") != target:
            continue
        if best is None or pat["confidence"] > best["confidence"]:
            best = pat

    if best is None:
        return {"ok": True, "prediction": None}

    return {
        "ok": True,
        "prediction": {
            "following": best["following"],
            "confidence": best["confidence"],
            "support": best["support"],
        },
    }


# ---------- outcome logging ----------

def _append_speculation_line(root: Path, record: dict[str, Any]) -> None:
    path = _cache_log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # public API contract: never raise. Drop record on disk failure.
        return


def record_speculation(
    root: Path,
    *,
    exec_id: str,
    pattern: dict,
    predicted_tool: str,
) -> None:
    """Append a speculation start to the log. Never raises."""
    if not isinstance(exec_id, str) or not exec_id:
        return
    record = {
        "ts": _now_iso(),
        "monotonic_ns": time.monotonic_ns(),
        "exec_id": exec_id,
        "kind": "speculate",
        "pattern": pattern if isinstance(pattern, dict) else {},
        "predicted_tool": predicted_tool if isinstance(predicted_tool, str) else "",
    }
    _append_speculation_line(root, record)


def record_outcome(
    root: Path,
    *,
    exec_id: str,
    hit: bool,
    actual_tool: str,
) -> None:
    """Append a speculation outcome to the log. Never raises."""
    if not isinstance(exec_id, str) or not exec_id:
        return
    record = {
        "ts": _now_iso(),
        "monotonic_ns": time.monotonic_ns(),
        "exec_id": exec_id,
        "kind": "outcome",
        "outcome": "hit" if hit else "miss",
        "actual_tool": actual_tool if isinstance(actual_tool, str) else "",
    }
    _append_speculation_line(root, record)


# ---------- aggregation ----------

def hit_rate(root: Path) -> dict:
    """Compute hit/miss totals from the speculative cache log.

    Returns ``{"ok": True, "total": int, "hits": int, "hit_rate": float}``.
    On unreadable log, returns ``{"ok": False, "reason": ..., ...}`` with
    counts at 0. Missing log file is treated as zero outcomes (ok=True).
    """
    path = _cache_log_path(root)
    if not path.exists():
        return {"ok": True, "total": 0, "hits": 0, "hit_rate": 0.0}

    total = 0
    hits = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("kind") != "outcome":
                    continue
                total += 1
                if rec.get("outcome") == "hit":
                    hits += 1
    except OSError as exc:
        return {
            "ok": False,
            "reason": f"OSError: {exc}",
            "total": 0,
            "hits": 0,
            "hit_rate": 0.0,
        }

    rate = (hits / total) if total else 0.0
    return {"ok": True, "total": total, "hits": hits, "hit_rate": round(rate, 6)}
