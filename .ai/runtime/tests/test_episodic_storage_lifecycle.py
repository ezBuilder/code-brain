from __future__ import annotations

from pathlib import Path

from ai_core import episodic_memory as episodic
from ai_core import storage_lifecycle as storage


def _known_untracked(monkeypatch) -> None:
    monkeypatch.setattr(storage, "_tracked_top_entries", lambda _root, _directory: (set(), True))
    monkeypatch.setattr(storage, "_referenced_entry_names", lambda _root, _directory, _names: set())


def test_authoritative_memory_does_not_make_reclaim_cap_impossible(
    tmp_path: Path, monkeypatch
) -> None:
    _known_untracked(monkeypatch)
    monkeypatch.setattr(storage, "AI_MAX_TOTAL_BYTES", 128)
    monkeypatch.setattr(storage, "EPISODIC_MAX_TOTAL_BYTES", 10_000)

    raw = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"r" * 2_000)

    status = storage.workspace_storage_status(tmp_path)

    assert status["ok"] is True
    assert status["authoritative_memory_bytes"] >= 2_000
    assert status["ai_reclaimable_bytes"] <= 128


def test_oversized_untracked_episodic_index_is_actually_reclaimed(
    tmp_path: Path, monkeypatch
) -> None:
    _known_untracked(monkeypatch)
    monkeypatch.setattr(storage, "AI_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage, "EPISODIC_MAX_TOTAL_BYTES", 200)

    tier = tmp_path / ".ai" / "memory" / "episodic" / "audit" / "tier_1.jsonl"
    tier.parent.mkdir(parents=True)
    tier.write_bytes(b"d" * 500)

    result = storage.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert result["episodic"]["removed"] == 1
    assert not tier.parent.exists()
    assert result["status"]["episodic_reclaimable_bytes"] == 0


def test_reclaiming_disposable_index_never_deletes_forget_tombstones(
    tmp_path: Path, monkeypatch
) -> None:
    _known_untracked(monkeypatch)
    monkeypatch.setattr(storage, "AI_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage, "EPISODIC_MAX_TOTAL_BYTES", 200)
    episodic.tombstone_range(tmp_path, "audit", start=10, end=20, reason="privacy request")
    tombstone = episodic._tombstone_path(tmp_path, "audit")
    tier = tmp_path / ".ai" / "memory" / "episodic" / "audit" / "tier_1.jsonl"
    tier.parent.mkdir(parents=True)
    tier.write_bytes(b"d" * 500)

    result = storage.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert not tier.exists()
    assert tombstone.exists()
    assert episodic.read_tombstones(tmp_path, "audit")[0].reason == "privacy request"


def test_tracked_episodic_index_is_a_pin_not_an_unsatisfiable_cap(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        storage,
        "_tracked_top_entries",
        lambda _root, directory: ({"audit"}, True)
        if directory.name == "episodic"
        else (set(), True),
    )
    monkeypatch.setattr(storage, "_referenced_entry_names", lambda _root, _directory, _names: set())
    monkeypatch.setattr(storage, "AI_MAX_TOTAL_BYTES", 128)
    monkeypatch.setattr(storage, "EPISODIC_MAX_TOTAL_BYTES", 200)

    tier = tmp_path / ".ai" / "memory" / "episodic" / "audit" / "tier_1.jsonl"
    tier.parent.mkdir(parents=True)
    tier.write_bytes(b"p" * 500)

    result = storage.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert tier.exists()
    assert result["status"]["episodic_pinned_bytes"] >= 500
    assert result["status"]["episodic_reclaimable_bytes"] == 0


def test_audit_rollup_sidecars_have_a_real_reclaim_path(
    tmp_path: Path, monkeypatch
) -> None:
    _known_untracked(monkeypatch)
    monkeypatch.setattr(storage, "AI_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage, "AUDIT_ROLLUP_MAX_TOTAL_BYTES", 200)

    rollup = tmp_path / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
    rollup.parent.mkdir(parents=True)
    rollup.write_bytes(b"r" * 500)

    result = storage.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert result["audit_rollups"]["removed"] == 1
    assert not rollup.exists()
    assert result["status"]["audit_rollup_reclaimable_bytes"] == 0


def test_non_git_workspace_reclaims_known_disposable_episodic_index(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "_tracked_top_entries", lambda _root, _directory: (set(), False))
    monkeypatch.setattr(storage, "_referenced_entry_names", lambda _root, _directory, _names: set())
    monkeypatch.setattr(storage, "AI_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage, "EPISODIC_MAX_TOTAL_BYTES", 200)
    tier = tmp_path / ".ai" / "memory" / "episodic" / "audit" / "tier_1.jsonl"
    tier.parent.mkdir(parents=True)
    tier.write_bytes(b"d" * 500)

    result = storage.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert result["episodic"]["removed"] == 1
    assert not tier.parent.exists()


def test_non_git_disposable_root_still_honors_keep_pin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "_tracked_top_entries", lambda _root, _directory: (set(), False))
    monkeypatch.setattr(storage, "_referenced_entry_names", lambda _root, _directory, _names: set())
    monkeypatch.setattr(storage, "AI_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage, "EPISODIC_MAX_TOTAL_BYTES", 200)
    source = tmp_path / ".ai" / "memory" / "episodic" / "audit"
    source.mkdir(parents=True)
    (source / "tier_1.jsonl").write_bytes(b"d" * 500)
    (source / ".keep").write_text("", encoding="utf-8")

    result = storage.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert source.exists()
    assert result["episodic"]["bytes_pinned"] >= 500
