"""Per-agent best-model selection (PRD §6.1 model, §10.3 drift).

The orchestrator launches each agent with its strongest model. Defaults are conservative
(only flags known to be safe are passed; an agent whose default config is already the best
model gets no extra flag so launch never breaks on an unknown flag). Everything is overridable
in .ai/runtime/state/worker-models.json so the operator can pin models/flags per agent.

This module records the *resolved* model so the registry/orchestrator can see and score it.
stdlib only, fail-soft.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MODELS_PARTS = (".ai", "runtime", "state", "worker-models.json")

TIERS = ("cheap", "balanced", "best")
DEFAULT_TIER = "balanced"  # cost-aware default — NOT the most expensive model

# Per-agent model by tier. The orchestrator routes a task to the cheapest adequate tier; only
# complex/high-risk work gets the best (most expensive) model. codex keeps its CLI-configured
# model (flags empty) to avoid a wrong-flag launch failure; claude/agy switch via --model.
TIER_MODELS: dict[str, dict[str, dict[str, Any]]] = {
    "codex": {
        "cheap": {"model": "gpt-5.5", "reasoning": "low", "flags": []},
        "balanced": {"model": "gpt-5.5", "reasoning": "high", "flags": []},
        "best": {"model": "gpt-5.5", "reasoning": "xhigh", "flags": []},
    },
    "claude": {
        "cheap": {"model": "claude-haiku-4-5", "reasoning": "low", "flags": ["--model", "claude-haiku-4-5"]},
        "balanced": {"model": "claude-sonnet-4-6", "reasoning": "high", "flags": ["--model", "claude-sonnet-4-6"]},
        "best": {"model": "claude-opus-4-8", "reasoning": "high", "flags": ["--model", "claude-opus-4-8"]},
    },
    "agy": {
        "cheap": {"model": "Gemini 3.5 Flash (Medium)", "reasoning": "low",
                  "flags": ["--model", "Gemini 3.5 Flash (Medium)"]},
        "balanced": {"model": "Gemini 3.5 Flash (High)", "reasoning": "high",
                     "flags": ["--model", "Gemini 3.5 Flash (High)"]},
        "best": {"model": "Gemini 3.1 Pro (High)", "reasoning": "high",
                 "flags": ["--model", "Gemini 3.1 Pro (High)"]},
    },
}


# Opt-in autonomy flags per agent: skip interactive permission prompts so a warm worker can
# run the loop protocol unattended. The SAFETY BOUNDARY is loopd's dispatch approval-gate
# (high-risk/secret/destructive work is parked, never dispatched) — these flags only remove
# the per-command prompt for the low-risk work loopd already cleared. Off by default.
AUTONOMY_FLAGS: dict[str, list[str]] = {
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "claude": ["--permission-mode", "bypassPermissions"],
    "agy": ["--dangerously-skip-permissions"],
}


def autonomy_flags(agent: str) -> list[str]:
    return list(AUTONOMY_FLAGS.get(str(agent).strip().lower(), []))


def models_path(root: Path) -> Path:
    return root.joinpath(*MODELS_PARTS)


# Operator config (worker-models.json) may NOT smuggle permission-bypass flags through the model
# flags list — autonomy is reachable ONLY via the explicit --autonomous launch flag.
_FORBIDDEN_FLAG = ("bypass", "dangerous", "skip-permission", "permission-mode", "no-sandbox", "yolo")


def _sanitize_flags(flags: Any) -> list[str]:
    out: list[str] = []
    for f in (flags or []):
        s = str(f)
        if any(bad in s.lower() for bad in _FORBIDDEN_FLAG):
            continue
        out.append(s)
    return out


def _overrides(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(models_path(root).read_text(encoding="utf-8"))
        return data.get("agents", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def norm_tier(tier: str | None) -> str:
    t = str(tier or "").strip().lower()
    return t if t in TIERS else DEFAULT_TIER


def resolve_model(root: Path, agent: str, *, tier: str | None = None) -> dict[str, Any]:
    """The model spec to launch `agent` with at the given cost tier: {model, reasoning, flags, tier, source}.

    Operator override (worker-models.json) may pin a whole agent or a specific tier; absent that,
    the per-agent TIER_MODELS table is used. Default tier is cost-aware (balanced), not the best.
    """
    a = str(agent).strip().lower()
    t = norm_tier(tier)
    agent_tiers = TIER_MODELS.get(a, {})
    base = dict(agent_tiers.get(t) or {"model": "default", "reasoning": "high", "flags": []})
    over = _overrides(root).get(a)
    if isinstance(over, dict):
        # override can be a flat spec (applies to all tiers) or {tiers: {best: {...}}}
        tier_over = (over.get("tiers", {}) or {}).get(t) if isinstance(over.get("tiers"), dict) else None
        spec = tier_over if isinstance(tier_over, dict) else over
        base.update({k: spec[k] for k in ("model", "reasoning", "flags") if k in spec})
        base["source"] = "operator-override"
    else:
        base["source"] = "wrapper-default"
    base["tier"] = t
    base["flags"] = _sanitize_flags(base.get("flags"))  # never carry a bypass flag via config
    return base


def set_model(root: Path, *, agent: str, model: str, reasoning: str = "high",
              flags: list[str] | None = None) -> dict[str, Any]:
    a = str(agent).strip().lower()
    path = models_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    agents = data.get("agents", {}) if isinstance(data, dict) else {}
    agents[a] = {"model": str(model)[:64], "reasoning": str(reasoning)[:16],
                 "flags": [str(f)[:64] for f in (flags or [])][:8]}
    out = {"schema_version": 1, "agents": agents}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "agent": a, "model": agents[a]}


def list_models(root: Path) -> dict[str, Any]:
    return {"ok": True, "default_tier": DEFAULT_TIER,
            "agents": {a: {t: resolve_model(root, a, tier=t) for t in TIERS} for a in TIER_MODELS}}
