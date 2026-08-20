from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import report


ROOT = Path(__file__).resolve().parents[3]


def _write_family(dist: Path, version: str) -> set[str]:
    names = {
        f"code-brain-{version}{suffix}"
        for suffix in report.RELEASE_ARTIFACT_SUFFIXES
    }
    for name in names:
        (dist / name).write_text(name, encoding="utf-8")
    return names


def test_plan_targets_only_stale_code_brain_families(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    current = _write_family(dist, "2.0.0")
    stale = _write_family(dist, "1.9.0") | _write_family(dist, "1.8.0")
    unrelated = {
        "notes.txt",
        "release-gate.summary.json",
        "dep-advisory.json",
        "custom-build.tar.gz",
    }
    for name in unrelated:
        (dist / name).write_text("keep", encoding="utf-8")

    plan = report.release_retention_plan(dist, "2.0.0")

    assert set(plan["current_files"]) == current
    assert set(plan["stale_files"]) == stale
    assert set(plan["unrelated_files"]) == unrelated
    assert plan["stale_versions"] == ["1.8.0", "1.9.0"]
    assert plan["clean"] is False
    assert set(path.name for path in dist.iterdir()) == current | stale | unrelated


def test_apply_removes_only_stale_family(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    current = _write_family(dist, "2.0.0")
    stale = _write_family(dist, "1.9.0")
    unrelated = {"dep-advisory.json", "custom-build.tar.gz"}
    for name in unrelated:
        (dist / name).write_text("keep", encoding="utf-8")

    applied = report.apply_release_retention(dist, "2.0.0")

    assert set(applied["removed"]) == stale
    assert applied["clean"] is True
    assert set(path.name for path in dist.iterdir()) == current | unrelated
    assert report.release_retention_plan(dist, "2.0.0")["clean"] is True


def test_matching_non_regular_candidate_is_rejected(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "code-brain-1.0.0.manifest.json").mkdir()

    with pytest.raises(ValueError, match="non-regular release artifact candidate"):
        report.release_retention_plan(dist, "2.0.0")


def test_retention_entry_bound_is_enforced(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setattr(report, "RELEASE_RETENTION_MAX_DIST_ENTRIES", 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (dist / name).write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="dist entry limit exceeded"):
        report.release_retention_plan(dist, "2.0.0")


def test_package_and_release_gate_wire_retention_contract() -> None:
    package = (ROOT / "scripts" / "package.sh").read_text(encoding="utf-8")
    gate = (ROOT / "scripts" / "release-gate.sh").read_text(encoding="utf-8")

    assert "apply_release_retention" in package
    assert "release_retention_plan" in package
    assert "release_retention_plan" in gate
    assert 'plan["clean"]' in gate
