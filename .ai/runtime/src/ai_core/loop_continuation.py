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
  * Explicit context-pressure → no continuation. `stop_hook_active` is diagnostic rather than
    a bypass: unchanged evidence is bounded by the shared stall fingerprint and host cap.
  * Antigravity system/error/max-step/non-idle stops → no continuation; a normal model_stop
    is supported through that host's inverted `decision:"continue"` wire contract.
  * Bounded: no-progress fingerprint + per-request counter + wall-clock cap.

stdlib only; no LLM, no network. Pure decision + a tiny per-session counter sidecar in .ai/cache/.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .private_write import atomic_write_private_text, private_file_lock

MAX_CONTINUATIONS = 8       # Claude's documented consecutive Stop-block cap (2026-08-28)
MAX_WALL_SECONDS = 1800     # 30 min since the first continuation in a session
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")
LIMIT_NOTICE = (
    "Code Brain continuation safety cap reached (8 attempts or 30 minutes; "
    "scope=repository/worktree + host session). The turn was released for user review."
)


def _enabled() -> bool:
    return str(os.environ.get("AI_LOOP_CONTINUATION", "")).strip().lower() not in ("", "0", "false", "no")


def _counter_path(root: Path, sid: str) -> Path:
    safe = _SID_RE.sub("_", sid)[:64] or "default"
    return Path(root) / ".ai" / "cache" / "loop_continuation" / f"{safe}.json"


def _bump_counter(root: Path, sid: str, *, now: float) -> bool:
    """Increment the per-session counter; return True if still within both caps, else False."""
    path = _counter_path(root, sid)
    try:
        with private_file_lock(path.with_suffix(".lock"), root=Path(root)):
            state: dict[str, Any] = {}
            try:
                if path.exists():
                    state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                state = {}
            count = int(state.get("count", 0) or 0)
            first_ts = float(state.get("first_ts", now) or now)
            if count >= MAX_CONTINUATIONS or (now - first_ts) > MAX_WALL_SECONDS:
                state.update(
                    {
                        "count": count,
                        "first_ts": first_ts,
                        "last_ts": now,
                        "yield_notice": LIMIT_NOTICE,
                        "notice_emitted": False,
                    }
                )
                atomic_write_private_text(
                    path,
                    json.dumps(state, ensure_ascii=False, sort_keys=True),
                    root=Path(root),
                )
                return False
            new_state = {
                "count": count + 1,
                "first_ts": first_ts if count else now,
                "last_ts": now,
            }
            atomic_write_private_text(
                path,
                json.dumps(new_state, ensure_ascii=False, sort_keys=True),
                root=Path(root),
            )
            return True
    except (OSError, ValueError, TypeError):
        return False


def consume_limit_notice(root: Path, sid: str) -> str:
    """Return a cap-release notice once for this repository/worktree + host session key."""
    path = _counter_path(root, str(sid or "default"))
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        with private_file_lock(path.with_suffix(".lock"), root=Path(root)):
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("notice_emitted") is not False:
                return ""
            notice = str(state.get("yield_notice") or "")[:500]
            if not notice:
                return ""
            state["notice_emitted"] = True
            atomic_write_private_text(
                path,
                json.dumps(state, ensure_ascii=False, sort_keys=True),
                root=Path(root),
            )
            return notice
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return ""


def reset_counter(root: Path, sid: str) -> bool:
    """Reset the bounded continuation budget when a new user request starts."""
    path = _counter_path(root, str(sid or "default"))
    try:
        with private_file_lock(path.with_suffix(".lock"), root=Path(root)):
            path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


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
        if _has_context_pressure(payload):
            return None
        from .completion_guard import (
            _fingerprint,
            _requires_user_input,
            request_plan_signal,
            _stalled,
            _termination_allows_continuation,
        )
        if not _termination_allows_continuation(payload) or _requires_user_input(payload):
            return None
        sid = str(
            payload.get("session_id")
            or payload.get("sid")
            or payload.get("conversationId")
            or "default"
        )
        active = request_plan_signal(root, sid)
        if not active:
            return None
        signal = {
            "kind": "plan",
            "path": "",
            "plan_id": active.get("plan_id"),
            "detail": f"{active.get('completed')}/{active.get('total')}",
        }
        if _stalled(root, sid, _fingerprint(root, signal)):
            return None
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
