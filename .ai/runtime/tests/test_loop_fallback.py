"""G4: transient-fault per-task fallback re-queue + classifier + worker derank."""
from __future__ import annotations

from pathlib import Path

from ai_core import error_classifier as ec
from ai_core import loop_engineering as le
from ai_core import loopd
from ai_core import worker_registry as wr


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    le.ensure_loop_dirs(tmp_path)
    return tmp_path


def _qfiles(root: Path, name: str) -> list[Path]:
    return sorted((le.loop_root(root) / name).glob("*.json"))


# --- classifier ---------------------------------------------------------------

def test_classifier_transient_vs_fatal() -> None:
    assert ec.is_transient_fault("OpenAI API rate limit exceeded (429)")
    assert ec.is_transient_fault("server overloaded, try again")
    assert ec.is_transient_fault("503 service unavailable")
    assert ec.is_transient_fault("쿼터 초과")
    assert not ec.is_transient_fault("TypeError: undefined is not a function")
    assert not ec.is_transient_fault("blocked: approval required")
    assert not ec.is_transient_fault("")


# --- re-queue vs dead-letter --------------------------------------------------

def _claim_fail(root: Path, rid: str, *, agent: str, reason: str) -> dict:
    claimed = le.claim(root, orchestrator_id="loopd", agent=agent, request_id=rid,
                       routed_tier="balanced", routed_agent=agent)
    lease = claimed["request"]["lease_id"]
    return le.fail(root, request_id=rid, lease_id=lease, reason=reason)


def test_transient_fail_requeues_and_records_tried(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=False)["request"]["id"]
    out = _claim_fail(root, rid, agent="codex", reason="rate limit 429")
    assert out.get("requeued") is True
    assert not _qfiles(root, "dead")           # not dead-lettered
    inbox = _qfiles(root, "inbox")
    assert len(inbox) == 1
    import json
    req = json.loads(inbox[0].read_text())
    assert req["status"] == "pending"
    assert req["tried_agents"] == ["codex"]
    assert "lease_id" not in req               # lease fields stripped on re-queue


def test_fatal_fail_dead_letters(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=False)["request"]["id"]
    out = _claim_fail(root, rid, agent="codex", reason="TypeError: bad code")
    assert not out.get("requeued")
    assert len(_qfiles(root, "dead")) == 1
    assert not _qfiles(root, "inbox")


def test_transient_requeue_bounded_by_max_attempts(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    rid = le.submit(root, instruction="x", goal="y", reviewer_required=False)["request"]["id"]
    landed_dead = False
    for _ in range(le.MAX_ATTEMPTS + 2):
        if not _qfiles(root, "inbox"):
            break
        out = _claim_fail(root, rid, agent="codex", reason="overloaded 503")
        if not out.get("requeued"):
            landed_dead = True
            break
    assert landed_dead
    assert len(_qfiles(root, "dead")) == 1
    assert not _qfiles(root, "inbox")


# --- worker selection + release ----------------------------------------------

def test_select_worker_deranks_tried_agent(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="%1", state="idle",
                       risk_tier_allowed=["low", "medium"])
    wr.register_worker(root, worker_id="claude-1", agent="claude", pane_id="%2", state="idle",
                       risk_tier_allowed=["low", "medium"])
    request = {"id": "loop-1-abcd", "goal": "do x", "instruction": "do x", "category": "standard",
               "dispatch": {"preferred_agents": ["codex", "claude"]}, "tried_agents": ["codex"]}
    chosen = loopd.select_worker(root, request)
    assert chosen is not None and chosen["agent"] == "claude"  # tried codex deranked


def test_release_for_request_frees_and_marks_quota(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="%1", state="idle")
    wr.set_state(root, worker_id="codex-1", state="assigned", request_id="loop-1-abcd")
    res = wr.release_for_request(root, request_id="loop-1-abcd", quota_exhausted=True)
    assert res["released"] == ["codex-1"]
    w = wr.get_worker(root, "codex-1")
    assert w["state"] == "idle" and w["current_request_id"] is None
    assert w["usage"]["quota_state"] == "quota_exhausted"
