"""Classify a settled-loop failure reason as transient (retryable) vs fatal. Pure, offline.

Design borrowed from OmO's runtime-fallback-error-classifier: a NARROW transient set
(rate-limit / quota / overload / 429 / 503 / 529 / too-many-requests, incl. CJK variants) that
warrants a per-task model/agent fallback re-queue. This is deliberately narrower than
route_floor._TASK_FAULT, which also matches TERMINAL faults (blocked / denied / ambiguous /
missing-repro) that must NOT be retried — retrying those just burns the attempt budget.

stdlib only; no LLM, no network. Used by loop_engineering._finish to decide re-queue vs
dead-letter, and re-usable by select_worker/worker_registry for quota-aware deranking.
"""
from __future__ import annotations

import re

# Retryable: a different model/agent (or the same one a moment later) may succeed.
_TRANSIENT = re.compile(
    r"(rate.?limit|too\s*many\s*requests|quota|over[\s-]?load(ed)?|capacity|throttl|"
    r"\b429\b|\b503\b|\b529\b|service\s+unavailable|temporarily\s+unavailable|"
    r"레이트\s*리밋|요청\s*한도|쿼터|할당량|과부하|일시적|사용\s*량\s*초과|"
    r"レート制限|クォータ|過負荷|一時的)",
    re.IGNORECASE,
)


def is_transient_fault(reason: str) -> bool:
    """True when a loop failure reason looks transient (retry on a different worker/tier)."""
    return bool(_TRANSIENT.search(str(reason or "")))
