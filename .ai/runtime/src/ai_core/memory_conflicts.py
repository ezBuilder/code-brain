"""Advisory conflict detector across durable decisions — deterministic, stdlib-only.

Code Brain already guards the highest-risk case at *write* time: loop_engineering._conflicting_
decisions flags a NEW durable rule that token-overlaps an existing one, and defers the semantic
call to the in-loop agent. What that misses is conflicts that already coexist in the corpus —
two decisions written far apart that now contradict. This module fills only that gap.

It is a candidate finder, not a judge: it flags PAIRS of live decisions with high token overlap
AND opposite polarity ("use X" vs "never use X") and writes them to conflicts.jsonl (resolved=
false) for a human/agent to review. It NEVER mutates decisions.jsonl — autonomous writes to
durable memory are Code Brain's highest-risk surface, so resolution stays manual.

No LLM, no network (the memanto inspiration ran this server-side; here it is a local heuristic
prefilter — the optional LLM judge half is deliberately out of scope for this pilot). Off by
default: page_out only runs it when AI_MEMORY_CONFLICT_SCAN is set; the CLI runs it on demand.
"""
from __future__ import annotations

import re
from typing import Any
from pathlib import Path

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all, decisions_path

# Mirror loop_engineering's tokenizer/stopwords so the two conflict paths agree on "same topic".
_STOPWORDS = frozenset(
    "the a an and or but if then else when while for with from into onto of in on at to by is are was "
    "were be been being do does did not no nor so than that this these those it its as use used using "
    "loop distill done dead lesson rule should must always never can will".split()
)
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{3,}|[가-힣]{2,}")

# Polarity markers: a decision carrying one of these negates an action the other asserts.
_NEGATION = frozenset(
    "never not no avoid dont stop remove disable drop deprecate forbid prohibit without".split()
) | {"don't", "doesn't", "won't", "shouldn't", "않", "마라", "말것", "금지", "안됨", "불가"}

_DEFAULT_THRESHOLD = 0.5  # share of the smaller token set that must overlap
_DEFAULT_SCAN = 800       # bound: only the most recent N live decisions are compared


def _significant_tokens(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(str(text).lower()) if tok not in _STOPWORDS}


def _polarity(text: str) -> bool:
    """True if the text carries a negation marker (Korean substrings + English words)."""
    low = str(text).lower()
    words = set(re.findall(r"[a-zA-Z']+", low))
    if words & _NEGATION:
        return True
    return any(marker in low for marker in ("않", "마라", "말것", "금지", "안됨", "불가"))


def conflicts_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "conflicts.jsonl"


def _live_decisions(root: Path, *, scan: int) -> list[dict[str, Any]]:
    """Folded, non-retired decisions+failures with text, most recent `scan` kept."""
    rows = read_jsonl_all(decisions_path(root))
    folded: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or f"_anon{len(order)}")
        if rid not in folded:
            order.append(rid)
        folded[rid] = rec
    live: list[dict[str, Any]] = []
    for rid in order:
        rec = folded[rid]
        if rec.get("kind") == "failure" and str(rec.get("status", "observed")) in {"stale", "refuted"}:
            continue
        if str(rec.get("decision", "")).strip():
            live.append(rec)
    return live[-max(1, int(scan)):]


def _existing_pairs(root: Path) -> set[frozenset[str]]:
    """Unresolved (a_id,b_id) pairs already recorded — so a rescan never re-flags them."""
    out: set[frozenset[str]] = set()
    for rec in read_jsonl_all(conflicts_path(root)):
        if isinstance(rec, dict) and not rec.get("resolved", False):
            a, b = str(rec.get("a_id") or ""), str(rec.get("b_id") or "")
            if a and b:
                out.add(frozenset((a, b)))
    return out


def scan_conflicts(
    root: Path,
    *,
    threshold: float = _DEFAULT_THRESHOLD,
    scan: int = _DEFAULT_SCAN,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Detect candidate conflicting decision pairs and append new ones to conflicts.jsonl.

    A pair conflicts when token overlap (vs the smaller token set) >= threshold AND the two
    rows differ in polarity. Advisory only: writes conflicts.jsonl, never touches decisions.
    Returns {"ok", "scanned", "candidates": [...], "written", "dry_run"}. Fail-soft.
    """
    try:
        live = _live_decisions(root, scan=scan)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "candidates": [], "written": 0}

    prepared = []
    for rec in live:
        toks = _significant_tokens(rec.get("decision", ""))
        if len(toks) >= 3:
            prepared.append((rec, toks, _polarity(rec.get("decision", ""))))

    already = _existing_pairs(root)
    candidates: list[dict[str, Any]] = []
    seen: set[frozenset[str]] = set()
    for i in range(len(prepared)):
        rec_a, tok_a, pol_a = prepared[i]
        for j in range(i + 1, len(prepared)):
            rec_b, tok_b, pol_b = prepared[j]
            if pol_a == pol_b:
                continue  # same polarity → agreement, not conflict
            overlap = len(tok_a & tok_b) / min(len(tok_a), len(tok_b))
            if overlap < threshold:
                continue
            a_id, b_id = str(rec_a.get("id") or ""), str(rec_b.get("id") or "")
            key = frozenset((a_id, b_id))
            if not a_id or not b_id or a_id == b_id or key in seen or key in already:
                continue
            seen.add(key)
            candidates.append({
                "a_id": a_id,
                "b_id": b_id,
                "overlap": round(overlap, 2),
                "a_text": str(rec_a.get("decision", ""))[:200],
                "b_text": str(rec_b.get("decision", ""))[:200],
            })

    candidates.sort(key=lambda c: c["overlap"], reverse=True)

    written = 0
    if not dry_run:
        for c in candidates:
            from .memory import _short_id
            append_jsonl(conflicts_path(root), {
                "id": _short_id("conf"),
                "ts": now_iso(),
                "resolved": False,
                **c,
            })
            written += 1
        if written:
            append_audit(root, action="memory.conflict_scan", category="memory",
                         payload={"written": written, "scanned": len(prepared)})

    return {
        "ok": True,
        "scanned": len(prepared),
        "candidates": candidates,
        "written": written,
        "dry_run": dry_run,
    }


def list_conflicts(root: Path, *, limit: int = 20, include_resolved: bool = False) -> dict[str, Any]:
    """List recorded conflict candidates (newest-first). Read-only."""
    rows = read_jsonl_all(conflicts_path(root))
    items = [r for r in rows if include_resolved or not r.get("resolved", False)]
    items = list(reversed(items))[: max(0, int(limit))]
    return {"ok": True, "count": len(items), "items": items}
