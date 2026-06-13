"""Capability-fit task classification (axis 1 of model routing).

Maps a loop request to a task CATEGORY, deterministically, from keyword/structure signals — NO LLM,
no network. Each category carries (preferred agent families best-fit-first, cold-start floor tier).
loopd routing uses this to pick the family whose strengths fit the task TYPE and the minimum-adequate
model tier: never always-best, never pure-cheapest. Orthogonal to difficulty — the per-task
``assess_tier`` may still raise the tier for an obviously hard instance (the floor is a lower bound).

Determinism: categories are evaluated in a fixed priority order and the FIRST match wins, so
overlapping signals (e.g. "implement a fix for the race condition") resolve to exactly one category.
"""
from __future__ import annotations

import re
from typing import Any

TIERS = ("cheap", "balanced", "best")
TIER_INDEX = {t: i for i, t in enumerate(TIERS)}

# Agent families and their characteristic strengths (used as the preferred-family ordering below):
#   claude — balanced coding, instruction-following, refactors, reviews
#   codex  — deep reasoning, algorithmic/debugging, long autonomous work
#   agy    — fast/cheap bulk throughput, large-context survey, research/summarize
FAMILIES = ("claude", "codex", "agy")

# Signals reused across categories. Compiled once; matched case-insensitively over goal+instruction+role.
_COMPLEX = re.compile(
    r"(architect|architecture|design\s+(a|the|an)\b|system\s*design|trade[\s-]?off|"
    r"race\s*condition|concurren|deadlock|lock\s*order|distributed|consensus|"
    r"근본\s*원인|아키텍처|설계|동시성|교착)",
    re.IGNORECASE,
)


def _matcher(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, re.IGNORECASE)


# Ordered category table — FIRST match wins (priority high→low). Each entry:
#   id, preferred_families (best-fit first), base_floor_tier, signal predicate.
# A predicate receives (text, checklist_len, text_len) and returns bool.
def _is_review(text: str, n_check: int, n_text: int) -> bool:
    return bool(_matcher(r"\b(review|audit|critique|verify|validate|lint|spot\s+bugs|"
                         r"find\s+bugs|security\s+review|리뷰|검수|감사|검증)\b").search(text)) and \
        not _matcher(r"\b(implement|add|build|create|fix|refactor|구현|추가|수정)\b").search(text)


def _is_reasoning(text: str, n_check: int, n_text: int) -> bool:
    return bool(_COMPLEX.search(text)) or bool(
        _matcher(r"\b(design|architect|plan|spec\s+out|propose\s+(an?\s+)?approach|"
                 r"evaluate|compare|decide\s+between|trade[\s-]?off|why\s+does|root[\s-]?cause|"
                 r"설계|계획|제안|평가|비교|결정|왜)\b").search(text))


def _is_debug(text: str, n_check: int, n_text: int) -> bool:
    return bool(_matcher(r"\b(fix|bug|debug|error|traceback|stack\s*trace|fails?|failing|broken|"
                         r"regression|crash|flaky|intermittent|off[\s-]?by[\s-]?one|exception|"
                         r"버그|디버그|오류|에러|실패|깨졌|회귀|크래시)\b").search(text))


def _is_refactor(text: str, n_check: int, n_text: int) -> bool:
    return bool(_matcher(r"\b(refactor|extract|inline|dedupe|de[\s-]?duplicate|restructure|"
                         r"migrat|rename\s+(symbol|across)|move\s+.*module|"
                         r"리팩터|추출|중복\s*제거|구조\s*변경|마이그레이)\b").search(text))


def _is_feature(text: str, n_check: int, n_text: int) -> bool:
    return bool(_matcher(r"\b(implement|add\s+(a|an|the|support)|build|create|feature|endpoint|"
                         r"component|new\s+(command|flag|module|option)|support\s+\w+|"
                         r"구현|기능|추가|엔드포인트|컴포넌트)\b").search(text))


def _is_research(text: str, n_check: int, n_text: int) -> bool:
    return bool(_matcher(r"\b(research|investigate|find\s+(all|out)|survey|gather|locate|"
                         r"where\s+is|how\s+does|look\s+up|explore\s+the\s+codebase|"
                         r"조사|탐색|찾아|어디(에|서)|어떻게\s+동작)\b").search(text))


def _is_docs(text: str, n_check: int, n_text: int) -> bool:
    return bool(_matcher(r"\b(document|explain|summari[sz]e|write[\s-]?up|readme|changelog|"
                         r"docstring|comment|annotate|문서|설명|요약|주석)\b").search(text))


def _is_trivial(text: str, n_check: int, n_text: int) -> bool:
    if n_text >= 200 or n_check > 1 or _COMPLEX.search(text):
        return False
    return bool(_matcher(r"\b(rename|typo|bump\s+version|format|reformat|sort\s+imports?|"
                         r"add\s+import|one[\s-]?line|tweak|whitespace|lint[\s-]?fix|"
                         r"오타|이름\s*변경|포맷|정렬)\b").search(text))


# (id, preferred_families, base_floor_tier, predicate) — evaluated top-to-bottom, first match wins.
# trivial_edit is checked FIRST: its guard is tight (short text, <=1 checklist item, no complexity),
# so a genuinely mechanical task ("fix typo") resolves to cheap before the broad "fix"/"add" verbs of
# debug/feature can over-classify it. _COMPLEX tasks are excluded from trivial, so a hard task still
# falls through to reasoning_design below.
_CATEGORIES: tuple[tuple[str, tuple[str, ...], str, Any], ...] = (
    ("trivial_edit", ("agy", "claude"), "cheap", _is_trivial),
    ("review_verify", ("claude", "codex"), "balanced", _is_review),
    ("reasoning_design", ("codex", "claude"), "best", _is_reasoning),
    ("debug_fix", ("codex", "claude"), "balanced", _is_debug),
    ("refactor", ("claude", "codex"), "balanced", _is_refactor),
    ("feature_impl", ("claude", "codex"), "balanced", _is_feature),
    ("research_search", ("agy", "codex"), "balanced", _is_research),
    ("docs_explain", ("agy", "claude"), "cheap", _is_docs),
)

# Deterministic fallthrough bucket so every request lands in exactly one category.
_FALLTHROUGH = ("standard", ("claude", "codex"), "balanced")

_BY_ID = {c[0]: c for c in _CATEGORIES}
_BY_ID[_FALLTHROUGH[0]] = (_FALLTHROUGH[0], _FALLTHROUGH[1], _FALLTHROUGH[2], None)

CATEGORY_IDS = tuple(c[0] for c in _CATEGORIES) + (_FALLTHROUGH[0],)


def _request_text(request: dict[str, Any]) -> str:
    return " ".join(str(request.get(k, "")) for k in ("goal", "instruction", "role"))


def classify(request: dict[str, Any]) -> str:
    """Return the task category id for this loop request (deterministic, first-match-wins)."""
    text = _request_text(request)
    n_text = len(text)
    checklist = request.get("checklist") if isinstance(request.get("checklist"), list) else []
    n_check = len(checklist)
    for cid, _fam, _floor, pred in _CATEGORIES:
        try:
            if pred(text, n_check, n_text):
                return cid
        except Exception:
            continue
    return _FALLTHROUGH[0]


def preferred_families(category: str) -> tuple[str, ...]:
    """Agent families that fit this category's task type, best-fit first."""
    entry = _BY_ID.get(category) or _BY_ID[_FALLTHROUGH[0]]
    return tuple(entry[1])


def base_floor_tier(category: str) -> str:
    """The cold-start (prior) minimum tier for this category."""
    entry = _BY_ID.get(category) or _BY_ID[_FALLTHROUGH[0]]
    return entry[2]


def base_floor_index(category: str) -> int:
    return TIER_INDEX[base_floor_tier(category)]


def route_summary(request: dict[str, Any]) -> dict[str, Any]:
    """Axis-1 result for a request: category + preferred families + base floor (no adaptive state)."""
    cat = classify(request)
    return {
        "category": cat,
        "preferred_families": list(preferred_families(cat)),
        "base_floor_tier": base_floor_tier(cat),
    }
