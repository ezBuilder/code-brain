"""Query complexity router for model-tier selection (Stage 4, §7.2 — deterministic heuristic).

A cheap, rule-based RouteLLM-style suggestion: simple short lookups → local/cheap tier,
long / multi-hop / reasoning-heavy queries → frontier tier. The CALLING agent makes the
actual model choice; this only returns a suggestion + the signals behind it. Per PRD §7.2
the bulk of calls (80-90%) should route local, so only multi-signal queries escalate.
No LLM, no network. stdlib (re).
"""
from __future__ import annotations

import re

_REASONING = re.compile(
    r"(?i)\b(why|how|analy[sz]e|compare|contrast|evaluate|critique|design|prove|derive|"
    r"implication|reason|synthesi[sz]e|justify)\b"
)
_MULTIHOP = re.compile(
    r"(?i)\b(and then|after that|both|versus|vs\.?|relationship between|across|compared? to|"
    r"trade-?off)\b"
)
_CODE = re.compile(r"[{}();]|def |class |import |=>|function ")

_MAX_LEN = 20_000  # bound regex input


def classify(query: str) -> dict:
    """Return {complexity: low|medium|high, tier: local|frontier, signals, words}.

    Heuristic routing *suggestion* (agent may override). low/medium → local (cost: most
    calls stay cheap, §7.2), high (≥2 signals or very long) → frontier (quality).
    """
    q = (query or "")[:_MAX_LEN]
    words = len(q.split())
    signals = []
    if words >= 40:
        signals.append("long")
    if _REASONING.search(q):
        signals.append("reasoning")
    if _MULTIHOP.search(q):
        signals.append("multihop")
    if _CODE.search(q):
        signals.append("code")
    if q.count("?") >= 2:
        signals.append("multi_question")
    score = len(signals)
    if score >= 2 or words >= 60:
        complexity, tier = "high", "frontier"
    elif score == 1 or words >= 25:
        complexity, tier = "medium", "local"
    else:
        complexity, tier = "low", "local"
    return {"complexity": complexity, "tier": tier, "signals": signals, "words": words}
