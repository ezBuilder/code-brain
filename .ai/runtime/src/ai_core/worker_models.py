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

# Best model per agent + the launch flags that select it.
# codex/agy: empty flags → use the CLI's configured default (already the strongest tier),
#            avoiding a wrong-flag launch failure. claude: --model is a known-safe flag.
DEFAULTS: dict[str, dict[str, Any]] = {
    "codex": {"model": "gpt-5.5", "reasoning": "xhigh", "flags": []},
    "claude": {"model": "claude-opus-4-8", "reasoning": "high", "flags": ["--model", "claude-opus-4-8"]},
    "agy": {"model": "Gemini 3.1 Pro (High)", "reasoning": "high",
            "flags": ["--model", "Gemini 3.1 Pro (High)"]},
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


def resolve_model(root: Path, agent: str) -> dict[str, Any]:
    """The model spec to launch `agent` with: {model, reasoning, flags, source}."""
    a = str(agent).strip().lower()
    base = dict(DEFAULTS.get(a, {"model": "default", "reasoning": "high", "flags": []}))
    over = _overrides(root).get(a)
    if isinstance(over, dict):
        base.update({k: over[k] for k in ("model", "reasoning", "flags") if k in over})
        base["source"] = "operator-override"
    else:
        base["source"] = "wrapper-default"
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
    return {"ok": True, "agents": {a: resolve_model(root, a) for a in DEFAULTS}}
