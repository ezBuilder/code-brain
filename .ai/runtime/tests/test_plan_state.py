"""G2: durable per-plan step state machine (checkbox = state, re-derived from disk)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import plan_state as ps


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_parse_steps_pure() -> None:
    text = "# Plan\n\n## Steps\n\n- [ ] write parser\n- [x] read file\nnoise line\n* [X] render\n"
    steps = ps.parse_steps(text)
    assert [s["label"] for s in steps] == ["write parser", "read file", "render"]
    assert [s["done"] for s in steps] == [False, True, True]


def test_init_show_check_roundtrip(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    st = ps.init_plan(root, plan_id="feat-x", steps=["a", "b", "c"], title="Feature X")
    assert st["total"] == 3 and st["completed"] == 0 and st["next_label"] == "a"
    ps.mark_step(root, plan_id="feat-x", match="b")
    st2 = ps.read_plan(root, "feat-x")           # re-derived from disk
    assert st2["completed"] == 1 and st2["next_label"] == "a"
    assert [s["done"] for s in st2["steps"]] == [False, True, False]


def test_check_by_index_and_undo(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    ps.init_plan(root, plan_id="p1", steps=["one", "two"])
    ps.mark_step(root, plan_id="p1", index=1)
    assert ps.read_plan(root, "p1")["completed"] == 1
    ps.mark_step(root, plan_id="p1", index=1, done=False)
    assert ps.read_plan(root, "p1")["completed"] == 0


def test_init_refuses_clobber_without_force(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    ps.init_plan(root, plan_id="p1", steps=["a"])
    assert ps.init_plan(root, plan_id="p1", steps=["b"])["reason"] == "plan_exists"
    assert ps.init_plan(root, plan_id="p1", steps=["b"], force=True)["ok"] is True


def test_invalid_plan_id_rejected(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    with pytest.raises(ValueError):
        ps.read_plan(root, "../etc/passwd")


def test_active_summary_picks_remaining(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    ps.init_plan(root, plan_id="done-plan", steps=["x"])
    ps.mark_step(root, plan_id="done-plan", index=1)         # fully complete → not active
    ps.init_plan(root, plan_id="live-plan", steps=["y", "z"])
    active = ps.active_summary(root)
    assert active is not None and active["plan_id"] == "live-plan" and active["remaining"] == 2


def test_label_redaction(tmp_path: Path) -> None:
    # use a home-path secret (redacted by redact_value, but NOT a secret_scan SECRET_PATTERN hit,
    # so this test file never trips the repo secret scan / allowlist invariant).
    root = _seed(tmp_path)
    st = ps.init_plan(root, plan_id="p1", steps=["open /Users/alice/private/key now"])
    assert "/Users/alice" not in st["steps"][0]["label"]


def test_context_line_surfaces_active_plan(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    from ai_core import autonomous_harness as ah
    assert "plan " not in ah.context_line(root)            # none yet
    ps.init_plan(root, plan_id="feat-x", steps=["a", "b"])
    ps.mark_step(root, plan_id="feat-x", index=1)
    line = ah.context_line(root)
    assert "plan feat-x: 1/2 done" in line and "next: b" in line
