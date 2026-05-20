from __future__ import annotations

from pathlib import Path
from typing import Any

from .redact import redact_value

CONTEXT_POLICY = {
    "engine": "code-brain-agent-runtime",
    "compression_threshold": 0.50,
    "gateway_hygiene_threshold": 0.85,
    "target_ratio": 0.20,
    "protect_last_n": 20,
    "tool_result_prune_chars": 200,
    "summary_min_tokens": 2000,
    "summary_max_tokens": 12000,
    "cache_breakpoints": "stable-system-plus-last-3",
}

RUNTIME_PATTERNS = [
    {
        "id": "closed_learning_loop",
        "label": "closed learning loop",
        "imported_as": ["eval_loop", "lessons", "recommendations"],
        "why": "Past outcomes become reusable lessons, skills, and routing rules instead of staying inside one chat transcript.",
    },
    {
        "id": "context_hygiene",
        "label": "context hygiene",
        "imported_as": ["session_resume", "memory_tier", "cb-exec output isolation"],
        "why": "Long work keeps a compact handoff, preserves hot facts, and avoids injecting large tool dumps.",
    },
    {
        "id": "graph_augmented_retrieval",
        "label": "graph augmented retrieval",
        "imported_as": ["retrieval_policy", "graph_context"],
        "why": "Code lookup can switch between BM25, symbol, and call-graph context instead of relying on text search only.",
    },
    {
        "id": "parallel_delegation",
        "label": "parallel delegation",
        "imported_as": ["agent recommendations", "worker ownership reports"],
        "why": "Independent workstreams can run with bounded context and return summaries, reducing parent context pressure.",
    },
    {
        "id": "release_readiness",
        "label": "release readiness",
        "imported_as": ["release_gate"],
        "why": "A read-only dashboard combines doctor, eval, git state, and generated recommendations before commercialization.",
    },
]


def context_policy() -> dict[str, Any]:
    return {"ok": True, "policy": CONTEXT_POLICY}


def insights(root: Path) -> dict[str, Any]:
    root = Path(root)
    eval_payload = _optional_call("eval_loop", "summarize_cases", root, latest_limit=3) or {"ok": True, "total": 0}
    lessons_payload = _optional_call("lessons", "lesson_summary", root) or {"ok": True, "total": 0}
    memory_pressure = _optional_call("memory_tier", "hot_pressure", root) or {"ok": True, "pressure": "unknown"}
    release_payload = _optional_call("release_gate", "summary", root) or {}
    recommendations = _recommendation_counts(root)

    applied = []
    for pattern in RUNTIME_PATTERNS:
        pid = pattern["id"]
        applied.append(
            {
                **pattern,
                "status": _pattern_status(
                    pid,
                    eval_payload=eval_payload,
                    lessons_payload=lessons_payload,
                    recommendations=recommendations,
                    release_payload=release_payload,
                ),
            }
        )

    reasons = [
        "Results improve when the model is wrapped in persistent memory, skills, tool routing, compression, and delegation.",
        "That can spend more tokens because it retrieves history, injects skill context, summarizes long sessions, and fans out workers.",
        "The payoff is fewer repeated mistakes and better continuity on long, operational tasks; the model alone is not the whole advantage.",
    ]
    payload = {
        "ok": True,
        "reasons": reasons,
        "context_policy": CONTEXT_POLICY,
        "signals": {
            "eval": eval_payload,
            "lessons": lessons_payload,
            "memory_pressure": memory_pressure,
            "recommendations": recommendations,
            "release_gate": {
                "ok": release_payload.get("ok"),
                "gates": release_payload.get("gates", {}),
            },
        },
        "applied_patterns": applied,
        "next_actions": _next_actions(applied),
    }
    return redact_value(payload)


def _optional_call(module_name: str, func_name: str, root: Path, **kwargs: Any) -> dict[str, Any] | None:
    try:
        module = __import__(f"ai_core.{module_name}", fromlist=[func_name])
        func = getattr(module, func_name)
        payload = func(root, **kwargs)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _recommendation_counts(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    loaders = {
        "skills": ("recommend", "list_visible"),
        "agents": ("agent_recommend", "list_visible"),
        "precall": ("precall_recommend", "list_visible"),
    }
    for key, (module_name, func_name) in loaders.items():
        try:
            module = __import__(f"ai_core.{module_name}", fromlist=[func_name])
            rows = getattr(module, func_name)(root)
        except Exception:
            rows = []
        statuses: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        out[key] = {"total": len(rows), "by_status": dict(sorted(statuses.items()))}
    return out


def _pattern_status(
    pattern_id: str,
    *,
    eval_payload: dict[str, Any],
    lessons_payload: dict[str, Any],
    recommendations: dict[str, Any],
    release_payload: dict[str, Any],
) -> str:
    if pattern_id == "closed_learning_loop":
        if int(eval_payload.get("total") or 0) or int(lessons_payload.get("total") or 0):
            return "active"
        return "wired"
    if pattern_id == "context_hygiene":
        return "active"
    if pattern_id == "graph_augmented_retrieval":
        return "active"
    if pattern_id == "parallel_delegation":
        agents = recommendations.get("agents", {})
        if int(agents.get("total") or 0):
            return "active"
        return "wired"
    if pattern_id == "release_readiness":
        return "active" if release_payload else "wired"
    return "unknown"


def _next_actions(applied: list[dict[str, Any]]) -> list[str]:
    actions = []
    if any(item["id"] == "closed_learning_loop" and item["status"] == "wired" for item in applied):
        actions.append("Record real pass/fail cases with `ai eval record` after each substantial loop.")
    actions.append("Use `ai runtime insights --json` before long commercialization loops to check whether learning signals are accumulating.")
    actions.append("Keep `release-gate summary` dirty-aware; do not treat a dirty worktree as release-ready.")
    return actions
