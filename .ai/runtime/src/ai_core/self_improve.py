"""Closed-loop self-improvement — the pieces wired into one safe, non-blocking cycle.

The loop, end to end:
  1. enqueue_review() — deterministic, no LLM. Builds a self-review task from recent signals
     (prompt_growth observations + obs token usage) and submits it to the loopd pool at the
     CHEAP model tier. This is the trigger; it never calls an LLM and never blocks a turn.
  2. A cheap, NON-SELF worker claims the task (via loopd) and runs the cb-self-improve skill:
     it compares recent user intent vs agent output and, if it sees a repeated generalizable
     improvement, calls `ai selfimprove propose --text "<rule>"`.
  3. propose_rule() routes the proposed rule through the M_core write-gate (it may never weaken
     security/approval/redaction) and, if it passes, applies it under prompt_growth's measured
     RATCHET: the rule is kept only if real output tokens do not regress, else auto-rolled-back.
  4. The kept rule is injected at SessionStart via learned_prompt.md, so improvements compound.

Safety: the judge is a separate cheap model (not the running agent); nothing auto-applies without
the M_core gate; every change is versioned, audited, and reversible by the deterministic ratchet.
No LLM and no network in THIS module. stdlib only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import prompt_growth as pg
from .memory import append_audit, now_iso

_REVIEW_GOAL = "self-improve: review recent outputs and propose at most one prompt rule"


def _review_instruction(root: Path) -> str:
    """A self-contained task spec for the cheap judge worker (read from cheap, write via CLI)."""
    try:
        sig = pg.violation_signals(root)
    except Exception:
        sig = {}
    long_reports = sig.get("long_reports", 0) if isinstance(sig, dict) else 0
    return (
        "You are a CHEAP non-self prompt-improvement judge for this project. Do NOT edit code or "
        "files. Compare the user's recent commands against the agent's recent outputs (use "
        "`ai obs search` and `ai prompt-growth status`; recent signal: "
        f"{long_reports} over-long reports). If — and only if — you see a REPEATED, generalizable "
        "behavior the project prompt should enforce, propose exactly ONE short rule:\n"
        "  ai selfimprove propose --text \"<one generalized rule>\" --rationale \"<why, with evidence>\"\n"
        "Rules: the proposal must NOT weaken security/approval/redaction (it will be rejected if it "
        "does). It is auto-applied then ratcheted — kept only if it does not worsen real tokens, "
        "else rolled back. If nothing is clearly worth a rule, do nothing and report 'no change'. "
        "Then complete the loop request."
    )


def enqueue_review(root: Path, *, tier: str = "cheap", priority: str = "P3") -> dict[str, Any]:
    """Deterministically queue ONE self-review task for the cheap judge worker. No LLM here."""
    from . import loop_engineering as le

    pin = tier if tier in ("cheap", "balanced", "best") else "cheap"
    # tier is pinned atomically at submit (no read-modify-write of the queued file → no TOCTOU)
    res = le.submit(root, instruction=_review_instruction(root), goal=_REVIEW_GOAL,
                    role="self-improve-judge", reviewer_required=False, priority=priority,
                    dispatch={"model_tier": pin})
    if res.get("ok"):
        append_audit(root, action="selfimprove.enqueue", category="self_improve",
                     payload={"request_id": res["request"]["id"], "tier": pin})
    return res


def propose_rule(root: Path, *, text: str, rationale: str = "",
                 source: str = "self-improve-judge") -> dict[str, Any]:
    """The judge worker calls this. M_core-gated + ratcheted via prompt_growth. Never blocks."""
    import secrets

    rule_id = "si-" + secrets.token_hex(4)
    out = pg.apply_external_rule(root, rule_id=rule_id, text=text, source=source, rationale=rationale)
    append_audit(root, action="selfimprove.propose", category="self_improve",
                 payload={"id": rule_id, "ok": out.get("ok"), "status": out.get("status"),
                          "reason": out.get("reason")})
    return out


def status(root: Path) -> dict[str, Any]:
    base = pg.status(root)
    base["self_improve"] = {
        "closed_loop": "enqueue_review → cheap judge → propose_rule → M_core gate → ratchet",
        "checked_at": now_iso(),
    }
    # Surface the eval fitness coupling so `ai selfimprove status` shows whether the ratchet has a
    # correctness signal to gate on. Read-only and fail-soft: omit the block if eval_loop is missing.
    try:
        from . import eval_loop

        base["self_improve"]["eval"] = eval_loop.eval_fitness(root)
    except Exception:
        pass
    return base
