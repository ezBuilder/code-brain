"""Worker launch wrapper — isolated tmux launch, no secrets, fixed binaries."""
from __future__ import annotations

from pathlib import Path

from ai_core import worker_launch as wl
from ai_core import worker_profiles as wp
from ai_core import worker_registry as wr
from ai_core.tmux_adapter import FakeTmuxAdapter


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_plan_resolves_isolated_env_no_secrets(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wp.register_profile(root, profile="agy-pro-1", agent="agy",
                        env={"AWS_SECRET_ACCESS_KEY": "leak", "CB_FLAG": "ok"})
    plan = wl.build_launch_plan(root, worker_id="agy-1", agent="agy", profile="agy-pro-1",
                                session="cb-x", window="agy-1")
    assert plan["ok"] and plan["command"].startswith("agy")
    assert plan["env"]["HOME"].endswith("agy-pro-1/home")
    assert "AWS_SECRET_ACCESS_KEY" not in plan["env"]
    assert all("leak" not in v for v in plan["env"].values())


def test_unknown_agent_refused(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    plan = wl.build_launch_plan(root, worker_id="x", agent="rogue", profile="p",
                                session="cb-x", window="w")
    assert plan["ok"] is False and "unknown agent" in plan["reason"]


def test_invalid_session_refused(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    plan = wl.build_launch_plan(root, worker_id="x", agent="codex", profile="p",
                                session="bad;rm -rf", window="w")
    assert plan["ok"] is False and "session" in plan["reason"]


def test_dry_run_does_not_spawn_or_register(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    res = wl.launch_worker(root, worker_id="codex-1", agent="codex", profile="codex-1", dry_run=True)
    assert res["ok"] and res["dry_run"] is True
    assert wr.list_workers(root) == []  # nothing registered on a dry run


def test_launch_with_fake_adapter_registers_and_boots(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    adapter = FakeTmuxAdapter()
    res = wl.launch_worker(root, worker_id="codex-1", agent="codex", profile="codex-1",
                           adapter=adapter, dry_run=False)
    assert res["ok"] and res["pane_id"]
    w = wr.get_worker(root, "codex-1")
    assert w["state"] == "idle" and w["tmux"]["pane_id"] == res["pane_id"]
    # boot prompt injected, never a secret
    assert any("Code Brain worker codex-1" in i["text"] for i in adapter.injected)
    # the launch command exported only path envs
    launched = adapter.launched[0]
    assert launched["command"] == "codex"
    assert all(k in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME") or k.startswith("CODE_BRAIN_")
               for k in launched["env"])


def test_pool_idempotent(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    wp.register_profile(root, profile="codex-1", agent="codex", worker_id="codex-1")
    adapter = FakeTmuxAdapter()
    first = wl.launch_pool(root, adapter=adapter, dry_run=False)
    assert first["launched"][0]["ok"]
    second = wl.launch_pool(root, adapter=adapter, dry_run=False)
    assert second["launched"][0].get("skipped") == "already up"


def test_launch_skips_uninstalled_agent(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    import ai_core.worker_launch as _wl
    # pretend no agent CLI is installed
    monkeypatch.setattr(_wl, "agent_available", lambda a: False)
    monkeypatch.setattr(_wl, "tmux_available", lambda: True)
    res = _wl.launch_worker(root, worker_id="codex-1", agent="codex", profile="codex-1",
                            inherit_auth=True, dry_run=False)
    assert res["ok"] is False and res.get("skipped") is True and "not installed" in res["reason"]


def test_launch_skips_when_no_tmux(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    import ai_core.worker_launch as _wl
    monkeypatch.setattr(_wl, "tmux_available", lambda: False)
    res = _wl.launch_worker(root, worker_id="codex-1", agent="codex", profile="codex-1",
                            inherit_auth=True, dry_run=False)
    assert res["ok"] is False and "tmux" in res["reason"]


def test_capabilities_shape(tmp_path: Path) -> None:
    import ai_core.worker_launch as _wl
    cap = _wl.capabilities()
    assert set(cap["agents"]) == {"codex", "claude", "agy"}
    assert isinstance(cap["tmux"], bool) and isinstance(cap["available_agents"], list)
