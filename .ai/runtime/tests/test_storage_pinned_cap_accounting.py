"""Storage caps apply to RECLAIMABLE bytes, not to bytes the enforcer may not delete.

The silent bug: `pinned` is a deletion veto (tracked in git, referenced by tracked source,
or an explicit `.keep`), but pinned bytes were still counted against the cap. Measured on
blurivo: .ai/tmp held 546MB of which 475MB were three user fixtures carrying explicit
`.keep` markers. Every enforce run deleted everything it was allowed to and still returned
ok=False, so `doctor` stayed permanently red and no user action short of deleting their own
fixtures could clear it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_core import storage_lifecycle as SL  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    (tmp_path / ".ai" / "tmp").mkdir(parents=True)
    (tmp_path / ".ai" / "outputs").mkdir(parents=True)
    return tmp_path


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_pinned_bytes_excluded_from_cap(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SL, "TMP_MAX_TOTAL_BYTES", 1000)
    big = repo / ".ai" / "tmp" / "fixture.bin"
    _write(big, 5000)
    (repo / ".ai" / "tmp" / "fixture.bin.keep").write_text("", encoding="utf-8")

    status = SL.workspace_storage_status(repo)
    assert status["tmp_pinned_bytes"] >= 5000
    assert status["tmp_reclaimable_bytes"] == 0
    assert status["ok"] is True, status
    # The absolute figure is still reported for transparency.
    assert status["tmp_bytes"] >= 5000


def test_unpinned_overflow_still_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SL, "TMP_MAX_TOTAL_BYTES", 1000)
    _write(repo / ".ai" / "tmp" / "junk.bin", 5000)
    status = SL.workspace_storage_status(repo)
    assert status["tmp_pinned_bytes"] == 0
    assert status["tmp_reclaimable_bytes"] >= 5000
    assert status["ok"] is False, status


def test_enforce_reports_ok_when_only_pinned_remains(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The blurivo shape: a sweep that removed all it could must not report failure."""
    monkeypatch.setattr(SL, "TMP_MAX_TOTAL_BYTES", 1000)
    _write(repo / ".ai" / "tmp" / "keepme.bin", 4000)
    (repo / ".ai" / "tmp" / "keepme.bin.keep").write_text("", encoding="utf-8")
    _write(repo / ".ai" / "tmp" / "deleteme.bin", 4000)

    result = SL.enforce_workspace_storage(repo)
    assert (repo / ".ai" / "tmp" / "keepme.bin").exists(), "pinned entry must survive"
    assert not (repo / ".ai" / "tmp" / "deleteme.bin").exists(), "unpinned overflow must go"
    assert result["ok"] is True, result
    assert int(result["tmp"]["bytes_pinned"]) >= 4000
    assert int(result["tmp"]["bytes_reclaimable"]) == 0


def test_enforce_still_fails_when_reclaimable_overflow_cannot_be_removed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Genuine failure must stay visible: an unremovable UNPINNED entry is a real error."""
    monkeypatch.setattr(SL, "TMP_MAX_TOTAL_BYTES", 1000)
    _write(repo / ".ai" / "tmp" / "stuck.bin", 5000)
    monkeypatch.setattr(SL, "_remove_managed_entry", lambda path, *, root: False)
    result = SL.enforce_workspace_storage(repo)
    assert result["ok"] is False, result


def test_referenced_and_tracked_entries_count_as_pinned(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SL, "TMP_MAX_TOTAL_BYTES", 1000)
    _write(repo / ".ai" / "tmp" / "referenced_asset_9s.mp4", 4000)
    (repo / "src.py").write_text("PATH = '.ai/tmp/referenced_asset_9s.mp4'\n", encoding="utf-8")
    _git(repo, "add", "src.py")
    _git(repo, "commit", "-q", "-m", "reference")

    status = SL.workspace_storage_status(repo)
    assert status["tmp_pinned_bytes"] >= 4000, status
    assert status["ok"] is True, status


def test_pinned_bytes_helper_is_soft_on_missing_dir(repo: Path) -> None:
    assert SL._pinned_bytes(repo, repo / ".ai" / "does-not-exist") == 0


def test_status_exposes_all_accounting_keys(repo: Path) -> None:
    status = SL.workspace_storage_status(repo)
    for key in (
        "tmp_bytes", "tmp_pinned_bytes", "tmp_reclaimable_bytes",
        "output_bytes", "output_pinned_bytes", "output_reclaimable_bytes",
        "ai_bytes", "ai_reclaimable_bytes",
    ):
        assert key in status, key


def test_non_git_workspace_still_enforces_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed git lookup must NOT be laundered into a user pin.

    `tracked_known=False` (no repo / git missing / oversized listing) means "deletion is
    withheld out of caution", not "the user asked to keep this". Conflating the two would
    mark every entry pinned and silently disable quota enforcement for every non-git
    workspace — caps would never fire again.
    """
    monkeypatch.setattr(SL, "TMP_MAX_TOTAL_BYTES", 100)
    tmp = tmp_path / ".ai" / "tmp"
    tmp.mkdir(parents=True)
    (tmp / "oversized.bin").write_bytes(b"x" * 200)

    rows, _errors = SL._managed_entries(tmp_path, tmp)
    assert rows[0]["pinned"] is False
    assert rows[0]["undetermined"] is True
    assert SL._pinned_bytes(tmp_path, tmp) == 0

    status = SL.workspace_storage_status(tmp_path)
    assert status["tmp_reclaimable_bytes"] >= 200
    assert status["ok"] is False, status


def test_undetermined_entries_are_never_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Withholding deletion is the whole point of `undetermined`; keep that guarantee."""
    monkeypatch.setattr(SL, "TMP_MAX_TOTAL_BYTES", 100)
    tmp = tmp_path / ".ai" / "tmp"
    tmp.mkdir(parents=True)
    target = tmp / "oversized.bin"
    target.write_bytes(b"x" * 200)

    SL.enforce_workspace_storage(tmp_path)
    assert target.exists(), "an undetermined entry must survive enforcement"


def test_protected_helper_covers_both_reasons() -> None:
    assert SL._protected({"pinned": True, "undetermined": False}) is True
    assert SL._protected({"pinned": False, "undetermined": True}) is True
    assert SL._protected({"pinned": False, "undetermined": False}) is False
