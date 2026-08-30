from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import hooks, obs  # noqa: E402
from ai_core.doctor import check_hot_path_slo  # noqa: E402


def test_hot_path_slo_reuses_actual_session_start_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def measured_hook(_root: Path, hook: str, _payload: dict) -> dict:
        calls.append(hook)
        if hook == "SessionStart":
            raise AssertionError("precomputed SessionStart timing must avoid resampling")
        return {"ok": True, "elapsed_ms": 1}

    monkeypatch.setattr(hooks, "handle_hook", measured_hook)
    monkeypatch.setattr(
        obs,
        "hook_entrypoint_latency",
        lambda _root: {
            "ok": True,
            "measured": True,
            "reason": "measured",
            "scope": "end_to_end",
            "best_ms": 57,
            "p95_ms": 61,
            "target_ms": obs.HOOK_ENTRYPOINT_TARGET_MS,
        },
    )

    check = check_hot_path_slo(tmp_path, session_start_ms=17)

    assert check.ok is True
    assert calls == ["DoctorSLOBaseline"] * 10
    assert "session_start_ms=17" in check.detail
    assert "p95_scope=in_process" in check.detail
    assert "entrypoint_p95_ms=61" in check.detail
    assert "entrypoint_best_ms=57" in check.detail
    assert "entrypoint_scope=end_to_end" in check.detail
    assert "entrypoint_hook=SessionStart" in check.detail
    assert f"entrypoint_gate_ms={obs.HOOK_ENTRYPOINT_TARGET_MS * 4}" in check.detail


def test_hot_path_slo_standalone_doctor_keeps_best_of_five(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_values = iter([35, 22, 19, 27, 21])
    calls: list[str] = []

    def measured_hook(_root: Path, hook: str, _payload: dict) -> dict:
        calls.append(hook)
        elapsed = next(session_values) if hook == "SessionStart" else 1
        return {"ok": True, "elapsed_ms": elapsed}

    monkeypatch.setattr(hooks, "handle_hook", measured_hook)

    check = check_hot_path_slo(tmp_path)

    assert check.ok is True
    assert calls.count("DoctorSLOBaseline") == 10
    assert calls.count("SessionStart") == 5
    assert "session_start_ms=19" in check.detail


def test_hot_path_slo_lightweight_uses_actual_session_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_hook(*_args, **_kwargs):
        raise AssertionError("lightweight session must not run synthetic SLO hooks")

    monkeypatch.setattr(hooks, "handle_hook", unexpected_hook)
    monkeypatch.setattr(
        obs,
        "hook_entrypoint_latency",
        lambda _root: (_ for _ in ()).throw(AssertionError("lightweight check must not spawn entrypoint")),
    )

    check = check_hot_path_slo(
        tmp_path,
        session_start_ms=13,
        sample_baseline=False,
    )

    assert check.ok is True
    assert "p95_ms=deferred" in check.detail
    assert "session_start_ms=13" in check.detail
    assert "entrypoint_reason=deferred_lightweight" in check.detail


def test_hot_path_slo_fails_when_measured_entrypoint_exceeds_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        obs,
        "hook_entrypoint_latency",
        lambda _root: {
            "ok": False,
            "measured": True,
            "reason": "measured",
            "scope": "end_to_end",
            "best_ms": 1000,
            "p95_ms": 1100,
            "target_ms": obs.HOOK_ENTRYPOINT_TARGET_MS,
        },
    )

    check = check_hot_path_slo(tmp_path, session_start_ms=1)

    assert check.ok is False
    assert "entrypoint_p95_ms=1100" in check.detail
    assert "entrypoint_best_ms=1000" in check.detail


def test_hot_path_slo_applies_coarse_gate_headroom_to_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        obs,
        "hook_entrypoint_latency",
        lambda _root: {
            "ok": False,
            "measured": True,
            "reason": "measured",
            "scope": "end_to_end",
            "best_ms": 450,
            "p95_ms": 500,
            "target_ms": obs.HOOK_ENTRYPOINT_TARGET_MS,
        },
    )

    check = check_hot_path_slo(tmp_path, session_start_ms=1)

    assert check.ok is True
    assert "entrypoint_p95_ms=500" in check.detail


def test_standalone_hot_path_check_does_not_create_missing_search_index(tmp_path: Path) -> None:
    db = tmp_path / ".ai" / "cache" / "code.sqlite"

    check = check_hot_path_slo(tmp_path)

    assert check.ok is True
    assert not db.exists()
