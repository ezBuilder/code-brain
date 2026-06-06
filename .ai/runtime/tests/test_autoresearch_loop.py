"""Stage 2 ratchet loop tests — agent-driven; runtime deterministic mechanics.

Default-OFF gate, input validation, and the pure ratchet logic run everywhere; the metric
run (hardened sandbox) is gated on sandbox-exec availability.
"""
from __future__ import annotations

import shutil

import pytest

from ai_core.autoresearch import loop, storage

_HAS_SBX = shutil.which("sandbox-exec") is not None


@pytest.fixture
def proj(tmp_path, monkeypatch):
    storage.ensure_tree(storage.data_root(tmp_path))
    monkeypatch.setattr(loop, "_enabled", lambda root: True)
    return tmp_path


# --- default-OFF gate ---

def test_disabled_by_default(tmp_path):
    r = loop.start(tmp_path, workspace=".", metric_cmd="echo metric: 1",
                   metric_grep=r"metric: ([0-9.]+)", direction="minimize")
    assert r == {"error": "loop_disabled"}


# --- input validation ---

def test_start_validation(proj, tmp_path):
    (tmp_path / "ws").mkdir()
    assert loop.start(proj, workspace="ws", metric_cmd="x", metric_grep="(",
                      direction="minimize")["error"] == "invalid_metric_grep_regex"
    assert loop.start(proj, workspace="ws", metric_cmd="", metric_grep="m",
                      direction="minimize")["error"] == "empty_metric_cmd"
    assert loop.start(proj, workspace="ws", metric_cmd="x", metric_grep="m",
                      direction="sideways")["error"] == "invalid_direction"
    assert loop.start(proj, workspace="../escape", metric_cmd="x", metric_grep="m",
                      direction="minimize")["error"] == "invalid_workspace"


def test_start_ok_and_budget_caps(proj, tmp_path):
    (tmp_path / "ws").mkdir()
    s = loop.start(proj, workspace="ws", metric_cmd=["python3", "-c", "print(1)"],
                   metric_grep=r"([0-9.]+)", direction="maximize", max_iters=99999)
    assert s["session_id"].startswith("lp_")
    assert s["budget"]["max_iters"] == loop._MAX_ITERS_CAP
    assert s["status"] == "running"


# --- pure ratchet logic ---

def test_extract_metric():
    assert loop._extract_metric("blah\nmetric: 0.83 done", r"metric:\s*([0-9.]+)") == 0.83
    assert loop._extract_metric("no match here", r"metric:\s*([0-9.]+)") is None


def test_is_better():
    assert loop._is_better(0.1, None, "minimize")
    assert loop._is_better(0.1, {"metric": 0.2}, "minimize")
    assert not loop._is_better(0.3, {"metric": 0.2}, "minimize")
    assert loop._is_better(0.3, {"metric": 0.2}, "maximize")


def test_record_session_not_found(proj):
    assert loop.record(proj, "lp_000000000000")["error"] == "session_not_found"


def test_start_refuses_overwriting_running(proj, tmp_path):
    (tmp_path / "ws").mkdir()
    kw = dict(workspace="ws", metric_cmd=["python3", "-c", "print(1)"],
              metric_grep=r"([0-9.]+)", direction="minimize")
    s1 = loop.start(proj, **kw)
    assert s1["status"] == "running"
    s2 = loop.start(proj, **kw)
    assert s2["error"] == "session_already_running"
    loop.stop(proj, s1["session_id"])
    s3 = loop.start(proj, **kw)  # restart allowed once stopped
    assert s3["status"] == "running"


def test_stop_and_status(proj, tmp_path):
    (tmp_path / "ws").mkdir()
    s = loop.start(proj, workspace="ws", metric_cmd="echo m: 1",
                   metric_grep=r"m:\s*([0-9.]+)", direction="maximize")
    sid = s["session_id"]
    assert loop.stop(proj, sid)["status"] == "stopped"
    assert loop.status(proj, sid)["status"] == "stopped"
    # record on a stopped loop is refused
    assert loop.record(proj, sid)["error"] == "loop_not_running"


# --- integration: hardened metric run (needs sandbox-exec) ---

@pytest.mark.skipif(not _HAS_SBX, reason="sandbox-exec not available")
def test_ratchet_keep_discard_keep(proj, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "val.txt").write_text("0.5")
    cmd = ["python3", "-c", "print('metric:', open('val.txt').read().strip())"]
    s = loop.start(proj, workspace="ws", metric_cmd=cmd,
                   metric_grep=r"metric:\s*([0-9.]+)", direction="minimize", max_iters=10)
    sid = s["session_id"]

    r1 = loop.record(proj, sid, cost_spent=0.01)
    assert r1["metric"] == 0.5 and r1["decision"] == "keep"

    (ws / "val.txt").write_text("0.7")  # worse for minimize
    r2 = loop.record(proj, sid)
    assert r2["metric"] == 0.7 and r2["decision"] == "discard"

    (ws / "val.txt").write_text("0.3")  # better
    r3 = loop.record(proj, sid)
    assert r3["metric"] == 0.3 and r3["decision"] == "keep"
    assert r3["best"]["metric"] == 0.3
    assert r3["iters_used"] == 3
    assert abs(r3["cost_used"] - 0.01) < 1e-9

    rp = loop._results_path(proj, sid)
    assert len(rp.read_text().strip().splitlines()) == 4  # header + 3 rows


@pytest.mark.skipif(not _HAS_SBX, reason="sandbox-exec not available")
def test_metric_not_found_is_crash(proj, tmp_path):
    (tmp_path / "ws").mkdir()
    cmd = ["python3", "-c", "print('no metric line')"]
    s = loop.start(proj, workspace="ws", metric_cmd=cmd,
                   metric_grep=r"metric:\s*([0-9.]+)", direction="minimize")
    r = loop.record(proj, s["session_id"])
    assert r["decision"] == "crash" and r["reason"] == "metric_not_found"


def test_loop_tools_registered_and_dispatch(tmp_path):
    from ai_core import mcp_server
    for t in ("autoresearch_loop_start", "autoresearch_loop_record",
              "autoresearch_loop_status", "autoresearch_loop_stop"):
        assert t in mcp_server.TOOL_NAMES
    # bare tmp project → loop disabled by default
    out = mcp_server._dispatch_tool(
        tmp_path, "autoresearch_loop_start",
        {"workspace": ".", "metric_cmd": "echo m: 1", "metric_grep": "m: ([0-9.]+)", "direction": "minimize"},
    )
    assert out == {"error": "loop_disabled"}


@pytest.mark.skipif(not _HAS_SBX, reason="sandbox-exec not available")
def test_budget_max_iters_stops(proj, tmp_path):
    (tmp_path / "ws").mkdir()
    cmd = ["python3", "-c", "print('m: 1.0')"]
    s = loop.start(proj, workspace="ws", metric_cmd=cmd,
                   metric_grep=r"m:\s*([0-9.]+)", direction="maximize", max_iters=1)
    sid = s["session_id"]
    r1 = loop.record(proj, sid)
    assert r1["iters_used"] == 1 and r1["should_continue"] is False
    r2 = loop.record(proj, sid)
    assert r2.get("error") == "loop_not_running"
