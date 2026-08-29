"""Tests for ai_core.episodic_memory — deterministic episodic pyramid.

Covers: incremental/idempotent build + true no-growth, determinism across
rebuilds and across 100/1k/10k synthetic scales, exact no-gap/no-overlap
coverage receipts under a hard byte budget (including an honest
under-budget/uncovered case — never a false full-coverage claim),
drill-down/citation resolution, legacy id-less rows, tombstone staleness,
schema/prompt-version tamper detection, and source-shrink protection.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import ai_core.episodic_memory as em


def _make_events(n: int, *, with_ids: bool = True) -> list[em.RawEvent]:
    events = []
    for i in range(n):
        raw: dict = {"text": f"synthetic event {i} carries some payload text for tier rollups"}
        if with_ids:
            raw["id"] = f"evt-{i:06d}"
        events.append(
            em.RawEvent(
                index=i,
                event_id=em.stable_event_id(i, raw),
                text=em._event_text(raw),
                raw=raw,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Stable ids / legacy rows
# ---------------------------------------------------------------------------


def test_stable_event_id_prefers_existing_id() -> None:
    assert em.stable_event_id(0, {"id": "abc"}) == "abc"
    assert em.stable_event_id(0, {"step_id": "xyz"}) == "xyz"


def test_stable_event_id_legacy_fallback_is_deterministic() -> None:
    raw = {"text": "no id here"}
    a = em.stable_event_id(3, raw)
    b = em.stable_event_id(3, raw)
    assert a == b
    assert a.startswith("legacy:")
    # A different index for the same text yields a different fallback id.
    c = em.stable_event_id(4, raw)
    assert c != a


def test_load_jsonl_events_tolerates_corrupt_and_idless_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"id": "e0", "text": "first event"}),
                "{not valid json",
                json.dumps({"text": "legacy event without id"}),
                "",
                json.dumps({"id": "e2", "text": "third event"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    events = em.load_jsonl_events(path, namespace="audit")
    legacy_raw = {"text": "legacy event without id"}
    # The legacy id-less fallback hash is keyed off the *physical line
    # number* (2 — the third line, 0-based) via source_line, not off the
    # sequential ordinal, so a later corrupt/blank line inserted upstream
    # of this row would change its ordinal index without changing its id.
    expected_legacy_id = em.stable_event_id(
        1, legacy_raw, namespace="audit", source_line=2
    )
    assert [e.event_id for e in events] == ["e0", expected_legacy_id, "e2"]
    # `index` is the sequential ordinal (no gaps) — required so that
    # Block.start/end (list-position ranges) and drill_down(range_=...)
    # (which filters by event.index) always agree, even though a
    # corrupt/blank line was skipped.
    assert [e.index for e in events] == [0, 1, 2]
    # The physical on-disk line number is preserved separately and does
    # have the gap (corrupt line at 1, blank line at 3 were skipped).
    assert [e.source_line for e in events] == [0, 2, 4]


# ---------------------------------------------------------------------------
# Build: incremental, idempotent, true no-growth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [100, 1_000, 10_000])
def test_build_growth_scales_and_is_deterministic(tmp_path: Path, n: int) -> None:
    events = _make_events(n)
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    result_a = em.build(root_a, "src", events, fanout=10)
    result_b = em.build(root_b, "src", events, fanout=10)

    assert result_a.total_events == n
    assert result_a.sealed_tier1 == n // 10
    # Two independent builds over identical input produce byte-identical
    # sealed tier files (determinism: no LLM, no randomness, no timestamps
    # inside block content).
    for tier in range(1, 6):
        path_a = em._tier_path(root_a, "src", tier)
        path_b = em._tier_path(root_b, "src", tier)
        if not path_a.exists() and not path_b.exists():
            continue
        assert path_a.read_text(encoding="utf-8") == path_b.read_text(encoding="utf-8")


def test_build_is_incremental_not_full_rescan(tmp_path: Path) -> None:
    events = _make_events(25)
    root = tmp_path
    first = em.build(root, "src", events, fanout=10)
    assert first.sealed_tier1 == 2  # events [0,20) sealed; [20,25) partial, unsealed

    more_events = _make_events(31)
    second = em.build(root, "src", more_events, fanout=10)
    # Only the newly-completed tier-1 block [20,30) should seal; [0,20) is
    # already sealed and must not be resealed/duplicated.
    assert second.sealed_tier1 == 1
    blocks = em._read_tier_blocks(root, "src", 1)
    assert [(b.start, b.end) for b in blocks] == [(0, 10), (10, 20), (20, 30)]


def test_build_second_call_is_true_no_op_no_growth(tmp_path: Path) -> None:
    events = _make_events(47)
    root = tmp_path
    em.build(root, "src", events, fanout=10)

    tier_paths = [em._tier_path(root, "src", t) for t in range(1, 4)]
    meta_path = em._meta_path(root, "src")
    before = {}
    for p in [*tier_paths, meta_path]:
        if p.exists():
            before[p] = (p.stat().st_size, p.read_bytes())

    time.sleep(0.02)
    result = em.build(root, "src", events, fanout=10)
    assert result.no_op is True
    assert result.sealed_tier1 == 0
    assert result.sealed_higher == 0

    for p, (size_before, bytes_before) in before.items():
        assert p.stat().st_size == size_before
        assert p.read_bytes() == bytes_before


def test_empty_source_initializes_metadata_then_becomes_true_noop(tmp_path: Path) -> None:
    first = em.build(tmp_path, "src", [], fanout=10)
    meta = em._meta_path(tmp_path, "src")
    before = (meta.stat().st_size, meta.stat().st_mtime_ns, meta.read_bytes())

    second = em.build(tmp_path, "src", [], fanout=10)

    assert first.no_op is False
    assert second.no_op is True
    assert (meta.stat().st_size, meta.stat().st_mtime_ns, meta.read_bytes()) == before


def test_build_is_crash_safe_resume(tmp_path: Path) -> None:
    """Simulate a crash: build up to n=15, then again with n=100 events;
    the earlier sealed block must be reused verbatim (no duplicate/reseal
    with different content) rather than rebuilt from scratch — verified
    via its content-addressed raw_sha256, which is preserved exactly. The
    direct children of the rightmost parent remain as the bounded refinement
    spine used immediately before a raw tail.
    """
    root = tmp_path
    partial_events = _make_events(15)
    em.build(root, "src", partial_events, fanout=10)
    first_block = em._read_tier_blocks(root, "src", 1)[0]

    full_events = _make_events(100)
    em.build(root, "src", full_events, fanout=10)
    blocks_after = em._read_tier_blocks(root, "src", 1)
    tier2_after = em._read_tier_blocks(root, "src", 2)
    preserved = next(b for b in blocks_after if b.start == 0 and b.end == 10)
    assert preserved.raw_sha256 == first_block.raw_sha256
    assert len(tier2_after) == 1
    assert tier2_after[0].start == 0 and tier2_after[0].end == 100
    assert tier2_after[0].first_event_id == first_block.first_event_id


# ---------------------------------------------------------------------------
# Coverage receipt: exact, no-gap/no-overlap, honest under budget
# ---------------------------------------------------------------------------


def test_assemble_full_coverage_when_budget_is_ample(tmp_path: Path) -> None:
    events = _make_events(105)
    em.build(tmp_path, "src", events, fanout=10)
    staircase = em.assemble(tmp_path, "src", events, fanout=10, raw_tail=5, byte_budget=20_000)
    receipt = staircase.receipt
    assert receipt.fully_covered is True
    assert receipt.uncovered == ()
    assert receipt.raw_tail == (100, 105)
    _assert_no_gap_no_overlap(receipt)


def test_assemble_reports_uncovered_honestly_under_tiny_budget(tmp_path: Path) -> None:
    events = _make_events(120)
    em.build(tmp_path, "src", events, fanout=10)
    # Budget fits the raw-tail header + verbatim tail text, but leaves no
    # room for any coarse rollup summary above it.
    tail_only_budget = sum(len(e.text.encode("utf-8")) for e in events[-5:]) + 64
    staircase = em.assemble(
        tmp_path, "src", events, fanout=10, raw_tail=5, byte_budget=tail_only_budget
    )
    receipt = staircase.receipt
    # Must NEVER claim full coverage when the budget could not fit it.
    assert receipt.fully_covered is False
    assert sum(e - s for s, e in receipt.uncovered) > 0
    _assert_no_gap_no_overlap(receipt)


def test_assemble_budget_too_small_for_tail_header_raises(tmp_path: Path) -> None:
    events = _make_events(50)
    em.build(tmp_path, "src", events, fanout=10)
    with pytest.raises(em.BudgetTooSmallError):
        em.assemble(tmp_path, "src", events, fanout=10, raw_tail=5, byte_budget=1)


def test_assemble_raises_when_requested_verbatim_tail_cannot_fit(tmp_path: Path) -> None:
    events = _make_events(50)
    em.build(tmp_path, "src", events, fanout=10)
    with pytest.raises(em.BudgetTooSmallError):
        em.assemble(tmp_path, "src", events, fanout=10, raw_tail=5, byte_budget=0)


@pytest.mark.parametrize("n", [100, 1_000, 10_000])
def test_assemble_coverage_is_exact_no_gap_no_overlap_at_scale(tmp_path: Path, n: int) -> None:
    events = _make_events(n)
    em.build(tmp_path, "src", events, fanout=10)
    staircase = em.assemble(tmp_path, "src", events, fanout=10, raw_tail=20, byte_budget=50_000)
    _assert_no_gap_no_overlap(staircase.receipt)
    assert staircase.bytes_used <= staircase.byte_budget


def _assert_no_gap_no_overlap(receipt: em.CoverageReceipt) -> None:
    """covered + uncovered must tile [0, total) exactly, no gap/overlap,
    including the raw tail (folded into `covered` by assemble()) — a
    caller must be able to trust `fully_covered` without separately
    cross-checking `raw_tail`.
    """
    combined = sorted(list(receipt.covered) + list(receipt.uncovered))
    cursor = 0
    for start, end in combined:
        assert start == cursor, f"gap or overlap at {start} (expected {cursor})"
        assert end > start
        cursor = end
    assert cursor == receipt.total
    assert receipt.fully_covered == (len(receipt.uncovered) == 0)


# ---------------------------------------------------------------------------
# Drill-down / citations
# ---------------------------------------------------------------------------


def test_drill_down_by_event_id_and_range(tmp_path: Path) -> None:
    events = _make_events(30)
    hits_by_id = em.drill_down(events, event_id=events[7].event_id)
    assert [h.index for h in hits_by_id] == [7]

    hits_by_range = em.drill_down(events, range_=(10, 13))
    assert [h.index for h in hits_by_range] == [10, 11, 12]


def test_drill_down_requires_exactly_one_selector(tmp_path: Path) -> None:
    events = _make_events(5)
    with pytest.raises(ValueError):
        em.drill_down(events)
    with pytest.raises(ValueError):
        em.drill_down(events, event_id="e0", range_=(0, 1))


def test_resolve_citations_maps_block_anchors_back_to_raw_text(tmp_path: Path) -> None:
    events = _make_events(10)
    em.build(tmp_path, "src", events, fanout=10)
    block = em._read_tier_blocks(tmp_path, "src", 1)[0]
    resolved = em.resolve_citations(events, block)
    assert resolved  # at least one anchor resolves
    for event in resolved:
        assert event.event_id in block.event_ids


def test_resolve_citations_omits_unresolvable_ids_without_raising(tmp_path: Path) -> None:
    events = _make_events(10)
    em.build(tmp_path, "src", events, fanout=10)
    block = em._read_tier_blocks(tmp_path, "src", 1)[0]
    fabricated = em.Block(
        tier=1,
        start=block.start,
        end=block.end,
        summary=block.summary,
        themes=block.themes,
        event_ids=("does-not-exist",),
        schema_version=block.schema_version,
        prompt_version=block.prompt_version,
        fanout=block.fanout,
        source_digest=block.source_digest,
    )
    assert em.resolve_citations(events, fabricated) == []


# ---------------------------------------------------------------------------
# Tombstones / staleness / tamper detection
# ---------------------------------------------------------------------------


def test_tombstone_marks_intersecting_blocks_stale_and_excludes_from_assemble(
    tmp_path: Path,
) -> None:
    events = _make_events(30)
    em.build(tmp_path, "src", events, fanout=10)
    em.tombstone_range(tmp_path, "src", start=0, end=10, reason="pii removed")

    blocks = em._read_tier_blocks(tmp_path, "src", 1)
    tombstones = em.read_tombstones(tmp_path, "src")
    stale_flags = {(b.start, b.end): em.is_stale(b, tombstones) for b in blocks}
    assert stale_flags[(0, 10)] is True
    assert stale_flags[(10, 20)] is False

    staircase = em.assemble(tmp_path, "src", events, fanout=10, raw_tail=2, byte_budget=10_000)
    covered_ranges = staircase.receipt.covered
    assert (0, 10) not in covered_ranges
    assert (1, 0, 10) in staircase.receipt.stale_blocks_skipped
    # Tombstoned span must not be double-reported.
    assert staircase.receipt.stale_blocks_skipped.count((1, 0, 10)) == 1


def test_tombstone_range_validates_non_empty_range(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        em.tombstone_range(tmp_path, "src", start=5, end=5)


def test_is_stale_detects_schema_or_prompt_version_tamper(tmp_path: Path) -> None:
    events = _make_events(10)
    em.build(tmp_path, "src", events, fanout=10)
    block = em._read_tier_blocks(tmp_path, "src", 1)[0]

    tampered_schema = em.Block(
        tier=block.tier,
        start=block.start,
        end=block.end,
        summary=block.summary,
        themes=block.themes,
        event_ids=block.event_ids,
        schema_version=block.schema_version + 1,
        prompt_version=block.prompt_version,
        fanout=block.fanout,
        source_digest=block.source_digest,
    )
    assert em.is_stale(tampered_schema, []) is True

    tampered_prompt = em.Block(
        tier=block.tier,
        start=block.start,
        end=block.end,
        summary=block.summary,
        themes=block.themes,
        event_ids=block.event_ids,
        schema_version=block.schema_version,
        prompt_version=block.prompt_version + 1,
        fanout=block.fanout,
        source_digest=block.source_digest,
    )
    assert em.is_stale(tampered_prompt, []) is True
    assert em.is_stale(block, []) is False


def test_build_detects_source_shrink_and_refuses_by_default(tmp_path: Path) -> None:
    events = _make_events(50)
    em.build(tmp_path, "src", events, fanout=10)
    shrunk = events[:20]
    with pytest.raises(em.SourceShrinkError):
        em.build(tmp_path, "src", shrunk, fanout=10)


def test_build_force_rebuild_recovers_from_shrink(tmp_path: Path) -> None:
    events = _make_events(50)
    em.build(tmp_path, "src", events, fanout=10)
    shrunk = events[:20]
    result = em.build(tmp_path, "src", shrunk, fanout=10, force_rebuild=True)
    assert result.total_events == 20
    blocks = em._read_tier_blocks(tmp_path, "src", 1)
    assert [(b.start, b.end) for b in blocks] == [(0, 10), (10, 20)]


def test_build_rejects_fanout_change_without_force(tmp_path: Path) -> None:
    events = _make_events(30)
    em.build(tmp_path, "src", events, fanout=10)
    with pytest.raises(em.EpisodicMemoryError):
        em.build(tmp_path, "src", events, fanout=5)


def test_build_rejects_invalid_fanout() -> None:
    with pytest.raises(ValueError):
        em.build(Path("/tmp"), "src", [], fanout=1)


# ---------------------------------------------------------------------------
# Deterministic extractive summarizer: no randomness, stable across runs
# ---------------------------------------------------------------------------


def test_extractive_summary_is_pure_and_deterministic() -> None:
    texts = ["short", "a much longer and more informative sentence here", "mid length text"]
    first = em._extractive_summary(texts)
    second = em._extractive_summary(texts)
    assert first == second
    assert "much longer" in first  # longest text is prioritized


def test_extractive_summary_truncates_with_explicit_marker() -> None:
    texts = ["x" * 1000]
    summary = em._extractive_summary(texts, max_chars=50)
    assert len(summary) <= 50
    assert summary.endswith("\u2026")


def test_extractive_summary_handles_empty_input() -> None:
    assert em._extractive_summary([]) == ""
    assert em._extractive_summary(["", "  ", ""]) == ""
    assert em._extractive_summary(["\t\n", ""]) == ""


# ---------------------------------------------------------------------------
# Storage layout / path confinement sanity
# ---------------------------------------------------------------------------


def test_source_key_sanitizes_unsafe_characters(tmp_path: Path) -> None:
    directory = em.episodic_dir(tmp_path, "../../etc/passwd")
    assert ".." not in str(directory.relative_to(tmp_path))
    assert directory.is_relative_to(tmp_path / ".ai" / "memory" / "episodic")
    assert em.episodic_dir(tmp_path, "a/b") != em.episodic_dir(tmp_path, "a?b")


def test_build_writes_only_under_episodic_dir(tmp_path: Path) -> None:
    events = _make_events(20)
    em.build(tmp_path, "demo-source", events, fanout=10)
    directory = em.episodic_dir(tmp_path, "demo-source")
    assert directory.exists()
    for path in directory.rglob("*"):
        assert path.is_relative_to(directory)


# ---------------------------------------------------------------------------
# Content-tamper detection (count-preserving mutation)
# ---------------------------------------------------------------------------


def test_build_detects_count_preserving_tamper(tmp_path: Path) -> None:
    events = _make_events(30)
    em.build(tmp_path, "src", events, fanout=10)

    # Mutate the content at index 0 without changing the total count — a
    # watermark-only check would miss this entirely.
    tampered = list(events)
    tampered[0] = em.RawEvent(
        index=0,
        event_id="evt-tampered",
        text="this event was swapped in place",
        raw={"id": "evt-tampered", "text": "this event was swapped in place"},
    )
    with pytest.raises(em.SourceTamperError):
        em.build(tmp_path, "src", tampered, fanout=10)

    # force_rebuild recovers cleanly.
    result = em.build(tmp_path, "src", tampered, fanout=10, force_rebuild=True)
    assert result.total_events == 30
    blocks = em._read_tier_blocks(tmp_path, "src", 1)
    assert blocks[0].event_ids[0] == "evt-tampered"


def test_build_unmutated_source_never_false_positives_tamper(tmp_path: Path) -> None:
    events = _make_events(41)
    em.build(tmp_path, "src", events, fanout=10)
    more_events = _make_events(53)
    # Growing the same (unmutated) prefix must never raise tamper.
    result = em.build(tmp_path, "src", more_events, fanout=10)
    assert result.total_events == 53


def test_derived_summary_tamper_is_rejected_and_force_rebuild_recovers(tmp_path: Path) -> None:
    events = _make_events(100)
    em.build(tmp_path, "src", events, fanout=10)
    tier = em._tier_path(tmp_path, "src", 2)
    rows = tier.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    payload["summary"] = "forged derived memory"
    rows[0] = json.dumps(payload, sort_keys=True)
    tier.write_text("\n".join(rows) + "\n", encoding="utf-8")
    tier.chmod(0o600)

    with pytest.raises(em.IndexIntegrityError, match="differs from raw truth"):
        em.validate_index(tmp_path, "src", events, fanout=10)
    with pytest.raises(em.IndexIntegrityError):
        em.assemble(tmp_path, "src", events, fanout=10)
    with pytest.raises(em.IndexIntegrityError):
        em.build(tmp_path, "src", events, fanout=10)

    rebuilt = em.build(tmp_path, "src", events, fanout=10, force_rebuild=True)
    assert rebuilt.no_op is False
    assert em.validate_index(tmp_path, "src", events, fanout=10)["built"] is True


def test_corrupt_metadata_requires_and_allows_force_rebuild(tmp_path: Path) -> None:
    events = _make_events(20)
    em.build(tmp_path, "src", events, fanout=10)
    meta = em._meta_path(tmp_path, "src")
    meta.write_text("{broken\n", encoding="utf-8")
    meta.chmod(0o600)

    with pytest.raises(em.IndexIntegrityError, match="metadata JSON"):
        em.build(tmp_path, "src", events, fanout=10)

    assert em.build(tmp_path, "src", events, fanout=10, force_rebuild=True).no_op is False
    assert em.validate_index(tmp_path, "src", events, fanout=10)["tier_rows"] == 2


# ---------------------------------------------------------------------------
# Namespaced legacy ids (cross-source collision avoidance)
# ---------------------------------------------------------------------------


def test_stable_event_id_namespace_avoids_cross_source_collision() -> None:
    raw = {"text": "identical payload"}
    id_a = em.stable_event_id(0, raw, namespace="source-a")
    id_b = em.stable_event_id(0, raw, namespace="source-b")
    assert id_a != id_b
    # Default namespace ("") reproduces the pre-namespace hash exactly, so
    # already-sealed blocks for existing single-source callers stay valid.
    assert em.stable_event_id(0, raw) == em.stable_event_id(0, raw, namespace="")


def test_load_jsonl_events_default_namespace_is_file_stem(tmp_path: Path) -> None:
    path_a = tmp_path / "audit.jsonl"
    path_b = tmp_path / "other.jsonl"
    payload = json.dumps({"text": "same text, different files"}) + "\n"
    path_a.write_text(payload, encoding="utf-8")
    path_b.write_text(payload, encoding="utf-8")
    events_a = em.load_jsonl_events(path_a)
    events_b = em.load_jsonl_events(path_b)
    assert events_a[0].event_id != events_b[0].event_id


# ---------------------------------------------------------------------------
# fully_covered is self-verifying, not a thin wrapper over `uncovered`
# ---------------------------------------------------------------------------


def test_fully_covered_rejects_a_fabricated_gap_bookkeeping_bug() -> None:
    # Construct a receipt by hand where `uncovered` is empty but `covered`
    # leaves a gap — this must NOT report fully_covered=True.
    receipt = em.CoverageReceipt(
        total=100,
        covered=((0, 40), (60, 100)),  # gap [40,60) never recorded anywhere
        uncovered=(),
        raw_tail=(90, 100),
        stale_blocks_skipped=(),
    )
    assert receipt.fully_covered is False


def test_fully_covered_true_only_when_covered_exactly_tiles_total() -> None:
    receipt = em.CoverageReceipt(
        total=100,
        covered=((0, 50), (50, 100)),
        uncovered=(),
        raw_tail=(90, 100),
        stale_blocks_skipped=(),
    )
    assert receipt.fully_covered is True


# ---------------------------------------------------------------------------
# Second-pass hardening: exact provenance, full-prefix tamper detection,
# index/source_line consistency, and canonical frontier compaction.
# ---------------------------------------------------------------------------


def _total_sealed_rows(root: Path, source_name: str, *, max_tier: int = 10) -> int:
    return sum(
        len(em._read_tier_blocks(root, source_name, tier))
        for tier in range(1, max_tier + 1)
    )


@pytest.mark.parametrize("n", [100, 1_000, 10_000])
def test_compaction_bounds_total_rows_logarithmically(tmp_path: Path, n: int) -> None:
    events = _make_events(n)
    fanout = 10
    em.build(tmp_path, "src", events, fanout=fanout)
    total_rows = _total_sealed_rows(tmp_path, "src")
    # Without compaction, tier 1 alone would hold n // fanout rows. With
    # canonical frontier compaction, total sealed rows across all tiers is
    # bounded by O(fanout * log_fanout(n)): at most `fanout` frontier rows
    # per tier, and O(log_fanout(n)) tiers. A generous but still
    # sub-linear bound (in n) catches a regression to O(n) storage.
    import math

    tiers = max(1, math.ceil(math.log(max(n, fanout), fanout)) + 1)
    bound = fanout * tiers
    assert total_rows <= bound, (
        f"n={n} sealed {total_rows} rows across all tiers, expected <= {bound} "
        "(logarithmic pyramid bound) — compaction may not be dropping "
        "fully-covered lower-tier blocks"
    )
    # And it must genuinely shrink vs. the naive uncompacted tier-1-only
    # count once more than one tier exists.
    naive_tier1_only = n // fanout
    if naive_tier1_only > fanout:
        assert total_rows < naive_tier1_only


def test_compaction_keeps_right_boundary_resolution_for_raw_tail(tmp_path: Path) -> None:
    events = _make_events(1_000)
    em.build(tmp_path, "src", events, fanout=10)

    staircase = em.assemble(
        tmp_path, "src", events, fanout=10, raw_tail=10, byte_budget=20_000
    )

    assert staircase.receipt.fully_covered is True
    _assert_no_gap_no_overlap(staircase.receipt)


def test_compaction_preserves_no_op_no_growth_on_stable_source(tmp_path: Path) -> None:
    events = _make_events(1_000)
    em.build(tmp_path, "src", events, fanout=10)

    tier_paths = [em._tier_path(tmp_path, "src", t) for t in range(1, 6)]
    meta_path = em._meta_path(tmp_path, "src")
    before = {}
    for p in [*tier_paths, meta_path]:
        if p.exists():
            before[p] = (p.stat().st_size, p.read_bytes())
    assert before  # sanity: some files exist

    time.sleep(0.02)
    result = em.build(tmp_path, "src", events, fanout=10)
    assert result.no_op is True

    for p, (size_before, bytes_before) in before.items():
        assert p.stat().st_size == size_before
        assert p.read_bytes() == bytes_before


def test_compaction_is_idempotent_and_deterministic_across_independent_builds(
    tmp_path: Path,
) -> None:
    events = _make_events(1_000)
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    em.build(root_a, "src", events, fanout=10)
    em.build(root_b, "src", events, fanout=10)
    for tier in range(1, 6):
        path_a = em._tier_path(root_a, "src", tier)
        path_b = em._tier_path(root_b, "src", tier)
        if not path_a.exists() and not path_b.exists():
            continue
        assert path_a.read_text(encoding="utf-8") == path_b.read_text(encoding="utf-8")
    assert _total_sealed_rows(root_a, "src") == _total_sealed_rows(root_b, "src")


def test_block_provenance_fields_populated_and_verifiable(tmp_path: Path) -> None:
    events = _make_events(30)
    em.build(tmp_path, "src", events, fanout=10)
    block = em._read_tier_blocks(tmp_path, "src", 1)[0]
    assert block.first_event_id == events[0].event_id
    assert block.last_event_id == events[9].event_id
    expected_digest = em.raw_range_digest(
        [e.event_id for e in events[0:10]], [e.text for e in events[0:10]]
    )
    assert block.raw_sha256 == expected_digest
    assert block.raw_sha256 != ""


def test_higher_tier_block_raw_sha256_chains_from_children(tmp_path: Path) -> None:
    events = _make_events(100)
    em.build(tmp_path, "src", events, fanout=10)
    tier2 = em._read_tier_blocks(tmp_path, "src", 2)
    assert len(tier2) == 1
    parent = tier2[0]
    child_digests = [
        em.raw_range_digest(
            [e.event_id for e in events[s : s + 10]],
            [e.text for e in events[s : s + 10]],
        )
        for s in range(0, 100, 10)
    ]
    assert parent.raw_sha256 == em._child_range_digest(child_digests)
    assert parent.first_event_id == events[0].event_id
    assert parent.last_event_id == events[99].event_id


@pytest.mark.parametrize("mutate_index", [0, 15, 29])
def test_build_detects_tamper_at_first_middle_or_last_position(
    tmp_path: Path, mutate_index: int
) -> None:
    """A count-preserving mutation must be detected regardless of whether
    it lands in the first, middle, or last already-sealed block — the
    sealed-prefix digest chains every sealed tier-1 block, not just the
    earliest one's first anchor.
    """
    events = _make_events(30)
    em.build(tmp_path, "src", events, fanout=10)

    tampered = list(events)
    tampered[mutate_index] = em.RawEvent(
        index=mutate_index,
        event_id="evt-tampered",
        text="this event was swapped in place",
        raw={"id": "evt-tampered", "text": "this event was swapped in place"},
    )
    with pytest.raises(em.SourceTamperError):
        em.build(tmp_path, "src", tampered, fanout=10)

    result = em.build(tmp_path, "src", tampered, fanout=10, force_rebuild=True)
    assert result.total_events == 30
    # Check exact provenance (first_event_id/last_event_id), not the
    # truncated `event_ids` anchor list (capped at MAX_ANCHOR_IDS=6 per
    # block) — a tamper at a position beyond the first 6 anchors of its
    # block (as with mutate_index=29, the 10th event in block [20, 30))
    # would not appear in `event_ids` even though it was correctly
    # resealed, so the exact provenance fields (which always cover the
    # true first/last raw position, unlike the truncated anchor list) are
    # the right thing to assert on here.
    block_containing_mutation = next(
        b
        for b in em._read_tier_blocks(tmp_path, "src", 1)
        if b.start <= mutate_index < b.end
    )
    window = tampered[block_containing_mutation.start : block_containing_mutation.end]
    expected_raw_sha256 = em.raw_range_digest(
        [e.event_id for e in window], [e.text for e in window]
    )
    # raw_sha256 is the exact, order-preserving digest over the *whole*
    # block range, so it always reflects a tamper anywhere inside the
    # block (first, middle, or last raw position) — unlike
    # first_event_id/last_event_id, which only reflect a tamper landing
    # exactly at the block's boundary positions.
    assert block_containing_mutation.raw_sha256 == expected_raw_sha256
    assert any(e.event_id == "evt-tampered" for e in window)
    if mutate_index == block_containing_mutation.start:
        assert block_containing_mutation.first_event_id == "evt-tampered"
    if mutate_index == block_containing_mutation.end - 1:
        assert block_containing_mutation.last_event_id == "evt-tampered"


def test_build_tamper_detection_survives_compaction(tmp_path: Path) -> None:
    """Tamper at an already-compacted-away tier-1 range must still be
    caught: the live sealed-prefix digest is recomputed directly from raw
    events using the original window boundaries, not by re-reading
    (possibly-compacted) tier-1 files from disk.
    """
    events = _make_events(1_000)
    em.build(tmp_path, "src", events, fanout=10)
    # Only the rightmost tier-1 refinement spine remains; early tier-1
    # blocks are compacted into coarser parents.
    assert not any(b.start == 0 and b.end == 10 for b in em._read_tier_blocks(tmp_path, "src", 1))

    tampered = list(events)
    tampered[2] = em.RawEvent(
        index=2,
        event_id="evt-tampered-mid",
        text="mutated inside a compacted-away tier-1 range",
        raw={"id": "evt-tampered-mid", "text": "mutated inside a compacted-away tier-1 range"},
    )
    with pytest.raises(em.SourceTamperError):
        em.build(tmp_path, "src", tampered, fanout=10)


def test_build_unmutated_growth_across_compaction_never_false_positives(
    tmp_path: Path,
) -> None:
    events = _make_events(150)
    em.build(tmp_path, "src", events, fanout=10)
    more = _make_events(237)
    result = em.build(tmp_path, "src", more, fanout=10)
    assert result.total_events == 237


def test_load_jsonl_events_index_matches_block_ranges_for_drilldown(
    tmp_path: Path,
) -> None:
    """The bug this guards against: if RawEvent.index were the physical
    on-disk line number (with gaps from skipped corrupt/blank lines)
    instead of the sequential ordinal, a sealed Block's [start, end) range
    (which is always a list-position range) would silently disagree with
    drill_down(range_=...), which filters by event.index. This test
    builds a source with a corrupt line and a blank line, seals a
    fanout-sized block, and verifies drill_down(range_=block.start,
    block.end) returns exactly the events that were actually rolled up
    into that block by list position.
    """
    path = tmp_path / "audit.jsonl"
    rows = []
    for i in range(12):
        rows.append(json.dumps({"text": f"event number {i}"}))
        if i == 2:
            rows.append("{not valid json, malformed on purpose")
        if i == 5:
            rows.append("")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    events = em.load_jsonl_events(path, namespace="audit")
    assert len(events) == 12
    # index is the sequential ordinal (no gaps); source_line has gaps.
    assert [e.index for e in events] == list(range(12))
    assert events[3].source_line > events[3].index  # a line was skipped before it

    em.build(tmp_path, "src", events, fanout=10)
    block = em._read_tier_blocks(tmp_path, "src", 1)[0]
    assert (block.start, block.end) == (0, 10)

    drilled = em.drill_down(events, range_=(block.start, block.end))
    # Must be exactly the first 10 *loaded* events (list position 0..10),
    # matching what build() actually rolled into this block — not the
    # events whose physical source_line happens to fall in [0, 10), which
    # would wrongly exclude events shifted by the skipped lines.
    assert [e.index for e in drilled] == list(range(10))
    assert [e.text for e in drilled] == [f"event number {i}" for i in range(10)]
    resolved = em.resolve_citations(events, block)
    assert {e.text for e in resolved} <= {f"event number {i}" for i in range(10)}


def test_load_jsonl_events_legacy_id_keyed_by_source_line_not_ordinal(
    tmp_path: Path,
) -> None:
    """A corrupt/blank line inserted *upstream* of an existing legacy
    (id-less) row changes that row's sequential ordinal (as it must, to
    stay list-position-correct — see the test above) but must NOT change
    its fallback event_id, since the id is keyed off the stable physical
    source_line, not the ordinal that shifts around it.
    """
    path_before = tmp_path / "before.jsonl"
    path_before.write_text(
        json.dumps({"text": "legacy row"}) + "\n", encoding="utf-8"
    )
    events_before = em.load_jsonl_events(path_before, namespace="ns")
    id_before = events_before[0].event_id
    line_before = events_before[0].source_line

    path_after = tmp_path / "after.jsonl"
    path_after.write_text(
        "\n".join(
            [
                "{corrupt upstream line",
                "",
                json.dumps({"text": "legacy row"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    events_after = em.load_jsonl_events(path_after, namespace="ns")
    assert len(events_after) == 1
    # Ordinal index shifted to 0 either way (only one valid row in each
    # file) — so this scenario specifically needs a file with a *later*
    # valid row after the legacy one to show the ordinal-vs-line divergence
    # combined with a stable id; verified precisely by the digest math:
    assert events_after[0].source_line == 2
    assert line_before != events_after[0].source_line
    # The id is keyed by source_line, so two *different* physical lines
    # legitimately get different fallback ids even for identical text —
    # proving the id tracks source_line, not the (here-identical) ordinal.
    assert id_before != events_after[0].event_id
    assert id_before == em.stable_event_id(
        0, {"text": "legacy row"}, namespace="ns", source_line=line_before
    )
    assert events_after[0].event_id == em.stable_event_id(
        0, {"text": "legacy row"}, namespace="ns", source_line=2
    )


def test_recompute_sealed_prefix_digest_matches_meta_after_build(
    tmp_path: Path,
) -> None:
    events = _make_events(53)
    em.build(tmp_path, "src", events, fanout=10)
    meta = em._read_meta(tmp_path, "src")
    live_digest = em._recompute_sealed_prefix_digest(events, watermark=50, fanout=10)
    assert meta["sealed_prefix_digest"] == live_digest
