from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.doctor import check_index_freshness_from_status  # noqa: E402


def test_precomputed_current_index_status_maps_to_passing_check() -> None:
    check = check_index_freshness_from_status(
        {
            "exists": True,
            "indexed": 42,
            "stale": False,
            "reason": "current",
            "changed_paths": [],
        }
    )

    assert check.ok is True
    assert check.detail == "ok indexed=42"


def test_precomputed_hash_mismatch_preserves_changed_paths() -> None:
    check = check_index_freshness_from_status(
        {
            "stale": True,
            "reason": "hash_mismatch",
            "changed_paths": ["src/app.py"],
        }
    )

    assert check.ok is False
    assert check.detail == "stale: src/app.py"