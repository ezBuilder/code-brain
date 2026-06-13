"""loopd control plane — token-free dispatch, approval gate, worker isolation profiles."""
from __future__ import annotations

from pathlib import Path

from ai_core import loop_engineering as le
from ai_core import loopd
from ai_core import worker_profiles as wp
from ai_core import worker_registry as wr
from ai_core.tmux_adapter import FakeTmuxAdapter


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    le.ensure_loop_dirs(tmp_path)
    return tmp_path


def test_empty_queue_zero_llm_polls(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    out = loopd.dispatch_once(root, adapter=FakeTmuxAdapter())
    assert out["dispatched"] == [] and out["llm_idle_polls"] == 0


def test_dispatch_to_idle_worker(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="%1", state="idle",
                       risk_tier_allowed=["low", "medium"])
    adapter = FakeTmuxAdapter(alive={"%1"})
    le.submit(root, instruction="add a focused test", goal="test work", reviewer_required=False)
    out = loopd.dispatch_once(root, adapter=adapter)
    assert len(out["dispatched"]) == 1 and out["dispatched"][0]["worker_id"] == "codex-1"
    assert adapter.injected and "loop claim" in adapter.injected[0]["text"]
    assert wr.get_worker(root, "codex-1")["state"] == "assigned"


def test_high_risk_is_parked_not_dispatched(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="%1", state="idle",
                       risk_tier_allowed=["low", "medium", "high"])
    adapter = FakeTmuxAdapter(alive={"%1"})
    le.submit(root, instruction="deploy to production and rotate the secret token",
              goal="prod deploy", reviewer_required=False)
    out = loopd.dispatch_once(root, adapter=adapter)
    assert out["dispatched"] == [] and len(out["blocked"]) == 1
    assert not adapter.injected  # never injected a gated task
    # idempotent: second pass does not re-block or dispatch
    out2 = loopd.dispatch_once(root, adapter=adapter)
    assert out2["blocked"] == [] and out2["dispatched"] == []


def test_no_idle_worker_skips_silently(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="%1", state="working")
    le.submit(root, instruction="x", goal="y", reviewer_required=False)
    out = loopd.dispatch_once(root, adapter=FakeTmuxAdapter(alive={"%1"}))
    assert out["dispatched"] == [] and out["skipped"] == 1 and out["llm_idle_polls"] == 0


def test_status_counts(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="agy-1", agent="agy", state="idle")
    le.submit(root, instruction="x", goal="y", reviewer_required=False)
    st = loopd.status(root)
    assert st["queue"]["pending"] == 1 and st["workers"]["total"] == 1 and st["llm_idle_polls"] == 0


def test_profile_isolation_paths_no_secrets(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    res = wp.register_profile(root, profile="agy-pro-1", agent="agy",
                              env={"CODE_BRAIN_WORKER_ID": "agy-1", "OPENAI_API_KEY": "sk-leak"})
    prof = res["profile"]
    assert "agy-pro-1" in prof["home"]
    # secret-bearing env keys are refused
    assert "OPENAI_API_KEY" not in prof["env"]
    env = wp.resolve_profile_env(root, "agy-pro-1")
    assert env["HOME"].endswith("agy-pro-1/home")
    assert all("sk-leak" not in v for v in env.values())
    # 0700 perms on the home dir
    import os
    mode = os.stat(prof["home"]).st_mode & 0o777
    assert mode == 0o700


def test_gated_keyword_in_any_field_is_blocked(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="%1", state="idle",
                       risk_tier_allowed=["low", "medium", "high"])
    # innocuous goal/instruction but a gated word hidden in dispatch metadata
    req = {"id": "loop-1-abcd", "goal": "tidy", "instruction": "tidy up",
           "dispatch": {"required_capabilities": ["rm -rf /"]}}
    assert loopd.infer_risk(req) == "high"


def test_non_pane_target_refused(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    # pane_id forged to another session — must never be injected
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="victim:0", state="idle")
    adapter = FakeTmuxAdapter(alive={"victim:0"})
    le.submit(root, instruction="x", goal="y", reviewer_required=False)
    out = loopd.dispatch_once(root, adapter=adapter)
    assert out["dispatched"] == [] and not adapter.injected


def test_update_worker_rejects_non_allowlisted_field(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", state="idle")
    r = wr.update_worker(root, worker_id="codex-1", risk_tier_allowed=["high"])
    assert r["ok"] is False and r["reason"] == "field_not_updatable"


def test_recovery_flags_stale_worker(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", state="working")
    wr.update_worker(root, worker_id="codex-1", heartbeat_at="2020-01-01T00:00:00Z")
    out = loopd.recovery_tick(root)
    assert "codex-1" in out["stale_workers"]
    assert wr.get_worker(root, "codex-1")["state"] == "stale"
