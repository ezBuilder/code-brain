"""Falsifiable, re-testable failure decisions — version/time-scoped, never permanent bans."""
from __future__ import annotations

from pathlib import Path

from ai_core import hooks
from ai_core import loop_engineering as le
from ai_core import memory


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_legacy_decision_byte_identical(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="plain decision", tags=["x"], source="op")["record"]
    assert set(rec.keys()) == {"id", "decided_at", "decision", "tags", "source"}  # no kind:null churn
    assert rec["id"].startswith("dec-")


def test_failure_fields_redacted_and_keys_redacted(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rec = memory.append_decision(
        root, text="fp8 fails", kind="failure",
        environment="/Users/alice/proj",
        observed_versions={"/Users/alice/torch": "2.4.0", "comfyui": "tok=sk-ant-AAAAAAAAAAAAAAAAAAAA"},
    )["record"]
    assert rec["kind"] == "failure" and rec["status"] == "observed"
    assert "/Users/alice" not in rec["environment"]
    # both key and value redaction applied
    joined = " ".join(f"{k} {v}" for k, v in rec["observed_versions"].items())
    assert "/Users/alice" not in joined and "sk-ant-" not in joined


def test_unknown_kind_coerced_to_decision(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="x", kind="fail")["record"]
    assert "kind" not in rec  # coerced to plain decision, no failure keys


def test_surface_partition_and_fold(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="plain A")
    f = memory.append_decision(root, text="fp8 broke", kind="failure",
                               observed_versions={"torch": "2.4.0"})["record"]
    plain, failures = memory.read_decisions_for_surface(root, limit=5)
    assert [p["decision"] for p in plain] == ["plain A"]
    assert len(failures) == 1 and failures[0]["id"] == f["id"]


def test_supersede_retires_original_full_file(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    f = memory.append_decision(root, text="fp8 broke", kind="failure",
                               observed_versions={"torch": "2.4.0"})["record"]
    # many intervening lines to prove full-file fold (not just a tail window)
    for i in range(40):
        memory.append_decision(root, text=f"noise {i}")
    memory.append_decision(root, text="fp8 works now", kind="failure", status="refuted",
                           supersedes_id=f["id"])
    _, failures = memory.read_decisions_for_surface(root, limit=5)
    assert all(x["id"] != f["id"] for x in failures)  # retired, gone from surface
    # original line still physically present (append-only)
    raw = (root / ".ai" / "memory" / "decisions.jsonl").read_text()
    assert raw.count(f["id"]) >= 2


def test_render_failure_no_ban_words(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    entry = {"decision": "x" * 250, "kind": "failure", "status": "observed",
             "observed_at": "2026-06-13", "observed_versions": {"torch": "2.4.0"},
             "environment": "M4 Max MPS"}
    lines = hooks._render_failure_lines(entry, {}, "2026-06-13")
    blob = "\n".join(lines)
    assert "FAILURE as-of 2026-06-13" in blob
    assert "torch=2.4.0" in blob  # version evidence survives long decision (per-segment clamp)
    assert "not a permanent" in blob
    for ban in ("절대 금지", "never use", "dont-use"):
        assert ban not in blob


def test_retest_flag_version_diff(tmp_path: Path) -> None:
    e = {"observed_versions": {"torch": "2.4.0"}}
    assert hooks._failure_retest_flag(e, {"torch": "2.5.0"}, "2026-06-13") == "retest"
    assert hooks._failure_retest_flag(e, {"torch": "2.4.0"}, "2026-06-13") == "fresh"
    assert hooks._failure_retest_flag(e, {}, "2026-06-13") == "unknown"


def test_retest_after_backstop(tmp_path: Path) -> None:
    assert hooks._failure_retest_flag({"retest_after": "2026-01-01"}, {}, "2026-06-13") == "retest"
    assert hooks._failure_retest_flag({"retest_after": "2027-01-01"}, {}, "2026-06-13") == "unknown"


def test_conflicting_decisions_skips_retired_failure(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    f = memory.append_decision(root, text="alpha beta gamma delta epsilon", kind="failure",
                               observed_versions={"torch": "2.4.0"})["record"]
    memory.append_decision(root, text="alpha beta gamma delta epsilon", kind="failure",
                           status="refuted", supersedes_id=f["id"])
    hits = le._conflicting_decisions(root, "alpha beta gamma delta epsilon")
    assert hits == []  # retired failure is not a contradiction source
