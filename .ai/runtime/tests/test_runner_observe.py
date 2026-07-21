from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from ai_core.runner_observe import observe_command, observation_path, observation_status


def test_observe_command_persists_bounded_success(tmp_path: Path) -> None:
    payload = observe_command(
        tmp_path,
        [sys.executable, "-c", "print('runner healthy')"],
        label="unit-success",
        stream=False,
    )
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["killed_9"] is False
    assert payload["transport_restart"] is False
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
    assert status["label"] == "unit-success"
    assert "output_tail" not in status


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
