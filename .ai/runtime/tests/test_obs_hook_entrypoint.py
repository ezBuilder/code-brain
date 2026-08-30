from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import hooks, obs  # noqa: E402


def test_hook_entrypoint_latency_does_not_fallback_when_managed_shim_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        obs.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("subprocess must not run")),
    )

    result = obs.hook_entrypoint_latency(tmp_path)

    assert result["ok"] is True
    assert result["measured"] is False
    assert result["reason"] == "shim_unavailable"
    assert result["scope"] == "end_to_end"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim contract")
def test_hook_entrypoint_latency_executes_managed_shim_with_bounded_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim = tmp_path / ".ai" / "bin" / "ai-hook"
    interpreter = tmp_path / ".ai" / "runtime" / ".venv" / "bin" / "python"
    shim.parent.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nprintf '{}\\n'\n", encoding="utf-8")
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o700)
    interpreter.chmod(0o700)
    calls: list[tuple[list[str], dict]] = []

    def completed(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "{}\n", "")

    clock = iter([0.0, 0.060, 1.0, 1.055, 2.0, 2.057])
    monkeypatch.setattr(obs.subprocess, "run", completed)
    monkeypatch.setattr(obs.time, "perf_counter", lambda: next(clock))

    result = obs.hook_entrypoint_latency(tmp_path, samples=99)

    assert result["ok"] is True
    assert result["measured"] is True
    assert result["reason"] == "measured"
    assert result["scope"] == "end_to_end"
    assert result["hook"] == "SessionStart"
    assert len(result["samples_ms"]) == obs.HOOK_ENTRYPOINT_SAMPLES
    assert result["best_ms"] == min(result["samples_ms"])
    assert result["steady_ms"] == sorted(result["samples_ms"])[1]
    assert result["p95_ms"] == max(result["samples_ms"])
    assert result["gross_ceiling_ms"] == obs.HOOK_ENTRYPOINT_GROSS_CEILING_MS
    assert len(calls) == obs.HOOK_ENTRYPOINT_SAMPLES
    assert all(call[0][0] == str(shim) for call in calls)
    assert all(call[0][1] == "SessionStart" for call in calls)
    assert all('"dry":true' in call[1]["input"] for call in calls)
    assert all('"agent":"codex"' in call[1]["input"] for call in calls)
    assert all(call[1]["env"]["CLAUDE_PROJECT_DIR"] == str(tmp_path) for call in calls)
    assert all(call[1]["env"]["CODEX_PROJECT_DIR"] == str(tmp_path) for call in calls)


@pytest.mark.parametrize(
    ("durations_ms", "expected_ok"),
    [
        ([55, 260, 57], True),
        ([260, 270, 55], False),
        ([55, 57, 1001], False),
    ],
)
def test_hook_entrypoint_latency_tolerates_one_small_outlier_but_not_sustained_or_gross_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durations_ms: list[int],
    expected_ok: bool,
) -> None:
    monkeypatch.setattr(obs, "_hook_entrypoint_command", lambda _root: (["ai-hook"], None))
    monkeypatch.setattr(
        obs.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "{}\n", ""),
    )
    clock: list[float] = []
    for index, duration_ms in enumerate(durations_ms):
        started = float(index * 10)
        clock.extend((started, started + duration_ms / 1000))
    values = iter(clock)
    monkeypatch.setattr(obs.time, "perf_counter", lambda: next(values))

    result = obs.hook_entrypoint_latency(tmp_path)

    assert result["ok"] is expected_ok
    assert result["steady_ms"] == sorted(result["samples_ms"])[1]
    assert result["p95_ms"] == max(result["samples_ms"])


def test_slo_bench_labels_in_process_and_end_to_end_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hooks,
        "handle_hook",
        lambda *_args, **_kwargs: {"ok": True, "elapsed_ms": 0},
    )
    monkeypatch.setattr(
        obs,
        "hook_entrypoint_latency",
        lambda _root: {
            "ok": True,
            "measured": True,
            "reason": "measured",
            "scope": "end_to_end",
            "best_ms": 55,
            "steady_ms": 57,
            "p95_ms": 58,
            "samples_ms": [58, 55, 57],
            "target_ms": obs.HOOK_ENTRYPOINT_TARGET_MS,
            "gross_ceiling_ms": obs.HOOK_ENTRYPOINT_GROSS_CEILING_MS,
        },
    )

    result = obs.slo_bench(tmp_path, iterations=2)

    assert result["ok"] is True
    assert result["p95_ms"] == 0
    assert result["p95_scope"] == "in_process"
    assert result["entrypoint"]["scope"] == "end_to_end"
    assert result["entrypoint"]["best_ms"] == 55
    assert result["entrypoint"]["steady_ms"] == 57
    assert result["entrypoint"]["p95_ms"] == 58
