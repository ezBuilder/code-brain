"""Injection-budget composition: directives must survive, evidence degrades gracefully.

The silent bug this pins down: build_context used to truncate the JOINED string from the
tail, so once the earlier sections filled MAX_INJECTION_BYTES every later section vanished.
Measured before the fix — code-brain discarded 57% of its composed context, navio 76%, and
on every project the auto-grown `learned_prompt` rules and the `session tail` were cut off
entirely. prompt_growth was therefore writing brevity rules that never reached the model,
which is why its measured effect looked like noise.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_core import hooks  # noqa: E402
from ai_core.prompt_growth import LEARNED_HEADER  # noqa: E402

LEARNED = LEARNED_HEADER + "\n\n- keep answers short."


def test_protected_prefix_tracks_prompt_growth_header() -> None:
    """The header is inlined in hooks to stay import-cycle free; catch any drift."""
    assert LEARNED_HEADER in hooks._PROTECTED_SECTION_PREFIXES


def test_short_context_is_untouched() -> None:
    out = hooks._fit_sections(["Response: be terse.", "decisions:\n- a"], 2048)
    assert out == "Response: be terse.\n\ndecisions:\n- a"


def test_never_exceeds_budget() -> None:
    sections = ["Response: r", "decisions:\n" + ("x" * 5000), LEARNED]
    out = hooks._fit_sections(sections, 512)
    assert len(out.encode("utf-8")) <= 512


def test_learned_rules_survive_a_huge_earlier_section() -> None:
    """The regression: a 2208B session tail used to evict the learned rules entirely."""
    sections = [
        "Response: be terse.",
        "decisions:\n" + ("d" * 900),
        "session tail:\n" + ("s" * 2200),
        LEARNED,
    ]
    out = hooks._fit_sections(sections, 2048)
    assert LEARNED_HEADER in out
    assert "keep answers short." in out
    assert len(out.encode("utf-8")) <= 2048


def test_all_directive_sections_survive_together() -> None:
    sections = [
        "Code Brain fast_path: hook=UserPromptSubmit",
        "Response: be terse.",
        "Search: use code_query.",
        "Read: use hashline.",
        "cb-turn: prev turn 9new; summarize.",
        "cb-stale: memory behind git.",
        "session tail:\n" + ("s" * 4000),
        LEARNED,
    ]
    out = hooks._fit_sections(sections, 2048)
    for prefix in ("Code Brain fast_path:", "Response:", "Search:", "Read:", "cb-turn:", "cb-stale:"):
        assert prefix in out, prefix
    assert LEARNED_HEADER in out


def test_evidence_is_clipped_not_dropped_wholesale() -> None:
    """An oversized evidence section should still contribute its head, not disappear."""
    sections = ["Response: r", "decisions:\n" + "\n".join(f"- item {i}" for i in range(400))]
    out = hooks._fit_sections(sections, 600)
    assert "decisions:" in out
    assert "- item 0" in out
    assert out.endswith("...")


def test_clip_prefers_a_line_boundary() -> None:
    section = "decisions:\n- alpha\n- beta\n- gamma\n- delta"
    out = hooks._clip_section(section, 30)
    assert out.endswith("...")
    assert "\n- alpha" in out
    # must not end mid-entry
    assert not out.replace("...", "").rstrip().endswith("- gam")


def test_pathological_protected_set_does_not_starve_budget() -> None:
    """If the protected sections alone exceed the budget, fall back to plain ordering
    rather than emitting nothing."""
    sections = ["Response: " + ("r" * 3000), LEARNED]
    out = hooks._fit_sections(sections, 512)
    assert out
    assert len(out.encode("utf-8")) <= 512


def test_deterministic() -> None:
    sections = ["Response: r", "decisions:\n" + ("d" * 3000), LEARNED]
    first = hooks._fit_sections(sections, 1024)
    for _ in range(5):
        assert hooks._fit_sections(sections, 1024) == first


def test_section_order_is_preserved() -> None:
    sections = ["Response: r", "decisions:\n- d", "session tail:\n- s", LEARNED]
    out = hooks._fit_sections(sections, 2048)
    assert out.index("Response:") < out.index("decisions:") < out.index("session tail:") < out.index(LEARNED_HEADER)


def test_empty_and_blank_sections_are_dropped() -> None:
    assert hooks._fit_sections([], 2048) == ""
    assert hooks._fit_sections(["", "Response: r", ""], 2048) == "Response: r"


def test_real_build_context_keeps_learned_rules(tmp_path: Path) -> None:
    """End-to-end through build_context with a deliberately oversized session tail."""
    mem = tmp_path / ".ai" / "memory"
    mem.mkdir(parents=True)
    (mem / "session-current.md").write_text(
        "\n".join(f"- [2026-08-27T00:00:0{i}Z] milestone {'m' * 300}" for i in range(6)),
        encoding="utf-8",
    )
    (mem / "learned_prompt.md").write_text(LEARNED, encoding="utf-8")
    ctx = hooks.build_context("UserPromptSubmit", {"agent": "codex"}, root=tmp_path)
    assert len(ctx.encode("utf-8")) <= hooks.MAX_INJECTION_BYTES
    assert LEARNED_HEADER in ctx, ctx[-200:]
