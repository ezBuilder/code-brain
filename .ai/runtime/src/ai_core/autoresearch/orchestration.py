"""Multi-agent survey gate for breadth-first research (Stage 4, §7.1 — deterministic policy).

A pre-flight go/no-go for the orchestrator-worker pattern. The runtime does NOT run agents
or manage their lifecycle — that is the calling agent-harness's job (Cadence-derived
orchestration is *reused, not rebuilt*, per §7.1). This only returns a *recommendation* plus
the cost warning and a bounded worker list, so multi-agent fan-out is justified, not reflexive.

Policy (Anthropic multi-agent research + PRD §7.1):
- Default single-agent. Multi-agent ONLY when the task is breadth-first AND decomposes into
  mutually *independent* sub-tasks (caller asserts `independent=True`) AND there are enough of
  them (>= MIN_FANOUT). If it does not decompose, you pay the ~15x token multiplier without
  earning it — tightly-coupled work (coding/debugging) stays single-agent.
- Workers bounded to [1, HARD_MAX_WORKERS]; excess subtopics are deferred (sequential batch).

The runtime cannot verify independence (semantic judgement — the caller's job); it enforces
the *structural* guardrails: explicit assertion, minimum fan-out, hard worker cap, input
bounds, and an unskippable cost warning. No LLM, no network. stdlib only.
"""
from __future__ import annotations

MIN_FANOUT = 3            # below this, single agent wins (Anthropic lead spins up 3-5 subagents)
DEFAULT_MAX_WORKERS = 5   # Anthropic: 3-5 specialized subagents
HARD_MAX_WORKERS = 8      # bound runaway fan-out (PRD §7.1 example: 8 independent SSM variants)
_MAX_SUBTOPICS = 50       # bound input count
_MAX_LABEL = 500          # bound each returned subtopic label

COST_WARNING = (
    "멀티에이전트는 채팅 대비 ~15배 토큰(단일 에이전트 ~4배 대비 약 3.7~4배). "
    "독립적·폭-우선 하위작업으로 분해될 때만 정당화된다. "
    "코딩·디버깅 등 상호의존 작업에는 부적합 — 단일 에이전트 유지."
)


def _clean(subtopics) -> list:
    """Keep non-empty string subtopics, type-filtered and bounded (no untrusted execution)."""
    if not isinstance(subtopics, (list, tuple)):
        return []
    out = []
    for s in subtopics:
        if not isinstance(s, str):
            continue
        s = s.strip()
        if s:
            out.append(s[:_MAX_LABEL])
        if len(out) >= _MAX_SUBTOPICS:
            break
    return out


def survey_plan(subtopics, independent: bool = False, max_workers: int = DEFAULT_MAX_WORKERS) -> dict:
    """Recommend single- vs multi-agent fan-out for a breadth-first survey (suggestion only).

    Returns {mode, workers, deferred, n_subtopics, cost_warning, reason}.
    `mode == "multi"` only when `independent` is True AND len(subtopics) >= MIN_FANOUT.
    The calling agent-harness executes the plan; this only gates and bounds it.
    """
    topics = _clean(subtopics)
    n = len(topics)
    try:
        cap = int(max_workers)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_WORKERS
    cap = max(1, min(cap, HARD_MAX_WORKERS))

    if not independent or n < MIN_FANOUT:
        if not independent:
            reason = "독립성 미주장 — 공유 컨텍스트/상호의존 작업은 단일 에이전트가 더 싸고 깨끗하다(§7.1)."
        else:
            reason = f"하위작업 {n}개 < 최소 {MIN_FANOUT} — 폭-우선 이득 부족, 단일 에이전트 유지."
        return {
            "mode": "single",
            "workers": [],
            "deferred": [],
            "n_subtopics": n,
            "cost_warning": COST_WARNING,
            "reason": reason,
        }

    workers = topics[:cap]
    deferred = topics[cap:]
    reason = f"독립 폭-우선 하위작업 {n}개 → 오케스트레이터-워커 {len(workers)}개 권장(각 워커는 요약만 반환)."
    if deferred:
        reason += f" 초과 {len(deferred)}개는 순차 배치."
    return {
        "mode": "multi",
        "workers": workers,
        "deferred": deferred,
        "n_subtopics": n,
        "cost_warning": COST_WARNING,
        "reason": reason,
    }
