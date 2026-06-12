"""Autonomous per-project prompt growth (deterministic, non-blocking, no LLM on the path).

The agent's project prompt grows by itself: each turn-end the Stop hook records a
compact observation (no LLM, off the critical path), a deterministic evaluator turns
sustained violations into a learned rule, and a measured ratchet keeps the rule only
while real output tokens do not regress — otherwise it auto-rolls-back. The grown rules
live in ``.ai/memory/learned_prompt.md`` and are injected at SessionStart, so the prompt
improves across sessions. Per project; never touches global files.

Hard guarantees:
- Never runs in a PreToolUse / blocking path. Capture is append-only; growth runs in the
  Stop hook (post-turn) bounded by a cooldown.
- No human approval, no CLI to memorize: apply and rollback are automatic.
- No LLM and no network here. stdlib only. Fail-soft everywhere (never breaks a turn).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all

LOG_PARTS = (".ai", "memory", "prompt_growth.jsonl")
LEARNED_PARTS = (".ai", "memory", "learned_prompt.md")
STATE_PARTS = (".ai", "memory", "prompt_growth_state.json")
VERSIONS_PARTS = (".ai", "memory", "prompt_growth", "versions")

# --- tunables (deliberately conservative; growth is slow and reversible) ---
BREVITY_LIMIT = 600          # an agent report well over the ≤50-char / 1-line default rule
WINDOW = 40                  # observations considered per evaluation
MIN_SAMPLES = 20             # do not grow until enough signal
VIOLATION_RATE = 0.5         # >=50% of recent reports verbose → warrant a brevity rule
RATCHET_WINDOW = 30          # turns to measure a freshly applied rule before judging it
RATCHET_REGRESS = 1.10       # post-rule avg output > 110% of baseline → rollback
LEARNED_HEADER = "# Learned project rules (auto-grown by Code Brain; do not edit by hand)"


def log_path(root: Path) -> Path:
    return root.joinpath(*LOG_PARTS)


def learned_path(root: Path) -> Path:
    return root.joinpath(*LEARNED_PARTS)


def _state_path(root: Path) -> Path:
    return root.joinpath(*STATE_PARTS)


def _read_state(root: Path) -> dict[str, Any]:
    try:
        return json.loads(_state_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# --- 1. capture (called from Stop hook; append-only, never raises) ---

def record_turn(root: Path, *, output_chars: int, agent: str = "claude") -> None:
    """Append one compact, LLM-free observation. Off the critical path; fail-soft."""
    try:
        append_jsonl(log_path(root), {
            "ts": now_iso(),
            "agent": str(agent or "")[:32],
            "output_chars": int(output_chars or 0),
            "verbose": 1 if int(output_chars or 0) > BREVITY_LIMIT else 0,
        })
    except Exception:
        pass


def _recent(root: Path, n: int) -> list[dict[str, Any]]:
    try:
        return read_jsonl_all(log_path(root))[-n:]
    except Exception:
        return []


# --- 2. measurement (real tokens only, no estimates) ---

def _output_tokens(root: Path) -> int:
    try:
        from .obs import usage_report

        report = usage_report(root)
        actual = report.get("actual_token_usage", {}) if isinstance(report, dict) else {}
        total = 0
        for agent in ("claude", "codex"):
            block = actual.get(agent, {}) if isinstance(actual, dict) else {}
            tokens = block.get("tokens", {}) if isinstance(block, dict) else {}
            total += int(tokens.get("output_tokens", 0) or 0)
        return total
    except Exception:
        return 0


# --- 3. learned-prompt file (auto-applied, versioned, reversible) ---

def _active_rules(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rules = state.get("rules")
    return rules if isinstance(rules, dict) else {}


def _render_learned(root: Path, state: dict[str, Any]) -> None:
    rules = _active_rules(state)
    live = [r for r in rules.values() if r.get("status") == "active"]
    path = learned_path(root)
    if not live:
        # remove the file entirely when nothing is active (clean rollback)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    lines = [LEARNED_HEADER, ""]
    for rule in sorted(live, key=lambda r: str(r.get("applied_at", ""))):
        lines.append(f"- {rule['text']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _snapshot_version(root: Path, state: dict[str, Any], reason: str) -> None:
    try:
        vdir = root.joinpath(*VERSIONS_PARTS)
        vdir.mkdir(parents=True, exist_ok=True)
        stamp = now_iso().replace(":", "").replace("-", "")
        (vdir / f"{stamp}.json").write_text(
            json.dumps({"reason": reason, "state": state}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def learned_prompt_text(root: Path) -> str:
    """Text injected at SessionStart (empty when nothing has grown yet)."""
    try:
        return learned_path(root).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# --- 4. the deterministic growth + ratchet loop ---

def evaluate_and_grow(root: Path) -> dict[str, Any]:
    """One deterministic step: maybe apply a rule, maybe ratchet/rollback. Never raises."""
    try:
        return _evaluate_and_grow(root)
    except Exception as exc:  # growth must never break a turn
        return {"ok": False, "reason": str(exc)}


def _evaluate_and_grow(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    rules = dict(_active_rules(state))
    actions: list[str] = []

    # (a) ratchet rules that have collected enough post-apply samples
    obs = _recent(root, RATCHET_WINDOW)
    cur_tokens = _output_tokens(root)
    for rid, rule in list(rules.items()):
        if rule.get("status") != "active":
            continue
        baseline = rule.get("baseline_tokens")
        applied_turns = rule.get("applied_turns")
        turns_now = state.get("turns", 0)
        if baseline is None or applied_turns is None:
            continue
        if turns_now - applied_turns < RATCHET_WINDOW:
            continue
        # judge once: did real output tokens regress since applying this rule?
        if baseline > 0 and cur_tokens > baseline * RATCHET_REGRESS:
            rule["status"] = "regressed"
            rule["rolled_back_at"] = now_iso()
            actions.append(f"rollback:{rid}")
        else:
            rule["status"] = "kept"  # graduated: proven non-regressive, stays active-but-final
            rule["kept_at"] = now_iso()
            actions.append(f"keep:{rid}")
        rules[rid] = rule

    # (b) consider applying the brevity rule when sustained verbosity is observed
    window = _recent(root, WINDOW)
    if len(window) >= MIN_SAMPLES:
        rate = sum(int(o.get("verbose", 0)) for o in window) / len(window)
        rid = "brevity-boost"
        existing = rules.get(rid)
        already = existing and existing.get("status") in {"active", "kept"}
        regressed = existing and existing.get("status") == "regressed"
        if rate >= VIOLATION_RATE and not already and not regressed:
            rules[rid] = {
                "id": rid,
                "text": "보고는 핵심 1줄(≤50자)로 강제한다. 명시 요청이 없으면 해설·다이어그램·목록을 만들지 않는다.",
                "status": "active",
                "applied_at": now_iso(),
                "applied_turns": state.get("turns", 0),
                "baseline_tokens": cur_tokens,
                "violation_rate": round(rate, 3),
            }
            actions.append(f"apply:{rid}")

    if not actions:
        return {"ok": True, "actions": [], "active": len([r for r in rules.values() if r.get("status") in {"active", "kept"}])}

    state["rules"] = rules
    _write_state(root, state)
    _render_learned(root, state)
    _snapshot_version(root, state, reason=",".join(actions))
    append_audit(root, action="prompt_growth.step", category="prompt_growth",
                 payload={"actions": actions})
    return {"ok": True, "actions": actions,
            "active": len([r for r in rules.values() if r.get("status") in {"active", "kept"}])}


def tick(root: Path, *, output_chars: int, agent: str = "claude", cooldown: int = 5) -> dict[str, Any]:
    """Stop-hook entrypoint: record this turn, then grow at most every ``cooldown`` turns."""
    record_turn(root, output_chars=output_chars, agent=agent)
    state = _read_state(root)
    turns = int(state.get("turns", 0)) + 1
    state["turns"] = turns
    _write_state(root, state)
    if turns % max(1, cooldown) != 0:
        return {"ok": True, "grew": False, "turns": turns}
    result = evaluate_and_grow(root)
    result["turns"] = turns
    result["grew"] = bool(result.get("actions"))
    return result


def status(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    rules = _active_rules(state)
    return {
        "ok": True,
        "turns": int(state.get("turns", 0)),
        "rules": [{"id": r.get("id"), "status": r.get("status"), "text": r.get("text", "")[:80]}
                  for r in rules.values()],
        "learned_prompt": learned_prompt_text(root),
    }
