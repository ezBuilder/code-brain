from __future__ import annotations

from typing import Any

MODES = ("high_fidelity", "balanced", "aggressive")
PROTECTED_SIGNALS = ("handoff", "rubric", "verdict", "blockers")
DEFAULT_MODE = "balanced"
DEFAULT_MAX_BYTES = 4096
MODE_BYTE_MULTIPLIERS = {
    "high_fidelity": 2.0,
    "balanced": 1.0,
    "aggressive": 0.5,
}
MODE_RESULT_LIMITS = {
    "high_fidelity": None,
    "balanced": None,
    "aggressive": 3,
}


def normalize_mode(mode: str | None) -> str:
    normalized = (mode or DEFAULT_MODE).strip().lower().replace("-", "_")
    if normalized not in MODES:
        raise ValueError(f"invalid context budget mode: {mode}")
    return normalized


def policy(mode: str | None = None, *, base_max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    normalized = normalize_mode(mode)
    base = max(512, int(base_max_bytes or DEFAULT_MAX_BYTES))
    max_bytes = int(base * MODE_BYTE_MULTIPLIERS[normalized])
    return {
        "mode": normalized,
        "max_bytes": max(512, max_bytes),
        "max_results": MODE_RESULT_LIMITS[normalized],
        "protected_signals": list(PROTECTED_SIGNALS),
    }


def _has_protected_signal(item: dict[str, Any]) -> bool:
    haystack = f"{item.get('path', '')}\n{item.get('snippet', '')}".casefold()
    return any(signal in haystack for signal in PROTECTED_SIGNALS)


def _line(item: dict[str, Any]) -> str:
    return f"- {item['path']}: {item['snippet']}"


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit_lines(lines: list[tuple[str, bool]], max_bytes: int) -> tuple[list[str], bool, bool]:
    selected = [line for line, _protected in lines]
    if _bytes("\n".join(selected)) <= max_bytes:
        return selected, False, False
    kept = list(lines)
    dropped = False
    for idx in range(len(kept) - 1, -1, -1):
        if kept[idx][1]:
            continue
        kept.pop(idx)
        dropped = True
        selected = [line for line, _protected in kept]
        if _bytes("\n".join(selected)) <= max_bytes:
            return selected, dropped, False
    return [line for line, _protected in kept], dropped, True


def apply(
    results: list[dict[str, Any]],
    *,
    mode: str,
    limit: int,
    base_max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    mode = normalize_mode(mode)
    budget = policy(mode, base_max_bytes=base_max_bytes)
    requested_limit = max(1, int(limit))
    protected = [item for item in results[:requested_limit] if _has_protected_signal(item)]
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    max_results = budget["max_results"] or requested_limit
    for item in results[:requested_limit]:
        protected_item = _has_protected_signal(item)
        if len(selected) >= max_results and not protected_item:
            continue
        selected.append(item)
        selected_ids.add(id(item))
    for item in protected:
        if id(item) not in selected_ids:
            selected.append(item)
            selected_ids.add(id(item))
    selected.sort(key=lambda item: results.index(item))
    lines_with_flags = [(_line(item), _has_protected_signal(item)) for item in selected]
    lines, dropped_for_bytes, over_budget_to_preserve = _fit_lines(lines_with_flags, int(budget["max_bytes"]))
    additional = "\n".join(lines)
    line_set = set(lines)
    selected_results = [item for item in selected if _line(item) in line_set]
    return {
        "additionalContext": additional,
        "results": selected_results,
        "context_budget": {
            "mode": mode,
            "max_bytes": budget["max_bytes"],
            "max_results": budget["max_results"],
            "requested_limit": requested_limit,
            "selected_results": len(selected_results),
            "available_results": len(results),
            "protected_signals": list(PROTECTED_SIGNALS),
            "bytes": _bytes(additional),
            "truncated": len(selected_results) < min(requested_limit, len(results)) or dropped_for_bytes,
            "over_budget_to_preserve": over_budget_to_preserve,
        },
    }
