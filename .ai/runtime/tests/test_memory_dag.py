"""Memory DAG edges: optional contradicts/derives_from/expires_at relationships (opt-in)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_core import memory


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _iso(delta_days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return dt.isoformat().replace("+00:00", "Z")


def test_legacy_decision_byte_identical_no_edge_keys(tmp_path: Path) -> None:
    """Default behavior unchanged: a plain decision gets no DAG-edge keys."""
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="plain decision", tags=["x"], source="op")["record"]
    assert set(rec.keys()) == {"id", "decided_at", "decision", "tags", "source"}


def test_edges_stored_only_when_given(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    base = memory.append_decision(root, text="base choice")["record"]
    rec = memory.append_decision(
        root,
        text="new choice",
        contradicts=base["id"],
        derives_from=base["id"],
    )["record"]
    assert rec["contradicts"] == base["id"]
    assert rec["derives_from"] == base["id"]
    # base record itself still carries no edge keys
    assert "contradicts" not in base and "derives_from" not in base


def test_malformed_edge_ids_ignored_fail_soft(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rec = memory.append_decision(
        root,
        text="x",
        contradicts="not-an-id",
        derives_from="todo-deadbeef",  # wrong prefix
        expires_at="",
    )["record"]
    assert "contradicts" not in rec
    assert "derives_from" not in rec
    assert "expires_at" not in rec


def test_contradicts_derives_from_round_trip_via_filtered(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    base = memory.append_decision(root, text="base zeta", source="op")["record"]
    memory.append_decision(
        root, text="follow zeta", source="op",
        contradicts=base["id"], derives_from=base["id"],
    )
    out = memory.read_decisions_filtered(root, text="follow zeta")
    assert out["count"] == 1
    item = out["items"][0]
    assert item["contradicts"] == base["id"]
    assert item["derives_from"] == base["id"]


def test_expired_decision_excluded_from_surface(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="still valid")
    memory.append_decision(root, text="time-boxed", expires_at=_iso(-1))
    plain, _ = memory.read_decisions_for_surface(root, limit=10)
    texts = [p["decision"] for p in plain]
    assert "still valid" in texts
    assert "time-boxed" not in texts
    # include_expired re-admits it
    plain_all, _ = memory.read_decisions_for_surface(root, limit=10, include_expired=True)
    assert "time-boxed" in [p["decision"] for p in plain_all]


def test_future_expiry_still_surfaces(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="future bound", expires_at=_iso(30))["record"]
    assert rec["expires_at"]  # stored
    plain, _ = memory.read_decisions_for_surface(root, limit=10)
    assert "future bound" in [p["decision"] for p in plain]


def test_expired_excluded_from_filtered_unless_flag(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="expired filtered", expires_at=_iso(-2))
    assert memory.read_decisions_filtered(root, text="expired filtered")["count"] == 0
    out = memory.read_decisions_filtered(root, text="expired filtered", include_expired=True)
    assert out["count"] == 1


def test_expired_failure_excluded_from_surface(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(
        root, text="fp8 broke", kind="failure",
        observed_versions={"torch": "2.4.0"}, expires_at=_iso(-1),
    )
    _, failures = memory.read_decisions_for_surface(root, limit=10)
    assert failures == []
    _, failures_all = memory.read_decisions_for_surface(root, limit=10, include_expired=True)
    assert len(failures_all) == 1
