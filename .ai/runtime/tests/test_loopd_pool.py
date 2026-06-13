"""loopd pool: model selection, multi-account isolation, completion→idle, single-line inject."""
from __future__ import annotations

from pathlib import Path

from ai_core import loop_engineering as le
from ai_core import loopd, worker_launch as wl, worker_models as wm
from ai_core import worker_profiles as wp, worker_registry as wr
from ai_core.tmux_adapter import FakeTmuxAdapter


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    le.ensure_loop_dirs(tmp_path)
    return tmp_path


def test_best_model_per_agent(tmp_path: Path) -> None:
    assert wm.resolve_model(tmp_path, "claude")["flags"] == ["--model", "claude-opus-4-8"]
    assert wm.resolve_model(tmp_path, "codex")["model"] == "gpt-5.5"
    wm.set_model(tmp_path, agent="codex", model="gpt-6", flags=["-m", "gpt-6"])
    assert wm.resolve_model(tmp_path, "codex")["flags"] == ["-m", "gpt-6"]


def test_launch_command_includes_model_flags(tmp_path: Path) -> None:
    _seed(tmp_path)
    plan = wl.build_launch_plan(tmp_path, worker_id="claude-1", agent="claude", profile="claude-1",
                               session="cb-x", window="claude-1", inherit_auth=True)
    assert plan["command"] == "claude --model claude-opus-4-8"


def test_multi_account_isolated_homes(tmp_path: Path) -> None:
    _seed(tmp_path)
    a = wp.add_account(tmp_path, agent="claude", account="work")
    b = wp.add_account(tmp_path, agent="claude", account="personal")
    assert a["profile"] != b["profile"]
    ea = wp.resolve_profile_env(tmp_path, a["profile"])
    eb = wp.resolve_profile_env(tmp_path, b["profile"])
    assert ea["HOME"] != eb["HOME"]  # OAuth caches isolated per account
    accounts = wp.list_accounts(tmp_path, agent="claude")
    assert {x["account"] for x in accounts} == {"work", "personal"}
    assert "login_command" in a and "claude" in a["login_command"]


def test_completion_frees_worker_to_idle(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="codex-1", agent="codex", pane_id="%1", state="idle")
    adapter = FakeTmuxAdapter(alive={"%1"})
    sub = le.submit(root, instruction="x", goal="y", reviewer_required=False)["request"]
    loopd.dispatch_once(root, adapter=adapter)
    assert wr.get_worker(root, "codex-1")["state"] == "assigned"
    # worker completes the request → moves to done/
    claim = le.claim(root, orchestrator_id="loopd", agent="codex")["request"]
    le.complete(root, request_id=claim["id"], lease_id=claim["lease_id"], summary="done")
    out = loopd.recovery_tick(root)
    assert "codex-1" in out["freed_workers"]
    assert wr.get_worker(root, "codex-1")["state"] == "idle"


def test_inject_collapses_to_single_line(tmp_path: Path) -> None:
    adapter = FakeTmuxAdapter(alive={"%1"})
    adapter.inject("%1", "line one\nline two\n  line three")
    assert "\n" not in adapter.injected[0]["text"]
    assert adapter.injected[0]["text"] == "line one line two line three"
