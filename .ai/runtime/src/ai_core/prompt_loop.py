"""Prompt self-improvement loop (deterministic substrate only).

The LLM judging — comparing a turn's user command vs the agent's output and proposing a
prompt patch — is done by the calling agent (a cheap non-self model, once per session via a
skill). This runtime owns only the deterministic parts: a pending-patch catalog (propose /
list / accept / reject, mirroring recommend.py's recommend->accept flow), lightweight
heuristic violation signals over recent events (so the judge has concrete targets), and
before/after output-token measurement from obs (no estimates). Patches NEVER auto-apply —
a human accepts. No LLM, no network here. stdlib only.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all

PATCHES_PARTS = (".ai", "memory", "prompt_patches.jsonl")
STATUSES = ("pending", "accepted", "rejected", "superseded")
TARGETS = ("global_claude", "global_codex", "project_agents")
MAX_RATIONALE = 2000
MAX_PATCH_BYTES = 8000
MAX_LIST = 50


def patches_path(root: Path) -> Path:
    return root.joinpath(*PATCHES_PARTS)


def _bounded(value: str, limit: int) -> str:
    return str(value or "").strip()[:limit]


def propose(
    root: Path,
    *,
    target: str,
    rationale: str,
    patch: str,
    violation: str = "",
    evidence: str = "",
    source: str = "prompt-loop",
) -> dict[str, Any]:
    """Record a pending prompt patch (the judge agent's proposal). Never auto-applies."""
    if target not in TARGETS:
        raise ValueError(f"invalid target: {target}")
    rationale = _bounded(rationale, MAX_RATIONALE)
    patch = _bounded(patch, MAX_PATCH_BYTES)
    if not rationale:
        raise ValueError("rationale is required")
    if not patch:
        raise ValueError("patch is required")
    record = {
        "id": "pp-" + secrets.token_hex(5),
        "created_at": now_iso(),
        "status": "pending",
        "target": target,
        "violation": _bounded(violation, 200),
        "rationale": rationale,
        "patch": patch,
        "evidence": _bounded(evidence, 1000),
        "source": _bounded(source, 64) or "prompt-loop",
    }
    append_jsonl(patches_path(root), record)
    append_audit(root, action="prompt_loop.propose", category="prompt_loop",
                 payload={"id": record["id"], "target": target})
    return {"ok": True, "patch": record}


def _latest_by_id(root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl_all(patches_path(root)):
        pid = rec.get("id")
        if isinstance(pid, str) and pid:
            latest[pid] = rec  # last write wins (status updates appended)
    return latest


def list_patches(root: Path, *, status: str | None = None) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    items = sorted(_latest_by_id(root).values(), key=lambda r: str(r.get("created_at", "")), reverse=True)
    if status is not None:
        items = [r for r in items if r.get("status") == status]
    return {"ok": True, "count": len(items[:MAX_LIST]), "patches": items[:MAX_LIST]}


def set_status(root: Path, *, patch_id: str, status: str, note: str = "") -> dict[str, Any]:
    if status not in {"accepted", "rejected", "superseded"}:
        raise ValueError("status must be accepted|rejected|superseded")
    current = _latest_by_id(root).get(patch_id)
    if current is None:
        return {"ok": False, "reason": "not_found", "id": patch_id}
    if current.get("status") != "pending":
        return {"ok": False, "reason": f"already_{current.get('status')}", "id": patch_id}
    updated = dict(current)
    updated.update({"status": status, "decided_at": now_iso(), "note": _bounded(note, 500)})
    append_jsonl(patches_path(root), updated)
    append_audit(root, action=f"prompt_loop.{status}", category="prompt_loop", payload={"id": patch_id})
    return {"ok": True, "patch": updated}


# --- deterministic violation signals (concrete targets for the judge) ---

_EVENTS_PARTS = (".ai", "memory", "events", "events.jsonl")
_LONG_REPORT_CHARS = 600   # an agent text report well over the ≤50-char rule
_SCAN = 400


def violation_signals(root: Path) -> dict[str, Any]:
    """Cheap heuristics over recent events: where output likely broke the brevity rule.

    Not a verdict — just counts/samples so the per-session judge has concrete material and
    so a baseline exists for the ratchet. The judge (LLM) decides if a patch is warranted.
    """
    events = read_jsonl_all(root.joinpath(*_EVENTS_PARTS))[-_SCAN:]
    long_reports = 0
    samples: list[str] = []
    for rec in events:
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        text = payload.get("text") or payload.get("message") or payload.get("response") or ""
        if isinstance(text, str) and len(text) > _LONG_REPORT_CHARS:
            long_reports += 1
            if len(samples) < 3:
                samples.append(text[:120])
    return {
        "ok": True,
        "scanned": len(events),
        "long_reports": long_reports,
        "samples": samples,
        "note": "heuristic only; the session judge decides whether to propose a patch",
    }


def measure_tokens(root: Path) -> dict[str, Any]:
    """Current measured output tokens (for the ratchet — real tokens only, no estimates)."""
    try:
        from .obs import usage_report

        report = usage_report(root)
        out: dict[str, Any] = {}
        actual = report.get("actual_token_usage", {}) if isinstance(report, dict) else {}
        for agent in ("claude", "codex"):
            block = actual.get(agent, {}) if isinstance(actual, dict) else {}
            tokens = block.get("tokens", {}) if isinstance(block, dict) else {}
            out[agent] = int(tokens.get("output_tokens", 0) or 0)
        return {"ok": True, "output_tokens": out}
    except Exception as exc:  # obs unavailable → no measurement, never raise
        return {"ok": False, "reason": str(exc)}
