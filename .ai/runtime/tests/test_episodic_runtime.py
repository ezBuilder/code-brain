from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from ai_core import episodic_runtime as runtime


def _write_events(root: Path, count: int, *, year: int = 2026) -> Path:
    path = root / ".ai" / "memory" / "audit" / f"{year}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    previous_sha: str | None = None
    for index in range(count):
        line = json.dumps(
            {
                "ts": f"{year}-01-01T00:00:{index % 60:02d}Z",
                "event_id": f"evt-{index:032x}",
                "action": "tool.failed" if index % 17 == 0 else "event.append",
                "category": "test",
                "payload": {"index": index, "kind": f"kind-{index % 5}"},
                "prev_sha": previous_sha,
            },
            sort_keys=True,
        )
        rows.append(line)
        previous_sha = hashlib.sha256(line.encode("utf-8")).hexdigest()
    path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_loader_keeps_global_ordinals_and_physical_source_lines(tmp_path: Path) -> None:
    older = tmp_path / ".ai" / "memory" / "audit" / "2025.jsonl"
    older.parent.mkdir(parents=True)
    older.write_text(
        json.dumps({"action": "first", "payload": {"x": 1}})
        + "\n\n"
        + json.dumps({"action": "second", "payload": {"x": 2}})
        + "\n",
        encoding="utf-8",
    )
    older.chmod(0o600)
    _write_events(tmp_path, 2)

    corpus = runtime.load_audit_corpus(tmp_path)

    assert [event.index for event in corpus.events] == [0, 1, 2, 3]
    assert [event.source_line for event in corpus.events[:2]] == [1, 3]
    assert len({event.event_id for event in corpus.events}) == 4
    assert corpus.malformed_rows == 0


def test_loader_refuses_partial_index_when_raw_row_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"action": "valid"}) + "\n{broken\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(runtime.EpisodicRuntimeError, match="malformed audit row"):
        runtime.load_audit_corpus(tmp_path)


def test_build_is_noop_no_growth_and_hook_cache_is_bounded(tmp_path: Path) -> None:
    _write_events(tmp_path, 1_000)

    first = runtime.build_audit_index(tmp_path)
    status_first = runtime.status(tmp_path)
    files = sorted((tmp_path / ".ai" / "memory" / "episodic").rglob("*"))
    snapshot = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in files
        if path.is_file()
    }
    second = runtime.build_audit_index(tmp_path)
    snapshot_after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in files
        if path.is_file()
    }

    assert first["ok"] is True
    assert second["build"]["no_op"] is True
    assert second["cache_changed"] is False
    assert snapshot_after == snapshot
    assert status_first["ready"] is True
    assert status_first["tier_rows"] < 10 * 10  # logarithmic frontier, not N/fanout rows
    hook = runtime.read_hook_context(tmp_path)
    assert hook.startswith("cb-life:")
    assert len(hook.encode("utf-8")) <= runtime.HOOK_CONTEXT_MAX_BYTES


def test_runtime_self_repairs_tampered_disposable_summary(tmp_path: Path) -> None:
    _write_events(tmp_path, 100)
    runtime.build_audit_index(tmp_path)
    tier = tmp_path / ".ai" / "memory" / "episodic" / "audit" / "tier_2.jsonl"
    payload = json.loads(tier.read_text(encoding="utf-8").splitlines()[0])
    payload["summary"] = "forged"
    tier.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tier.chmod(0o600)

    assert runtime.status(tmp_path)["ready"] is False
    repaired = runtime.build_audit_index(tmp_path)

    assert repaired["repaired"] is True
    assert repaired["repair_reason"] == "derived_index_integrity"
    assert runtime.status(tmp_path)["ready"] is True


def test_runtime_rebases_shrunk_source_and_preserves_history_gap(tmp_path: Path) -> None:
    _write_events(tmp_path, 100)
    runtime.build_audit_index(tmp_path)
    _write_events(tmp_path, 20)

    repaired = runtime.build_audit_index(tmp_path)
    report = runtime.status(tmp_path)
    second = runtime.build_audit_index(tmp_path)

    assert repaired["ok"] is True
    assert repaired["repaired"] is True
    assert repaired["repair_reason"] == "source_shrink"
    assert repaired["source_history_gap"] == {
        "schema_version": 1,
        "reason": "source_shrink",
        "previous_indexed_events": 100,
        "current_raw_events": 20,
    }
    assert report["ready"] is True
    assert report["indexed_events"] == report["raw_events"] == 20
    assert report["source_truth_complete"] is False
    assert second["build"]["no_op"] is True
    assert second["source_history_gap"] == repaired["source_history_gap"]


def test_runtime_clears_stale_index_when_source_becomes_empty(tmp_path: Path) -> None:
    path = _write_events(tmp_path, 10)
    runtime.build_audit_index(tmp_path)
    path.write_text("", encoding="utf-8")

    repaired = runtime.build_audit_index(tmp_path)
    report = runtime.status(tmp_path)

    assert repaired["repair_reason"] == "source_shrink"
    assert report["ready"] is True
    assert report["indexed_events"] == report["raw_events"] == 0
    assert report["source_truth_complete"] is False


def test_loader_rejects_modern_hash_chain_mismatch(tmp_path: Path) -> None:
    path = _write_events(tmp_path, 3)
    rows = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[1])
    payload["prev_sha"] = None
    rows[1] = json.dumps(payload, sort_keys=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(runtime.EpisodicRuntimeError, match="hash-chain mismatch"):
        runtime.load_audit_corpus(tmp_path)


def test_doctor_does_not_misreport_failed_unbuilt_index_as_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_core.doctor import check_episodic_memory

    monkeypatch.setattr(
        runtime,
        "status",
        lambda _root: {"ok": False, "built": False, "integrity_ok": False},
    )

    result = check_episodic_memory(tmp_path)

    assert result.ok is False
    assert "invalid derived index" in result.detail


def test_explicit_context_obeys_hard_budget_and_has_exact_receipt(tmp_path: Path) -> None:
    _write_events(tmp_path, 1_000)
    runtime.build_audit_index(tmp_path)

    payload = runtime.context_payload(tmp_path, byte_budget=1_500, raw_tail=10)

    assert payload["ok"] is True
    assert payload["bytes_used"] <= 1_500
    assert payload["receipt"]["fully_covered"] is True
    assert payload["authoritative"] is False
    assert payload["drilldown_required"] is True


def test_drilldown_returns_raw_source_location_by_id_and_range(tmp_path: Path) -> None:
    _write_events(tmp_path, 30)
    corpus = runtime.load_audit_corpus(tmp_path)

    by_id = runtime.drilldown_payload(tmp_path, event_id=corpus.events[12].event_id)
    by_range = runtime.drilldown_payload(tmp_path, start=10, end=13)

    assert by_id["ok"] is True
    assert by_id["events"][0]["index"] == 12
    assert by_id["events"][0]["source"]["line"] == 13
    assert by_range["ok"] is True
    assert [item["index"] for item in by_range["events"]] == [10, 11, 12]
    assert by_range["authoritative"] is True


def test_legacy_fold_is_visible_but_never_claimed_as_complete_truth(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "ts": "2026-01-01T23:59:59Z",
                "action": "_folded",
                "payload": {"date": "2026-01-01", "total": 999},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    status = runtime.status(tmp_path)
    dry = runtime.build_audit_index(tmp_path, dry_run=True)

    assert status["legacy_fold_rows"] == 1
    assert status["source_truth_complete"] is False
    assert dry["legacy_fold_rows"] == 1


def test_hook_cache_surfaces_append_staleness_and_rejects_same_size_tamper(tmp_path: Path) -> None:
    path = _write_events(tmp_path, 100)
    runtime.build_audit_index(tmp_path)
    original_hook = runtime.read_hook_context(tmp_path)
    assert original_hook

    previous_line = path.read_text(encoding="utf-8").splitlines()[-1]
    appended = json.dumps(
        {
            "action": "new.append",
            "event_id": f"evt-{100:032x}",
            "payload": {},
            "prev_sha": hashlib.sha256(previous_line.encode("utf-8")).hexdigest(),
        },
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(appended + "\n")
    stale_hook = runtime.read_hook_context(tmp_path)
    assert stale_hook != original_hook
    assert "index stale" in stale_hook

    runtime.build_audit_index(tmp_path)
    before = path.read_bytes()
    stat = path.stat()
    mutated = before.replace(b"event.append", b"event.tamper", 1)
    assert len(mutated) == len(before)
    path.write_bytes(mutated)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert runtime.read_hook_context(tmp_path) == ""


def test_missing_audit_directory_is_empty_but_not_a_ready_index(tmp_path: Path) -> None:
    corpus = runtime.load_audit_corpus(tmp_path)
    built = runtime.build_audit_index(tmp_path)
    context = runtime.context_payload(tmp_path)

    assert corpus.events == ()
    assert corpus.source_states == ()
    assert built["ok"] is True and built["built"] is False
    assert built["reason"] == "no_audit_events"
    assert context["ok"] is False and context["reason"] == "no_audit_events"


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_loader_refuses_canonical_audit_file_excluded_by_trust_checks(tmp_path: Path) -> None:
    audit = tmp_path / ".ai" / "memory" / "audit"
    audit.mkdir(parents=True)
    external = tmp_path / "external-audit.jsonl"
    external.write_text(json.dumps({"action": "external"}) + "\n", encoding="utf-8")
    (audit / "2026.jsonl").symlink_to(external)

    with pytest.raises(runtime.EpisodicRuntimeError, match="untrusted or unreadable"):
        runtime.load_audit_corpus(tmp_path)


def test_hook_cache_rejects_source_replacement_and_source_set_change(tmp_path: Path) -> None:
    path = _write_events(tmp_path, 100)
    runtime.build_audit_index(tmp_path)
    assert runtime.read_hook_context(tmp_path)

    replacement = path.with_name("replacement.jsonl")
    replacement.write_bytes(path.read_bytes() + b'{"action":"legacy.append"}\n')
    replacement.chmod(0o600)
    os.replace(replacement, path)
    assert runtime.read_hook_context(tmp_path) == ""

    runtime.build_audit_index(tmp_path)
    assert runtime.read_hook_context(tmp_path)
    other = _write_events(tmp_path, 1, year=2027)
    assert other.exists()
    assert runtime.read_hook_context(tmp_path) == ""
