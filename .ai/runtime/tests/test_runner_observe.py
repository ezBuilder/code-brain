from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import ai_core.runner_observe as runner_observe
from ai_core.runner_observe import _bounded_command, observe_command, observation_path, observation_status


def test_bounded_command_replaces_oversized_argv_with_digest() -> None:
    payload = _bounded_command([sys.executable, "x" * 100_000])

    assert payload["truncated"] is True
    assert payload["argc"] == 2
    assert payload["original_bytes"] > 16_000
    assert len(payload["sha256"]) == 64
    assert len(payload["preview"].encode("utf-8")) <= 4_000


def test_observe_command_persists_bounded_success(tmp_path: Path) -> None:
    payload = observe_command(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import time; data = bytearray(2 * 1024 * 1024); print(len(data)); time.sleep(0.15)",
        ],
        label="unit-success",
        stream=False,
        timeout_seconds=2,
    )
    assert payload["ok"] is True
    assert payload["spawned"] is True
    assert payload["observer_error"] is None
    assert payload["timed_out"] is False
    assert payload["interrupted"] is False
    assert payload["interrupt_observation_enabled"] is True
    assert payload["exit_code"] == 0
    assert payload["killed_9"] is False
    assert payload["transport_restart"] is False
    assert payload["termination"]["classification"] == "success"
    assert payload["rss_samples"] >= 1
    assert payload["rss_sample_errors"] == 0
    assert payload["peak_rss_kib"] is None or payload["peak_rss_kib"] > 0
    assert set(payload["resource_before"]) == {"host", "cgroup"}
    assert set(payload["resource_after"]) == {"host", "cgroup"}
    assert payload["policy"]["command_max_bytes"] == 16_000
    assert payload["policy"]["timeout_seconds"] == 2.0
    assert payload["output_total_bytes"] > 0
    assert len(payload["output_tail"].encode("utf-8")) <= 64_000

    path = observation_path(tmp_path)
    assert path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = observation_status(tmp_path)
    assert status["ok"] is True
    assert status["bounded"] is True
    assert status["observed"] is True
    assert status["spawned"] is True
    assert status["timed_out"] is False
    assert status["schema_version"] == 5
    assert status["interrupted"] is False
    assert status["interrupt_observation_enabled"] is True
    assert status["label"] == "unit-success"
    assert status["termination"]["classification"] == "success"
    assert status["rss_samples"] >= 1
    assert status["rss_sample_errors"] == 0
    assert status["observer_error"] is None
    assert set(status["resources"]) == {"before", "after"}
    assert "output_tail" not in status


def test_observe_command_persists_spawn_failure(tmp_path: Path) -> None:
    payload = observe_command(
        tmp_path,
        [str(tmp_path / "definitely-missing-runner")],
        label="spawn-failure",
        stream=False,
    )

    assert payload["ok"] is False
    assert payload["spawned"] is False
    assert payload["timed_out"] is False
    assert payload["interrupted"] is False
    assert payload["interrupt_observation_enabled"] is False
    assert payload["raw_returncode"] is None
    assert payload["exit_code"] == 127
    assert payload["observer_error"]["phase"] == "spawn"
    assert payload["observer_error"]["type"] == "FileNotFoundError"
    assert payload["termination"]["classification"] == "spawn_error"
    assert payload["termination"]["child_classification"] == "nonzero_exit"
    assert payload["rss_samples"] == 0
    assert observation_path(tmp_path).is_file()

    status = observation_status(tmp_path)
    assert status["ok"] is False
    assert status["observed"] is True
    assert status["spawned"] is False
    assert status["exit_code"] == 127
    assert status["observer_error"]["phase"] == "spawn"
    assert status["termination"]["classification"] == "spawn_error"


def test_observe_command_cleans_up_after_output_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPipe:
        def read1(self, _size: int) -> bytes:
            raise OSError("synthetic pipe failure")

        def close(self) -> None:
            return None

    class FakeProcess:
        pid = 424242
        stdout = FailingPipe()

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                if timeout is None:
                    raise AssertionError("unbounded wait after observer failure")
                raise subprocess.TimeoutExpired("fake", timeout)
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    fake = FakeProcess()
    cleanup_calls: list[int] = []
    memory_snapshot = {
        "host": {"source": "test", "total_bytes": 1, "available_bytes": 1},
        "cgroup": {
            "version": None,
            "path": None,
            "current_bytes": None,
            "max_bytes": None,
            "peak_bytes": None,
            "usage_ratio": None,
            "events": {},
        },
    }

    monkeypatch.setattr(runner_observe.subprocess, "Popen", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(runner_observe, "process_tree_rss_kib", lambda _pid: None)
    monkeypatch.setattr(runner_observe, "system_memory_snapshot", lambda: memory_snapshot)

    def fake_cleanup(process: FakeProcess) -> None:
        cleanup_calls.append(process.pid)
        process.returncode = -15

    monkeypatch.setattr(runner_observe, "_terminate_observed_process", fake_cleanup)

    payload = observe_command(
        tmp_path,
        [sys.executable, "-c", "print('unreachable')"],
        label="read-failure",
        stream=False,
    )

    assert cleanup_calls == [fake.pid]
    assert payload["ok"] is False
    assert payload["spawned"] is True
    assert payload["timed_out"] is False
    assert payload["exit_code"] == 70
    assert payload["raw_returncode"] == -15
    assert payload["observer_error"]["phase"] == "read_output"
    assert payload["termination"]["classification"] == "observer_failure"
    assert payload["termination"]["child_classification"] == "signal_termination"
    assert payload["rss_samples"] == 0
    assert observation_status(tmp_path)["observer_error"]["phase"] == "read_output"
    assert not any(
        thread.name == "code-brain-runner-rss" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_observe_command_tolerates_rss_sampler_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_observe,
        "process_tree_rss_kib",
        lambda _pid: (_ for _ in ()).throw(OSError("synthetic RSS failure")),
    )

    payload = observe_command(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(0.12); print('ok')"],
        label="rss-sampler-failure",
        stream=False,
    )

    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["observer_error"] is None
    assert payload["timed_out"] is False
    assert payload["peak_rss_kib"] is None
    assert payload["rss_samples"] == 0
    assert payload["rss_sample_errors"] >= 2
    assert payload["termination"]["classification"] == "success"


def test_observe_command_enforces_explicit_timeout(tmp_path: Path) -> None:
    payload = observe_command(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import time; print('before-timeout', flush=True); time.sleep(30)",
        ],
        label="explicit-timeout",
        stream=False,
        timeout_seconds=0.15,
    )

    assert payload["ok"] is False
    assert payload["spawned"] is True
    assert payload["timed_out"] is True
    assert payload["interrupted"] is False
    assert payload["observer_error"] is None
    assert payload["exit_code"] == 124
    assert payload["killed_9"] is False
    assert payload["transport_restart"] is False
    assert payload["termination"]["classification"] == "timeout"
    assert payload["policy"]["timeout_seconds"] == 0.15
    assert "before-timeout" in payload["output_tail"]
    assert payload["elapsed_ms"] < 5_000
    assert not any(
        thread.name == "code-brain-runner-timeout" and thread.is_alive()
        for thread in threading.enumerate()
    )

    status = observation_status(tmp_path)
    assert status["ok"] is False
    assert status["timed_out"] is True
    assert status["exit_code"] == 124
    assert status["termination"]["classification"] == "timeout"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_observe_command_timeout_stops_descendant_process_group(tmp_path: Path) -> None:
    heartbeat = tmp_path / "descendant-heartbeat.txt"
    child_script = f"""
import pathlib
import signal
import time

signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = pathlib.Path({str(heartbeat)!r})
with path.open("a", buffering=1) as handle:
    for _ in range(10_000):
        handle.write("x")
        handle.flush()
        time.sleep(0.01)
"""
    parent_script = f"""
import pathlib
import subprocess
import sys
import time

path = pathlib.Path({str(heartbeat)!r})
subprocess.Popen([sys.executable, "-c", {child_script!r}])
deadline = time.monotonic() + 2
while not path.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print("descendant-started", flush=True)
time.sleep(30)
"""

    payload = observe_command(
        tmp_path,
        [sys.executable, "-c", parent_script],
        label="descendant-timeout",
        stream=False,
        timeout_seconds=0.5,
    )

    assert payload["timed_out"] is True
    assert payload["observer_error"] is None
    assert payload["exit_code"] == 124
    assert heartbeat.is_file()
    size_after_return = heartbeat.stat().st_size
    time.sleep(0.15)
    assert heartbeat.stat().st_size == size_after_return


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent-signal and process-group semantics")
@pytest.mark.parametrize(
    ("observer_signal", "expected_exit"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_observer_interrupt_stops_descendants_and_persists_evidence(
    tmp_path: Path,
    observer_signal: signal.Signals,
    expected_exit: int,
) -> None:
    heartbeat = tmp_path / f"interrupt-{observer_signal.name}.txt"
    child_script = f"""
import pathlib
import signal
import time

signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = pathlib.Path({str(heartbeat)!r})
with path.open("a", buffering=1) as handle:
    for _ in range(10_000):
        handle.write("x")
        handle.flush()
        time.sleep(0.01)
"""
    parent_script = f"""
import pathlib
import subprocess
import sys
import time

path = pathlib.Path({str(heartbeat)!r})
subprocess.Popen([sys.executable, "-c", {child_script!r}])
deadline = time.monotonic() + 2
while not path.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print("interrupt-ready", flush=True)
time.sleep(30)
"""
    helper_script = f"""
import sys
from pathlib import Path
from ai_core.runner_observe import observe_command

payload = observe_command(
    Path({str(tmp_path)!r}),
    [sys.executable, "-c", {parent_script!r}],
    label={f"observer-{observer_signal.name.lower()}"!r},
    stream=True,
)
raise SystemExit(payload["exit_code"])
"""
    env = os.environ.copy()
    runtime_src = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(runtime_src)
    process = subprocess.Popen(
        [sys.executable, "-c", helper_script],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "interrupt-ready"

    process.send_signal(observer_signal)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == expected_exit, stdout + stderr
    assert heartbeat.is_file()
    size_after_return = heartbeat.stat().st_size
    time.sleep(0.15)
    assert heartbeat.stat().st_size == size_after_return

    status = observation_status(tmp_path)
    assert status["ok"] is False
    assert status["interrupted"] is True
    assert status["observer_signal"] == observer_signal.name
    assert status["observer_signal_number"] == int(observer_signal)
    assert status["observer_signal_count"] >= 1
    assert status["interrupt_observation_enabled"] is True
    assert status["exit_code"] == expected_exit
    assert status["killed_9"] is False
    assert status["termination"]["classification"] == "observer_interrupted"


@pytest.mark.parametrize("timeout_seconds", [0, -1, float("inf"), float("nan"), True, "1"])
def test_observe_command_rejects_invalid_timeout(
    tmp_path: Path,
    timeout_seconds: object,
) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        observe_command(
            tmp_path,
            [sys.executable, "-c", "print('must-not-run')"],
            label="invalid-timeout",
            stream=False,
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )
    assert observation_path(tmp_path).exists() is False


def test_observe_command_detects_zero_exit_transport_restart(tmp_path: Path) -> None:
    payload = observe_command(
        tmp_path,
        [sys.executable, "-c", "print('transport connection reset; reconnecting')"],
        label="transport-marker",
        stream=False,
    )
    assert payload["exit_code"] == 0
    assert payload["ok"] is False
    assert payload["transport_restart"] is True
    assert payload["marker_counts"]["transport_restart"] >= 1
    status = observation_status(tmp_path)
    assert status["ok"] is False
    assert status["transport_restart"] is True


def test_observe_command_detects_marker_across_read_chunks(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.write('x' * 65530 + 'RUN_NOT_FOUND\\n')"
    payload = observe_command(
        tmp_path,
        [sys.executable, "-c", script],
        label="chunk-boundary",
        stream=False,
    )
    assert payload["exit_code"] == 0
    assert payload["transport_restart"] is True
    assert payload["marker_counts"]["run_not_found"] == 1
    assert len(payload["output_tail"].encode("utf-8")) <= 64_000


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL semantics")
def test_observe_command_normalizes_sigkill(tmp_path: Path) -> None:
    payload = observe_command(
        tmp_path,
        [sys.executable, "-c", "import os; os.kill(os.getpid(), 9)"],
        label="sigkill",
        stream=False,
    )
    assert payload["raw_returncode"] == -9
    assert payload["exit_code"] == 137
    assert payload["signal"] == "SIGKILL"
    assert payload["killed_9"] is True
    assert payload["ok"] is False
    assert payload["termination"]["signal"] == "SIGKILL"
    assert payload["termination"]["classification"] in {
        "cgroup_oom_kill_confirmed",
        "cgroup_memory_limit_confirmed",
        "cgroup_memory_limit_likely",
        "host_memory_pressure_likely",
        "external_sigkill_or_execution_limit",
    }


def test_observation_status_rejects_symlink(tmp_path: Path) -> None:
    path = observation_path(tmp_path)
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)
    status = observation_status(tmp_path)
    assert status["ok"] is False
    assert status["bounded"] is True
    assert status["observed"] is False
    assert status["reason"] == "open_error"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("spawned", "yes"),
        ("timed_out", "yes"),
        ("interrupted", "yes"),
        ("observer_signal", 15),
        ("observer_signal_number", "15"),
        ("observer_signal_count", -1),
        ("interrupt_observation_enabled", "yes"),
        ("observer_error", "not-an-object"),
        ("rss_samples", True),
        ("rss_sample_errors", -1),
        ("peak_rss_kib", -1),
    ],
)
def test_observation_status_rejects_invalid_schema_field_types(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    observe_command(
        tmp_path,
        [sys.executable, "-c", "print('valid-before-corruption')"],
        label="schema-corruption",
        stream=False,
    )
    path = observation_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = invalid_value
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = observation_status(tmp_path)
    assert status["ok"] is False
    assert status["observed"] is True
    assert status["reason"] == "invalid_schema"


def test_observation_status_rejects_inconsistent_success(tmp_path: Path) -> None:
    observe_command(
        tmp_path,
        [sys.executable, "-c", "print('valid-before-corruption')"],
        label="schema-consistency",
        stream=False,
    )
    path = observation_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["observer_error"] = {"phase": "read_output", "type": "OSError"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = observation_status(tmp_path)
    assert status["ok"] is False
    assert status["reason"] == "invalid_schema"


def _load_run_observed_script() -> object:
    path = Path(__file__).resolve().parents[3] / "scripts" / "run-observed.py"
    spec = importlib.util.spec_from_file_location("code_brain_run_observed_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_observed_cli_forwards_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_run_observed_script()
    observed: dict[str, object] = {}

    def fake_observe_command(
        root: Path,
        command: list[str],
        *,
        label: str,
        timeout_seconds: float | None,
        evidence_token: str | None,
        env: dict[str, str] | None,
    ) -> dict[str, object]:
        observed.update(
            root=root,
            command=command,
            label=label,
            timeout_seconds=timeout_seconds,
            evidence_token=evidence_token,
            env=env,
        )
        return {"exit_code": 124, "ok": False}

    monkeypatch.setattr(module, "observe_command", fake_observe_command)

    result = module.main(
        ["--label", "cli-timeout", "--timeout-seconds", "0.25", "--", "echo", "hello"]
    )
    assert result == 124
    assert observed["command"] == ["echo", "hello"]
    assert observed["label"] == "cli-timeout"
    assert observed["timeout_seconds"] == 0.25
    assert observed["evidence_token"] is None
    assert observed["env"] is None


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_run_observed_cli_rejects_invalid_timeout(value: str) -> None:
    module = _load_run_observed_script()

    with pytest.raises(SystemExit) as exc_info:
        module.main(["--label", "invalid", "--timeout-seconds", value, "--", "echo"])
    assert exc_info.value.code == 2


def test_managed_observer_entrypoints_share_timeout_wrapper() -> None:
    root = Path(__file__).resolve().parents[3]
    wrapper = root / "scripts" / "run-observed-command.sh"
    wrapper_text = wrapper.read_text(encoding="utf-8")
    bootstrap = (root / "bootstrap.sh").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    ci_local = (root / "scripts" / "ci-local.sh").read_text(encoding="utf-8")

    assert wrapper.stat().st_mode & stat.S_IXUSR
    assert (root / "bootstrap.sh").stat().st_mode & stat.S_IXUSR
    assert (root / "scripts" / "ci-local.sh").stat().st_mode & stat.S_IXUSR
    assert "AI_RUNNER_TIMEOUT_SECONDS" in wrapper_text
    assert 'observer+=(--timeout-seconds "$AI_RUNNER_TIMEOUT_SECONDS")' in wrapper_text
    assert 'exec "${observer[@]}" -- "$@"' in wrapper_text
    assert "run-observed-command.sh bootstrap-pytest" in bootstrap
    assert "run-observed-command.sh make-test" in makefile
    assert "run-observed-command.sh ci-local-windows-runtime" in ci_local
    release_gate = (root / "scripts" / "release-gate.sh").read_text(encoding="utf-8")
    assert 'AI_RUNNER_EVIDENCE_TOKEN="$RUNNER_EVIDENCE_TOKEN" ./bootstrap.sh' in release_gate
    assert "runner evidence is stale, unbound, or interrupt-unsafe" in release_gate
    for text in (bootstrap, makefile, ci_local):
        assert "scripts/run-observed.py --label" not in text

    lint = (root / "scripts" / "lint.sh").read_text(encoding="utf-8")
    assert "git ls-files -s -z" in lint
    assert "tracked executable is not executable in the working tree" in lint


def test_managed_observer_wrapper_timeout_and_default_runtime(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    wrapper = root / "scripts" / "run-observed-command.sh"
    marker = tmp_path / "must-not-run.txt"
    env = os.environ.copy()
    env["AI_RUNNER_TIMEOUT_SECONDS"] = "0"
    invalid = subprocess.run(
        [
            str(wrapper),
            "invalid-env-timeout",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    assert invalid.returncode == 2
    assert marker.exists() is False

    env["AI_RUNNER_TIMEOUT_SECONDS"] = "0.15"
    timed_out = subprocess.run(
        [
            str(wrapper),
            "env-timeout",
            "--",
            sys.executable,
            "-c",
            "import time; print('managed-timeout', flush=True); time.sleep(30)",
        ],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    assert timed_out.returncode == 124
    assert "managed-timeout" in timed_out.stdout

    env.pop("AI_RUNNER_TIMEOUT_SECONDS", None)
    evidence_token = "fixture"
    env["AI_RUNNER_EVIDENCE_TOKEN"] = evidence_token
    success = subprocess.run(
        [
            str(wrapper),
            "env-default-unlimited",
            "--",
            sys.executable,
            "-c",
            (
                "import os; print('managed-success'); "
                "print(os.environ.get('AI_RUNNER_EVIDENCE_TOKEN', 'token-stripped'))"
            ),
        ],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    assert success.returncode == 0, success.stdout + success.stderr
    assert "managed-success" in success.stdout
    assert "token-stripped" in success.stdout
    assert evidence_token not in success.stdout

    status = observation_status(root)
    assert status["ok"] is True
    assert status["label"] == "env-default-unlimited"
    assert status["timed_out"] is False
    assert status["termination"]["classification"] == "success"
    assert status["evidence_token_sha256"] == hashlib.sha256(evidence_token.encode()).hexdigest()
    assert status["interrupt_observation_enabled"] is True


def test_operational_bounds_requires_actual_runner_observation(tmp_path: Path) -> None:
    from ai_core.report import OPERATIONAL_CHECK_GROUPS, operational_bounds_summary

    doctor = {
        "checks": [
            {"name": name, "ok": True, "detail": "ok"}
            for names in OPERATIONAL_CHECK_GROUPS.values()
            for name in names
        ]
    }
    result = operational_bounds_summary(
        tmp_path,
        doctor=doctor,
        metrics_payload={"usage": {"skipped": True, "reason": "unit-test"}},
        sandbox_payload={"ok": True, "bounded": True},
        runner_payload={"ok": True, "bounded": True, "observed": False},
    )

    assert result["ok"] is False
    assert result["runner"]["observed"] is False
