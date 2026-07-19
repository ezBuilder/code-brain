from __future__ import annotations

from pathlib import Path

from ai_core import doctor


def test_doctor_reports_full_and_incremental_scan_counts(tmp_path: Path) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    original = doctor.secret_scan_files
    doctor.secret_scan_files = lambda _root, **_kwargs: [source]
    try:
        full = doctor.check_secret_scan(tmp_path, incremental=False, update_state=True)
        incremental = doctor.check_secret_scan(tmp_path, incremental=True, update_state=False)
    finally:
        doctor.secret_scan_files = original

    assert "mode=full baseline=provided total=1 reused=0 rescanned=1" in full.detail
    assert "mode=incremental baseline=provided total=1 reused=1 rescanned=0" in incremental.detail