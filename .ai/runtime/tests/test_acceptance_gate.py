"""G1: deterministic acceptance re-run gates verified completion + opt-in typed evidence."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import acceptance
from ai_core import loop_engineering as le


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    le.ensure_loop_dirs(tmp_path)
    return tmp_path


def _claim(root: Path, rid: str) -> str:
    return le.claim(root, orchestrator_id="loopd", agent="codex", request_id=rid)["request"]["lease_id"]


# --- run_acceptance -----------------------------------------------------------

def test_run_acceptance_pass_fail_empty(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    assert acceptance.run_acceptance(root, commands=["true"])["all_passed"] is True
    assert acceptance.run_acceptance(root, commands=["false"])["all_passed"] is False
    assert acceptance.run_acceptance(root, commands=["true", "false"])["all_passed"] is False
    assert acceptance.run_acceptance(root, commands=[])["all_passed"] is False  # nothing verified


# --- completion gate ----------------------------------------------------------

def test_acceptance_required_blocks_complete_without_run(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=False,
                    acceptance_required=True)["request"]["id"]
    lease = _claim(root, rid)
    with pytest.raises(le.LoopPhaseError):
        le.complete(root, request_id=rid, lease_id=lease, summary="done")


def test_acceptance_pass_allows_complete(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=False,
                    acceptance_required=True)["request"]["id"]
    lease = _claim(root, rid)
    acc = le.record_acceptance(root, request_id=rid, lease_id=lease, commands=["true"])
    assert acc["acceptance"]["all_passed"] is True
    out = le.complete(root, request_id=rid, lease_id=lease, summary="done")
    assert out["ok"] and not out.get("requeued")


def test_acceptance_fail_still_blocks_complete(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=False,
                    acceptance_required=True)["request"]["id"]
    lease = _claim(root, rid)
    le.record_acceptance(root, request_id=rid, lease_id=lease, commands=["false"])
    with pytest.raises(le.LoopPhaseError):
        le.complete(root, request_id=rid, lease_id=lease, summary="done")


def test_non_acceptance_request_unaffected(tmp_path: Path) -> None:
    """self_improve-style work (no reviewer, no acceptance) completes as before."""
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=False)["request"]["id"]
    lease = _claim(root, rid)
    out = le.complete(root, request_id=rid, lease_id=lease, summary="done")
    assert out["ok"]


# --- typed evidence on verdict ------------------------------------------------

def test_verdict_evidence_stored_and_redacted(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=True,
                    rubric="r", checklist=["c"])["request"]["id"]
    lease = _claim(root, rid)
    le.record_verdict(root, request_id=rid, lease_id=lease, reviewer="rev", verdict="pass",
                      summary="ok", evidence=[{"command": "pytest -q", "observed": "12 passed",
                                               "artifact_path": "/tmp/log"}, {"command": ""}])
    import json
    req = json.loads((le.loop_root(root) / "processing" / f"{rid}.json").read_text())
    ev = req["reviewer_verdict"]["evidence"]
    assert len(ev) == 1 and ev[0]["command"] == "pytest -q"  # empty item dropped
    assert le._verdict_has_evidence(req) is True
