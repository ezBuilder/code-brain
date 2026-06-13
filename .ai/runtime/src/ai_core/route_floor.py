"""Outcome-adaptive minimum-tier floor per task category (axis 2 of model routing).

The learned, per-category model-tier FLOOR (a lower bound, never a forced tier). It moves only on
real, corroborated loop outcomes and is reusable like the prompt_growth ratchet:

  * win   — status=done, clean first pass (attempts<=1), AND corroborated by an INDEPENDENT signal
             (a reviewer verdict pass: a self-reported complete alone is never a win → anti-gaming).
  * loss  — status=dead AND model-attributed (not an env/infra/ambiguity/blocked fault).
  * neutral — everything else (unverified done, retry-success, env failure, blocked) → audited,
             but excluded from both the rate and the sample count.

Adaptation is ASYMMETRIC and bounded [cheap..best]:
  * escalate +1 fast — UP_LOSSES consecutive model-attributed losses (safety > cost, no cooldown).
  * de-escalate -1 slow but reachable — DOWN_WINS consecutive corroborated clean wins, with a
    minimum sample size, Beta-smoothed success >= threshold, and an event-count cooldown.

Determinism: the decision is a pure function of the per-category counters; "time" is an event
sequence counter maintained in state, never a wall clock or RNG (mirrors prompt_growth's discipline).

Memory-poisoning safety (ASI06): the ONLY thing this module ever writes is a bounded integer floor
in [0,2] per known category. It can never touch security/approval/redaction. The clamp + category
allowlist below are the structural guard (the free-text M_core gate does not apply — there is no
free text here). stdlib only; no LLM, no network.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import task_router as tr
from .memory import append_audit

# Tunable knobs (named so behaviour is auditable from one place). Asymmetry encodes "fast up, slow
# but reachable down" — calibrated for a solo light user: 6 corroborated clean wins is attainable,
# 2 attributed losses escalates immediately.
UP_LOSSES = 2          # consecutive model-attributed losses → escalate one tier
DOWN_WINS = 6          # consecutive corroborated clean wins → eligible to de-escalate one tier
MIN_N = 6              # minimum (wins+losses) in the window before de-escalation may fire
COOLDOWN_EVENTS = 6    # events since last floor change before another de-escalation may fire
SMOOTH_A = 1           # Beta prior alpha (resists premature confidence on tiny samples)
SMOOTH_B = 1           # Beta prior beta
DOWN_SMOOTHED_MIN = 0.85  # Beta-smoothed success floor required to de-escalate

_MAX_TIER = len(tr.TIERS) - 1

# A loop 'dead' is a TASK/ENV fault (NOT the model's inadequacy) when the reason matches these — such
# failures never escalate the floor (prevents over-provisioning on impossible/flaky/ambiguous tasks).
_TASK_FAULT = re.compile(
    r"(timeout|timed?\s*out|enoent|no such file|network|rate.?limit|\b429\b|\b503\b|connection\s+refused|"
    r"lease\s+expired|disk|oom|out of memory|killed|tmux|pane|sandbox|denied|permission|"
    r"ambiguous|underspecified|unclear|cannot\s+reproduce|missing\s+repro|needs?[\s-]clarification|"
    r"blocked|parked|approval|모호|불명확|재현\s*불가|차단|승인|환경|네트워크|타임아웃)",
    re.IGNORECASE,
)


def _state_path(root: Path) -> Path:
    return Path(root) / ".ai" / "runtime" / "state" / "route-floors.json"


def _outcomes_path(root: Path) -> Path:
    return Path(root) / ".ai" / "memory" / "route_outcomes.jsonl"


def _load_state(root: Path) -> dict[str, Any]:
    p = _state_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # fail-soft: a corrupt state file must never break dispatch


def _write_state(root: Path, state: dict[str, Any]) -> None:
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _new_cat_state(category: str) -> dict[str, Any]:
    base = tr.base_floor_index(category)
    return {"floor": base, "base": base, "wins": 0, "losses": 0, "neutral": 0,
            "consec_wins": 0, "consec_losses": 0, "last_change_seq": 0, "total_seq": 0}


def classify_outcome(*, status: str, attempts: int, reviewer_required: bool,
                     verdict_pass: bool, reason: str = "") -> str:
    """Map a settled loop outcome to win|loss|neutral (deterministic, no LLM)."""
    status = str(status or "").lower()
    attempts = int(attempts or 0)
    corroborated = bool(reviewer_required) and bool(verdict_pass)  # independent signal only
    if status == "done" and attempts <= 1 and corroborated:
        return "win"
    if status == "dead" and not _TASK_FAULT.search(str(reason or "")):
        return "loss"  # model-attributed failure (env/ambiguity/blocked already excluded)
    return "neutral"   # unverified done, retry-success, or task/env fault


def route_decide(cat_state: dict[str, Any]) -> tuple[int, str | None]:
    """Pure decision over one category's counters → (new_floor, action|None). No clock, no RNG."""
    floor = int(cat_state.get("floor", 0))
    wins = int(cat_state.get("wins", 0))
    losses = int(cat_state.get("losses", 0))
    consec_wins = int(cat_state.get("consec_wins", 0))
    consec_losses = int(cat_state.get("consec_losses", 0))
    total_seq = int(cat_state.get("total_seq", 0))
    last_change = int(cat_state.get("last_change_seq", 0))

    if consec_losses >= UP_LOSSES and floor < _MAX_TIER:
        return floor + 1, "escalate"

    if floor > 0 and consec_wins >= DOWN_WINS:
        n = wins + losses
        smoothed = (wins + SMOOTH_A) / (n + SMOOTH_A + SMOOTH_B) if n >= 0 else 0.0
        if n >= MIN_N and smoothed >= DOWN_SMOOTHED_MIN and (total_seq - last_change) >= COOLDOWN_EVENTS:
            return floor - 1, "deescalate"

    return floor, None


def effective_floor(root: Path, category: str) -> int:
    """The current learned floor index for a category (cold-starts at the static prior). Bounded."""
    if category not in tr.CATEGORY_IDS:
        category = "standard"
    st = _load_state(root).get(category)
    floor = int(st.get("floor", tr.base_floor_index(category))) if isinstance(st, dict) else tr.base_floor_index(category)
    return max(0, min(_MAX_TIER, floor))


def effective_floor_tier(root: Path, category: str) -> str:
    return tr.TIERS[effective_floor(root, category)]


def record_outcome(root: Path, *, category: str, tier: str, status: str, attempts: int,
                   reviewer_required: bool, verdict_pass: bool, reason: str = "") -> dict[str, Any]:
    """Record one settled loop outcome and adapt the category floor. Fail-soft (never raises)."""
    try:
        if category not in tr.CATEGORY_IDS:
            category = "standard"
        outcome = classify_outcome(status=status, attempts=attempts,
                                   reviewer_required=reviewer_required, verdict_pass=verdict_pass,
                                   reason=reason)
        state = _load_state(root)
        st = state.get(category)
        if not isinstance(st, dict):
            st = _new_cat_state(category)
        st["total_seq"] = int(st.get("total_seq", 0)) + 1

        if outcome == "win":
            st["wins"] = int(st.get("wins", 0)) + 1
            st["consec_wins"] = int(st.get("consec_wins", 0)) + 1
            st["consec_losses"] = 0
        elif outcome == "loss":
            st["losses"] = int(st.get("losses", 0)) + 1
            st["consec_losses"] = int(st.get("consec_losses", 0)) + 1
            st["consec_wins"] = 0
        else:
            st["neutral"] = int(st.get("neutral", 0)) + 1
            # neutral breaks a clean-win streak (a retry-success / unverified run is not a clean win)
            st["consec_wins"] = 0

        # append the raw outcome line (audit trail / future analysis)
        _append_outcome_line(root, {"category": category, "tier": str(tier), "status": str(status),
                                    "outcome": outcome, "attempts": int(attempts or 0),
                                    "seq": st["total_seq"]})

        new_floor, action = route_decide(st)
        moved = False
        if action and new_floor != int(st.get("floor", 0)):
            from_floor = int(st["floor"])
            st["floor"] = max(0, min(_MAX_TIER, new_floor))  # hard clamp — never outside [cheap..best]
            st["last_change_seq"] = st["total_seq"]
            # reset the window so the new floor must re-prove itself before moving again
            st["wins"] = st["losses"] = st["neutral"] = 0
            st["consec_wins"] = st["consec_losses"] = 0
            moved = True
            append_audit(root, action=f"route.floor.{action}", category="route",
                         payload={"task_category": category, "from_tier": tr.TIERS[from_floor],
                                  "to_tier": tr.TIERS[st["floor"]], "at_seq": st["total_seq"]})

        state[category] = st
        _write_state(root, state)
        return {"ok": True, "category": category, "outcome": outcome,
                "floor_tier": tr.TIERS[int(st["floor"])], "moved": moved}
    except Exception as exc:  # bookkeeping must never break the loop finish
        return {"ok": False, "error": str(exc)[:200]}


def _append_outcome_line(root: Path, row: dict[str, Any]) -> None:
    p = _outcomes_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def status(root: Path) -> dict[str, Any]:
    """Per-category current floors (for `ai loopd` / obs introspection)."""
    state = _load_state(root)
    out = {}
    for cid in tr.CATEGORY_IDS:
        st = state.get(cid)
        floor = int(st["floor"]) if isinstance(st, dict) and "floor" in st else tr.base_floor_index(cid)
        out[cid] = {"floor_tier": tr.TIERS[max(0, min(_MAX_TIER, floor))],
                    "base_tier": tr.base_floor_tier(cid),
                    "preferred_families": list(tr.preferred_families(cid)),
                    "wins": int(st.get("wins", 0)) if isinstance(st, dict) else 0,
                    "losses": int(st.get("losses", 0)) if isinstance(st, dict) else 0}
    return {"ok": True, "categories": out}
