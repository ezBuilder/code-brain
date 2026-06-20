"""Stop-hook plan-continuation driver (G3, OmO ultrawork-loop inspired) — opt-in, bounded.

OmO keeps a task moving past a model's premature "done" by re-prompting on the harness's idle/Stop
hook until the work is actually finished. CB externalizes the loop condition to the *parsed plan*
(G2 plan_state), never the model's self-assessment: while an active plan has unchecked steps, the
Stop hook re-injects a next-step directive (the host treats a Stop `decision:block` + reason as
"keep going").

Hard safety rails (CB philosophy, not OmO's token-burner default):
  * OFF by default — only runs when AI_LOOP_CONTINUATION is set.
  * NEVER overrides a security block (the caller only consults this when decision != block).
  * No active plan / no remaining steps  → no continuation.
  * stop_hook_active / explicit context-pressure → no continuation (avoid compaction & self-loops).
  * Antigravity → no continuation (it kills its Stop hook before work runs; structurally impossible).
  * Bounded: per-session continuation counter + wall-clock cap; exceeding either stops the loop.

stdlib only; no LLM, no network. Pure decision + a tiny per-session counter sidecar in .ai/cache/.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

MAX_CONTINUATIONS = 20      # per session; a hard backstop against runaway re-prompting
MAX_WALL_SECONDS = 1800     # 30 min since the first continuation in a session
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _enabled() -> bool:
    return str(os.environ.get("AI_LOOP_CONTINUATION", "")).strip() not in ("", "0", "false", "no")


def _counter_path(root: Path, sid: str) -> Path:
    safe = _SID_RE.sub("_", sid)[:64] or "default"
    return Path(root) / ".ai" / "cache" / "loop_continuation" / f"{safe}.json"


def _bump_counter(root: Path, sid: str, *, now: float) -> bool:
    """Increment the per-session counter; return True if still within both caps, else False."""
    path = _counter_path(root, sid)
    state: dict[str, Any] = {}
    try:
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    count = int(state.get("count", 0) or 0)
    first_ts = float(state.get("first_ts", now) or now)
    if count >= MAX_CONTINUATIONS or (now - first_ts) > MAX_WALL_SECONDS:
        return False
    new_state = {"count": count + 1, "first_ts": first_ts if count else now, "last_ts": now}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new_state), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False
    return True


def _has_context_pressure(payload: dict[str, Any]) -> bool:
    for key in ("context_pressure", "compact_pending", "near_compaction"):
        if payload.get(key):
            return True
    return False


def continuation_directive(payload: dict[str, Any], root: Path, *, now: float | None = None) -> str | None:
    """Return a next-step directive to keep the loop going, or None to let the turn end. Fail-soft.

    The caller (Stop hook) sets response decision=block + reason=<this> ONLY when not already
    blocking for security. Returns None whenever any safety rail trips.
    """
    try:
        if not _enabled():
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("stop_hook_active"):
            return None  # already inside a continuation cycle → never self-loop
        if _has_context_pressure(payload):
            return None
        agent = str(payload.get("agent") or payload.get("agent_type") or "").lower()
        if "antigravity" in agent or agent == "agy":
            return None  # Stop hook is killed before work runs on Antigravity
        from . import plan_state
        active = plan_state.active_summary(root)
        if not active or active.get("remaining", 0) <= 0:
            return None
        sid = str(payload.get("session_id") or payload.get("sid") or "default")
        if not _bump_counter(root, sid, now=now if now is not None else time.time()):
            return None
        nxt = active.get("next_label") or "the next unchecked step"
        return (
            f"Plan {active['plan_id']}: {active['completed']}/{active['total']} done, "
            f"{active['remaining']} left. Do NOT stop — continue with the next step: {nxt}. "
            f"Mark it with `.ai/bin/ai plan check --id {active['plan_id']} --match \"...\"` when done. "
            "Stop only when every step is checked or you hit a real, recorded blocker."
        )
    except Exception:
        return None
