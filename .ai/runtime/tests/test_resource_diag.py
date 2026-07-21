from __future__ import annotations

from pathlib import Path

from ai_core.resource_diag import _cgroup_memory, classify_termination, system_memory_snapshot


def _snapshot(*, oom_kill: int = 0, usage_ratio: float | None = None, available_ratio: float = 0.5) -> dict:
    return {
        "host": {
            "total_bytes": 1000,
            "available_bytes": int(1000 * available_ratio),
            "available_ratio": available_ratio,
        },
        "cgroup": {
            "version": 2,
            "current_bytes": None,
            "max_bytes": None,
            "usage_ratio": usage_ratio,
            "events": {"oom_kill": oom_kill},
        },
    }


def test_classify_confirmed_cgroup_oom_kill() -> None:
    result = classify_termination(
        returncode=-9,
        timed_out=False,
        before=_snapshot(oom_kill=3),
        after=_snapshot(oom_kill=4),
        peak_rss_kib=12345,
    )

    assert result["classification"] == "cgroup_oom_kill_confirmed"
    assert result["signal"] == "SIGKILL"
    assert result["confidence"] == "high"
    assert "cgroup_oom_kill_delta=1" in result["evidence"]
    assert "peak_rss_kib=12345" in result["evidence"]


def test_classify_unattributed_sigkill_without_oom_evidence() -> None:
    result = classify_termination(
        returncode=137,
        timed_out=False,
        before=_snapshot(),
        after=_snapshot(),
        peak_rss_kib=1024,
        stderr="Killed: 9",
    )

    assert result["classification"] == "external_sigkill_or_execution_limit"
    assert result["signal"] == "SIGKILL"
    assert result["shell_mapped"] is True
    assert result["confidence"] == "low"


def test_classify_timeout_takes_precedence_over_termination_signal() -> None:
    result = classify_termination(
        returncode=-15,
        timed_out=True,
        before=_snapshot(),
        after=_snapshot(),
        peak_rss_kib=512,
    )

    assert result["classification"] == "timeout"
    assert "runner_deadline_exceeded" in result["evidence"]


def test_system_memory_snapshot_is_stable_schema() -> None:
    snapshot = system_memory_snapshot()

    assert set(snapshot) == {"host", "cgroup"}
    assert "source" in snapshot["host"]
    assert "events" in snapshot["cgroup"]


def test_cgroup_v2_uses_current_nested_scope_and_local_events(tmp_path: Path) -> None:
    proc = tmp_path / "proc-self-cgroup"
    proc.write_text("0::/user.slice/code-brain.scope\n", encoding="ascii")
    root = tmp_path / "cgroup"
    scope = root / "user.slice" / "code-brain.scope"
    scope.mkdir(parents=True)
    (scope / "memory.current").write_text("900\n", encoding="ascii")
    (scope / "memory.max").write_text("1000\n", encoding="ascii")
    (scope / "memory.peak").write_text("950\n", encoding="ascii")
    (scope / "memory.events").write_text("oom_kill 99\n", encoding="ascii")
    (scope / "memory.events.local").write_text("oom_kill 2\nmax 3\n", encoding="ascii")

    result = _cgroup_memory(proc_cgroup=proc, cgroup_root=root)

    assert result["version"] == 2
    assert result["path"] == scope.as_posix()
    assert result["current_bytes"] == 900
    assert result["max_bytes"] == 1000
    assert result["peak_bytes"] == 950
    assert result["usage_ratio"] == 0.9
    assert result["events"] == {"oom_kill": 2, "max": 3}


def test_cgroup_v1_uses_memory_controller_scope(tmp_path: Path) -> None:
    proc = tmp_path / "proc-self-cgroup"
    proc.write_text("7:cpu,cpuacct:/job\n5:memory:/job\n", encoding="ascii")
    root = tmp_path / "cgroup"
    scope = root / "memory" / "job"
    scope.mkdir(parents=True)
    (scope / "memory.usage_in_bytes").write_text("100\n", encoding="ascii")
    (scope / "memory.limit_in_bytes").write_text("400\n", encoding="ascii")
    (scope / "memory.max_usage_in_bytes").write_text("300\n", encoding="ascii")
    (scope / "memory.failcnt").write_text("4\n", encoding="ascii")

    result = _cgroup_memory(proc_cgroup=proc, cgroup_root=root)

    assert result["version"] == 1
    assert result["path"] == scope.as_posix()
    assert result["usage_ratio"] == 0.25
    assert result["peak_bytes"] == 300
    assert result["events"] == {"failcnt": 4}
