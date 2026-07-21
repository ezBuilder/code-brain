from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.context_budget import (  # noqa: E402
    PROTECTED_SIGNALS,
    apply as apply_context_budget,
)


def _base_results() -> list[dict[str, str]]:
    return [
        {"path": "src/zeta.py", "snippet": "zeta body"},
        {"path": "src/alpha.py", "snippet": "alpha body"},
        {"path": "src/mid.py", "snippet": "mid body"},
    ]


def test_prefix_determinism_idempotence() -> None:
    results = _base_results()
    first = apply_context_budget(results, mode="balanced", limit=5)
    second = apply_context_budget(_base_results(), mode="balanced", limit=5)
    assert first["additionalContext"] == second["additionalContext"]
    # Byte-identical prefix across repeated calls on identical inputs.
    assert first["additionalContext"].encode("utf-8") == second["additionalContext"].encode("utf-8")


def test_order_invariance_to_incoming_relevance_rank() -> None:
    forward = _base_results()
    reversed_rank = list(reversed(_base_results()))
    out_forward = apply_context_budget(forward, mode="balanced", limit=5)
    out_reversed = apply_context_budget(reversed_rank, mode="balanced", limit=5)
    # Same item SET in different incoming order -> identical emitted bytes,
    # proving emission no longer keyed on volatile relevance rank.
    assert out_forward["additionalContext"] == out_reversed["additionalContext"]


def test_protected_signal_leads_prefix() -> None:
    results = [
        {"path": "src/alpha.py", "snippet": "alpha body"},
        {"path": "src/zeta.py", "snippet": "zeta body"},
        {"path": "docs/handoff.md", "snippet": "handoff notes"},
    ]
    out = apply_context_budget(results, mode="balanced", limit=5)
    lines = out["additionalContext"].splitlines()
    assert lines, "expected non-empty additionalContext"
    # Protected (handoff) line is the first emitted line, ahead of non-protected.
    assert "handoff" in lines[0].casefold()
    assert any(sig in lines[0].casefold() for sig in PROTECTED_SIGNALS)


def test_protected_never_dropped_under_tight_budget() -> None:
    results = [
        {"path": "src/alpha.py", "snippet": "a" * 400},
        {"path": "src/zeta.py", "snippet": "z" * 400},
        {"path": "docs/verdict.md", "snippet": "verdict: keep me"},
    ]
    out = apply_context_budget(results, mode="aggressive", limit=5, base_max_bytes=512)
    additional = out["additionalContext"]
    # Protected verdict line survives even when bytes are squeezed.
    assert "verdict" in additional.casefold()
    selected_paths = {item["path"] for item in out["results"]}
    assert "docs/verdict.md" in selected_paths
    budget = out["context_budget"]
    assert isinstance(budget["truncated"], bool)
    assert isinstance(budget["over_budget_to_preserve"], bool)


def test_canonical_lexical_order_of_non_protected() -> None:
    results = _base_results()
    out = apply_context_budget(results, mode="high_fidelity", limit=5)
    lines = out["additionalContext"].splitlines()
    # All non-protected here; emitted in lexical path order.
    assert lines == [
        "- src/alpha.py: alpha body",
        "- src/mid.py: mid body",
        "- src/zeta.py: zeta body",
    ]


def test_fail_soft_missing_keys() -> None:
    # Missing 'snippet' / 'path' must not raise; _has_protected_signal and the
    # canonical key both default via .get().
    results = [
        {"path": "src/no_snippet.py"},
        {"snippet": "orphan snippet"},
        {},
    ]
    out = apply_context_budget(results, mode="balanced", limit=5)
    assert isinstance(out["additionalContext"], str)
    # Determinism still holds for the degenerate shape.
    again = apply_context_budget(
        [{"path": "src/no_snippet.py"}, {"snippet": "orphan snippet"}, {}],
        mode="balanced",
        limit=5,
    )
    assert out["additionalContext"] == again["additionalContext"]


def test_requested_limit_caps_ordinary_results_even_below_mode_cap() -> None:
    out = apply_context_budget(_base_results(), mode="aggressive", limit=1)
    assert len(out["results"]) == 1
    assert out["context_budget"]["ordinary_result_cap"] == 1
