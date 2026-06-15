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

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all, rotate_jsonl_tail


_ALLOWED_RULE_SOURCES = frozenset({
    "prompt_growth.deterministic", "self-improve", "self-improve-judge", "cli",
})


def _sanitize_rule_text(text: str) -> str:
    """A learned rule is injected into the agent's context — strip prompt-injection vectors:
    collapse newlines (no closing the block / opening a fake section) and drop structural markdown."""
    import re

    one_line = " ".join(str(text or "").split())
    one_line = re.sub(r"(^|\s)([#>]|---+|```+|===+)", " ", one_line)  # headers / hr / fences / blockquote
    return one_line.replace("`", "").strip()[:400]


def _guard_self_write(text: str) -> dict[str, Any]:
    """Fail-CLOSED: if the guard errors, REFUSE the self-write (never inject an unvetted rule)."""
    try:
        from .self_write_guard import validate_self_write

        return validate_self_write(text)
    except Exception as exc:
        return {"ok": False, "violations": [{"invariant": "guard_error", "matched": str(exc)[:60]}]}

LOG_PARTS = (".ai", "memory", "prompt_growth.jsonl")
LEARNED_PARTS = (".ai", "memory", "learned_prompt.md")
STATE_PARTS = (".ai", "memory", "prompt_growth_state.json")
VERSIONS_PARTS = (".ai", "memory", "prompt_growth", "versions")

# --- tunables (deliberately conservative; growth is slow and reversible) ---
BREVITY_LIMIT = 600          # verbose enough to violate the terse default
WINDOW = 40                  # observations considered per evaluation
MIN_SAMPLES = 20             # do not grow until enough signal
VIOLATION_RATE = 0.5         # >=50% of recent reports verbose → warrant a brevity rule
RATCHET_WINDOW = 30          # turns to measure a freshly applied rule before judging it
RATCHET_REGRESS = 1.10       # post-rule avg output > 110% of baseline → rollback
PROMPT_GROWTH_MAX_BYTES = 512_000
PROMPT_GROWTH_KEEP = 2000
PROMPT_GROWTH_VERSION_KEEP = 30
LEARNED_HEADER = "# Learned project rules (auto-grown by Code Brain; do not edit by hand)"
BREVITY_RULE_TEXT = (
    "Self-initiated progress/output <=10 words. Answers to user questions concise by default. "
    "Match the user's language unless requested otherwise. "
    "Expand for explicit detail, severe error/risk, or required question. No next-step outro; keep working."
)


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


def rotate_logs(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "log": rotate_jsonl_tail(
            log_path(root),
            max_bytes=PROMPT_GROWTH_MAX_BYTES,
            keep_lines=PROMPT_GROWTH_KEEP,
            dry_run=dry_run,
        ),
        "versions": prune_versions(root, dry_run=dry_run),
    }


def prune_versions(root: Path, *, keep: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    vdir = root.joinpath(*VERSIONS_PARTS)
    if not vdir.is_dir():
        return {"ok": True, "pruned": [], "kept": 0, "dry_run": dry_run}
    versions = sorted(path for path in vdir.glob("*.json") if path.is_file())
    keep_count = max(0, int(PROMPT_GROWTH_VERSION_KEEP if keep is None else keep))
    remove = versions if keep_count == 0 else versions[:-keep_count]
    pruned: list[str] = []
    for path in remove:
        pruned.append(path.name)
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                pass
    return {"ok": True, "pruned": pruned, "kept": len(versions) - len(remove), "dry_run": dry_run}


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
        rotate_jsonl_tail(log_path(root), max_bytes=PROMPT_GROWTH_MAX_BYTES, keep_lines=PROMPT_GROWTH_KEEP)
    except Exception:
        pass


def _recent(root: Path, n: int) -> list[dict[str, Any]]:
    try:
        return read_jsonl_all(log_path(root))[-n:]
    except Exception:
        return []


def _recent_output_avg(root: Path, n: int) -> float:
    """Mean output_chars over the last n turn-observations — a direct, per-turn, non-cumulative
    signal for the ratchet (immune to the cumulative-token distortion/gaming)."""
    obs = _recent(root, max(1, int(n)))
    vals = [int(o.get("output_chars", 0) or 0) for o in obs if isinstance(o, dict)]
    return (sum(vals) / len(vals)) if vals else 0.0


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
    live = [r for r in rules.values() if r.get("status") in {"active", "kept"}]
    path = learned_path(root)
    if not live:
        # remove the file entirely when nothing should be injected (clean rollback)
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
        prune_versions(root)
    except Exception:
        pass


def learned_prompt_text(root: Path) -> str:
    """Text injected at SessionStart (empty when nothing has grown yet)."""
    try:
        return learned_path(root).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def apply_external_rule(root: Path, *, rule_id: str, text: str, source: str = "self-improve",
                        rationale: str = "") -> dict[str, Any]:
    """Apply a judge-proposed prompt rule under the SAME ratchet as deterministic growth.

    The closed self-improvement loop calls this: a cheap non-self judge proposes a generalized
    rule, it passes the M_core write-gate here, and it enters the prompt_growth state as an
    `active` rule with a measured token baseline — the existing ratchet then KEEPS it only if real
    output tokens do not regress, else rolls it back. Never blocks a turn; fully reversible.
    """
    rule_id = "".join(c for c in str(rule_id) if c.isalnum() or c in "-_")[:64] or "ext"
    source = str(source or "self-improve")[:64]
    if source not in _ALLOWED_RULE_SOURCES:
        return {"ok": False, "reason": "untrusted_source", "source": source}
    text = _sanitize_rule_text(text)  # strip newlines / markdown injection vectors
    if not text:
        return {"ok": False, "reason": "empty_rule"}
    verdict = _guard_self_write(text)
    if not verdict.get("ok", True):
        append_audit(root, action="prompt_growth.blocked", category="prompt_growth",
                     payload={"id": rule_id, "violations": verdict.get("violations", [])})
        return {"ok": False, "reason": "core_invariant_violation", "violations": verdict.get("violations", [])}
    try:
        state = _read_state(root)
        rules = dict(_active_rules(state))
        existing = rules.get(rule_id)
        if existing and existing.get("status") in {"active", "kept"}:
            return {"ok": True, "status": "already_active", "id": rule_id}
        if existing and existing.get("status") == "regressed":
            return {"ok": True, "status": "previously_regressed", "id": rule_id}
        # dedup by text too — do not re-add a rule the judge already proposed under another id
        for r in rules.values():
            if str(r.get("text", "")).strip() == text and r.get("status") in {"active", "kept", "regressed"}:
                return {"ok": True, "status": "duplicate_text", "id": r.get("id")}
        rules[rule_id] = {
            "id": rule_id,
            "text": text,
            "status": "active",
            "applied_at": now_iso(),
            "applied_turns": int(state.get("turns", 0)),
            "baseline_tokens": _output_tokens(root),
            "baseline_obs_avg": _recent_output_avg(root, RATCHET_WINDOW),
            "rationale": str(rationale or "")[:300],
            "source": str(source or "self-improve")[:64],
        }
        state["rules"] = rules
        _write_state(root, state)
        _render_learned(root, state)
        _snapshot_version(root, state, reason=f"apply:{rule_id}")
        append_audit(root, action="prompt_growth.external_apply", category="prompt_growth",
                     payload={"id": rule_id, "source": source})
        return {"ok": True, "status": "applied", "id": rule_id}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


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
        turns_now = state.get("turns", 0)
        applied_turns = rule.get("applied_turns")
        if applied_turns is None:
            continue
        window_turns = turns_now - applied_turns
        if window_turns < RATCHET_WINDOW:
            continue
        # Judge by output_chars averaged over a WINDOW (per-turn, non-cumulative → not gameable by
        # usage volume). pre = the window captured just before applying the rule; post = the window
        # since. A rule that genuinely worsened output (longer) over a fair window is rolled back.
        pre_avg = float(rule.get("baseline_obs_avg") or 0.0)
        post_avg = _recent_output_avg(root, min(window_turns, RATCHET_WINDOW))
        if pre_avg > 0 and post_avg > pre_avg * RATCHET_REGRESS:
            rule["status"] = "regressed"
            rule["rolled_back_at"] = now_iso()
            actions.append(f"rollback:{rid}")
        else:
            rule["status"] = "kept"  # graduated: proven non-regressive, stays active-but-final
            rule["kept_at"] = now_iso()
            actions.append(f"keep:{rid}")
        rules[rid] = rule

    # (b) consider applying or upgrading the brevity rule when sustained verbosity is observed
    rid = "brevity-boost"
    existing = rules.get(rid)
    if (
        existing
        and existing.get("status") in {"active", "kept"}
        and str(existing.get("text") or "") != BREVITY_RULE_TEXT
    ):
        existing["text"] = BREVITY_RULE_TEXT
        existing["updated_at"] = now_iso()
        rules[rid] = existing
        actions.append(f"update:{rid}")
    window = _recent(root, WINDOW)
    if len(window) >= MIN_SAMPLES:
        rate = sum(int(o.get("verbose", 0)) for o in window) / len(window)
        existing = rules.get(rid)
        already = existing and existing.get("status") in {"active", "kept"}
        regressed = existing and existing.get("status") == "regressed"
        if rate >= VIOLATION_RATE and not already and not regressed:
            rule_text = BREVITY_RULE_TEXT
            # ASI06 write-validation gate: a self-applied rule may never weaken a core invariant.
            verdict = _guard_self_write(rule_text)
            if not verdict.get("ok", True):
                append_audit(root, action="prompt_growth.blocked", category="prompt_growth",
                             payload={"id": rid, "violations": verdict.get("violations", [])})
                actions.append(f"blocked:{rid}")
            else:
                rules[rid] = {
                    "id": rid,
                    "text": rule_text,
                    "status": "active",
                    "applied_at": now_iso(),
                    "applied_turns": state.get("turns", 0),
                    "baseline_tokens": cur_tokens,
                    "baseline_obs_avg": _recent_output_avg(root, RATCHET_WINDOW),
                    "violation_rate": round(rate, 3),
                    "source": "prompt_growth.deterministic",
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
