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


def test_tiered_models_default_is_balanced_not_best(tmp_path: Path) -> None:
    # default tier is cost-aware (balanced), NOT the most expensive model
    assert wm.resolve_model(tmp_path, "claude")["model"] == "claude-sonnet-4-6"
    assert wm.resolve_model(tmp_path, "claude", tier="cheap")["model"] == "claude-haiku-4-5"
    assert wm.resolve_model(tmp_path, "claude", tier="best")["model"] == "claude-opus-4-8"
    assert wm.resolve_model(tmp_path, "agy", tier="best")["model"] == "Gemini 3.1 Pro (High)"
    assert wm.resolve_model(tmp_path, "agy", tier="cheap")["model"].startswith("Gemini 3.5 Flash")


def test_launch_command_uses_tier(tmp_path: Path) -> None:
    _seed(tmp_path)
    bal = wl.build_launch_plan(tmp_path, worker_id="claude-1", agent="claude", profile="claude-1",
                               session="cb-x", window="claude-1", inherit_auth=True)
    assert bal["command"] == "claude --model claude-sonnet-4-6"  # balanced default
    best = wl.build_launch_plan(tmp_path, worker_id="claude-1", agent="claude", profile="claude-1",
                                session="cb-x", window="claude-1", inherit_auth=True, tier="best")
    assert best["command"] == "claude --model claude-opus-4-8"


def test_agy_best_tier_model_quoted(tmp_path: Path) -> None:
    _seed(tmp_path)
    plan = wl.build_launch_plan(tmp_path, worker_id="agy-1", agent="agy", profile="agy-1",
                               session="cb-x", window="agy-1", inherit_auth=True, tier="best")
    # spaced/parenthesized model name is shlex-quoted into one safe argv element
    assert plan["command"] == "agy --model 'Gemini 3.1 Pro (High)'"


def test_autonomous_appends_per_agent_flags(tmp_path: Path) -> None:
    _seed(tmp_path)
    cx = wl.build_launch_plan(tmp_path, worker_id="codex-1", agent="codex", profile="codex-1",
                              session="cb-x", window="codex-1", inherit_auth=True, autonomous=True)
    assert cx["command"].endswith("--dangerously-bypass-approvals-and-sandbox")
    cl = wl.build_launch_plan(tmp_path, worker_id="claude-1", agent="claude", profile="claude-1",
                              session="cb-x", window="claude-1", inherit_auth=True, autonomous=True)
    assert cl["command"].endswith("--permission-mode bypassPermissions")
    # default (no autonomous) keeps no bypass flag
    safe = wl.build_launch_plan(tmp_path, worker_id="codex-1", agent="codex", profile="codex-1",
                                session="cb-x", window="codex-1", inherit_auth=True)
    assert "dangerously" not in safe["command"]


def test_gate_catches_expanded_high_risk(tmp_path: Path) -> None:
    for bad in ("release to production now", "set the OPENAI_API_KEY", "kubectl apply -f x",
                "drop database prod", "terraform apply", "remove all rows from users",
                "gh pr merge 5", "rimraf the build", "rotate the oauth token", "find . -delete"):
        assert loopd.infer_risk({"id": "loop-1-a", "goal": bad}) == "high", bad


def test_override_cannot_smuggle_bypass_flag(tmp_path: Path) -> None:
    _seed(tmp_path)
    wm.set_model(tmp_path, agent="codex", model="x",
                 flags=["--dangerously-bypass-approvals-and-sandbox", "--model", "x"])
    flags = wm.resolve_model(tmp_path, "codex")["flags"]
    assert all("bypass" not in f and "dangerous" not in f for f in flags)


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
    loopd.dispatch_once(root, adapter=adapter)  # loopd claims the request for the worker
    assert wr.get_worker(root, "codex-1")["state"] == "assigned"
    # the request is already in processing with loopd's lease — read it and complete
    import json
    proc = json.loads((le.loop_root(root) / "processing" / f"{sub['id']}.json").read_text())
    le.complete(root, request_id=sub["id"], lease_id=proc["lease_id"], summary="done")
    out = loopd.recovery_tick(root)
    assert "codex-1" in out["freed_workers"]
    assert wr.get_worker(root, "codex-1")["state"] == "idle"


def test_assess_tier_complexity(tmp_path: Path) -> None:
    assert loopd.assess_tier({"id": "loop-1-a", "goal": "fix typo", "instruction": "fix a typo"}) == "cheap"
    assert loopd.assess_tier({"id": "loop-1-a", "goal": "x", "instruction": "refactor the auth module"}) == "best"
    assert loopd.assess_tier({"id": "loop-1-a", "goal": "deploy to prod", "instruction": "deploy"}) == "best"
    medium = ("add a helper function that formats the report header and wire it into the two "
              "call sites; keep the existing style and add a short unit test for the happy path "
              "and one empty-input case, then update the module docstring to mention it briefly")
    assert loopd.assess_tier({"id": "loop-1-a", "goal": "add helper", "instruction": medium}) == "balanced"
    # explicit override wins
    assert loopd.assess_tier({"id": "loop-1-a", "goal": "refactor", "dispatch": {"model_tier": "cheap"}}) == "cheap"


def test_select_worker_routes_cheapest_adequate(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    # a cheap and a best worker are both idle
    wr.register_worker(root, worker_id="agy-cheap", agent="agy", pane_id="%1", state="idle",
                       model={"tier": "cheap"}, risk_tier_allowed=["low", "medium"])
    wr.register_worker(root, worker_id="codex-best", agent="codex", pane_id="%2", state="idle",
                       model={"tier": "best"}, risk_tier_allowed=["low", "medium"])
    # trivial task → cheapest adequate (cheap) wins, sparing the best model
    simple = {"id": "loop-1-a", "goal": "fix typo", "instruction": "fix typo"}
    assert loopd.select_worker(root, simple)["worker_id"] == "agy-cheap"
    # complex task → needs best; only the best-tier worker is adequate
    hard = {"id": "loop-2-b", "goal": "x", "instruction": "refactor the concurrency model"}
    assert loopd.select_worker(root, hard)["worker_id"] == "codex-best"


def test_nudge_clears_benign_interrupt(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wr.register_worker(root, worker_id="agy-1", agent="agy", pane_id="%5", state="working")
    adapter = FakeTmuxAdapter(alive={"%5"})
    adapter.set_output("%5", "...\nHow's the CLI experience so far? Help us improve:\n[0] Skip")
    out = loopd.recovery_tick(root, adapter=adapter)
    assert "agy-1" in out["nudged_workers"]
    # the skip key was sent to the worker pane
    assert any(k["pane_id"] == "%5" and k["key"] == "0" for k in getattr(adapter, "keys", []))


def test_inject_collapses_to_single_line(tmp_path: Path) -> None:
    adapter = FakeTmuxAdapter(alive={"%1"})
    adapter.inject("%1", "line one\nline two\n  line three")
    assert "\n" not in adapter.injected[0]["text"]
    assert adapter.injected[0]["text"] == "line one line two line three"
