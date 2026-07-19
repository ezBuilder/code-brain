from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import doctor, obs, transcripts  # noqa: E402


def _make_repo(tmp_path: Path) -> Path:
    config = tmp_path / ".ai" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("version: 1\n", encoding="utf-8")
    return tmp_path


def test_doctor_diagnostics_skips_expensive_transcript_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_usage(_root: Path) -> dict:
        raise AssertionError("doctor diagnostics must not scan agent transcripts")

    monkeypatch.setattr(transcripts, "claude_usage_summary", unexpected_usage)
    monkeypatch.setattr(transcripts, "codex_usage_summary", unexpected_usage)

    result = doctor.check_diagnostics(_make_repo(tmp_path))

    assert result.ok is True


def test_explicit_diagnostics_keeps_full_usage_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transcripts,
        "claude_usage_summary",
        lambda _root: {
            "ok": True,
            "source": "test",
            "sessions_scanned": 1,
            "sessions_matched": 1,
            "messages": 2,
            "tokens": {},
            "total_observed_tokens": 3,
        },
    )
    monkeypatch.setattr(
        transcripts,
        "codex_usage_summary",
        lambda _root: {"ok": True, "source": "test", "sessions_scanned": 1},
    )

    payload = obs.diagnostics(_make_repo(tmp_path), dry_run=True, include_doctor=False)

    assert payload["bundle"]["metrics"]["usage"]["claude"]["sessions_scanned"] == 1
    assert payload["bundle"]["metrics"]["usage"]["codex"]["sessions_scanned"] == 1


def test_lightweight_diagnostics_marks_usage_as_skipped(tmp_path: Path) -> None:
    payload = obs.diagnostics(
        _make_repo(tmp_path),
        dry_run=True,
        include_doctor=False,
        include_usage=False,
    )

    assert payload["bundle"]["metrics"]["usage"] == {
        "skipped": True,
        "reason": "lightweight diagnostics smoke check",
    }


def test_lightweight_session_doctor_defers_full_diagnostics_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_diagnostics(_root: Path):
        raise AssertionError("lightweight session must defer full diagnostics smoke")

    monkeypatch.setattr(doctor, "check_diagnostics", unexpected_diagnostics)
    checks = doctor.run_checks(
        tmp_path,
        precomputed_index_status={
            "ok": True,
            "reason": "current",
            "changed_paths": [],
            "indexed_files": 0,
        },
        precomputed_session_start_ms=5,
        lightweight=True,
    )

    diagnostics_check = next(check for check in checks if check.name == "diagnostics_dry_run")
    hot_path_check = next(check for check in checks if check.name == "hot_path_slo")
    assert diagnostics_check.ok is True
    assert diagnostics_check.detail == "deferred: run doctor --strict for full diagnostics smoke"
    assert "p95_ms=deferred" in hot_path_check.detail