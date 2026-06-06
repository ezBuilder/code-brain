"""Graded citation matcher tests (Stage 3 verify) — deterministic, no LLM."""
from __future__ import annotations

from ai_core.autoresearch import verify_matcher as vm


def test_normalize_whitespace_and_case():
    assert vm.normalize("The  Quick\nBROWN  fox") == "the quick brown fox"


def test_exact_substring():
    r = vm.match_score("brown fox", "the quick brown fox jumps")
    assert r["score"] == 1.0 and r["kind"] == "exact"


def test_case_and_whitespace_still_exact():
    r = vm.match_score("THE   QUICK", "the quick brown")
    assert r["score"] == 1.0 and r["exact"] is True


def test_fuzzy_typo_scores_0_7():
    r = vm.match_score("the quick browm fox", "the quick brown fox jumps over")  # 1 typo
    assert r["kind"] == "fuzzy" and r["score"] == 0.7


def test_miss_scores_zero():
    r = vm.match_score("completely unrelated sentence here", "the quick brown fox")
    assert r["score"] == 0.0 and r["kind"] == "miss"


def test_long_tail_disables_fuzzy():
    # a near-match that would be fuzzy is rejected when long_tail (exact-only)
    r = vm.match_score("the quick browm", "the quick brown fox", long_tail=True)
    assert r["score"] == 0.0 and r["kind"] == "long_tail_miss"


def test_long_tail_exact_still_passes():
    r = vm.match_score("quick brown", "the quick brown fox", long_tail=True)
    assert r["score"] == 1.0 and r["kind"] == "exact"


def test_empty_quote_trivially_passes():
    assert vm.match_score("", "anything")["score"] == 1.0
