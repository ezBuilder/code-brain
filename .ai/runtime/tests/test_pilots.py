"""Pilot discoverability registry + doctor INFO surfacing."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import doctor
from ai_core import pilots


_ALL_ENVS = {
    "AI_MCP_RESOURCES",
    "AI_DIR_CONTEXT",
    "AI_MEMORY_CONFLICT_SCAN",
    "AI_LOOP_CONTINUATION",
    "AI_AST_CHUNK",
    "AI_SELF_IMPROVE_AUTO",
}

_SAFE_ENVS = {
    "AI_MCP_RESOURCES",
    "AI_DIR_CONTEXT",
    "AI_MEMORY_CONFLICT_SCAN",
    "AI_LOOP_CONTINUATION",
    "AI_SELF_IMPROVE_AUTO",
}


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in _ALL_ENVS:
        monkeypatch.delenv(env, raising=False)


def test_status_lists_all_features_with_required_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    states = pilots.status(tmp_path)
    assert set(states) == _ALL_ENVS
    for env, info in states.items():
        assert info["env"] == env
        assert set(info) == {"env", "default", "effective_on", "desc", "safe"}
        assert isinstance(info["default"], bool)
        assert isinstance(info["effective_on"], bool)
        assert isinstance(info["safe"], bool)
        assert isinstance(info["desc"], str) and info["desc"]


def test_default_state_reflects_intended_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    states = pilots.status(tmp_path)
    # Default-ON features are on when unset; opt-in pilots are off.
    assert states["AI_MCP_RESOURCES"]["effective_on"] is True
    assert states["AI_DIR_CONTEXT"]["effective_on"] is True
    assert states["AI_MEMORY_CONFLICT_SCAN"]["effective_on"] is True
    assert states["AI_LOOP_CONTINUATION"]["effective_on"] is True
    assert states["AI_AST_CHUNK"]["effective_on"] is False
    assert states["AI_SELF_IMPROVE_AUTO"]["effective_on"] is False


def test_effective_on_reflects_env_enable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_AST_CHUNK", "1")
    monkeypatch.setenv("AI_SELF_IMPROVE_AUTO", "true")
    states = pilots.status(tmp_path)
    assert states["AI_AST_CHUNK"]["effective_on"] is True
    assert states["AI_SELF_IMPROVE_AUTO"]["effective_on"] is True


def test_effective_on_reflects_env_disable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_MCP_RESOURCES", "0")
    monkeypatch.setenv("AI_DIR_CONTEXT", "false")
    monkeypatch.setenv("AI_LOOP_CONTINUATION", "off")
    states = pilots.status(tmp_path)
    assert states["AI_MCP_RESOURCES"]["effective_on"] is False
    assert states["AI_DIR_CONTEXT"]["effective_on"] is False
    assert states["AI_LOOP_CONTINUATION"]["effective_on"] is False


def test_enable_all_returns_safe_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    mapping = pilots.enable_all()
    assert set(mapping) == _SAFE_ENVS
    assert all(value == "1" for value in mapping.values())
    # Risky / eval-gated pilot excluded from the one-switch safe set.
    assert "AI_AST_CHUNK" not in mapping


def test_disable_all_returns_safe_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    mapping = pilots.disable_all()
    assert set(mapping) == _SAFE_ENVS
    assert all(value == "0" for value in mapping.values())
    assert "AI_AST_CHUNK" not in mapping


def test_enable_all_include_unsafe_covers_everything() -> None:
    mapping = pilots.enable_all(include_unsafe=True)
    assert set(mapping) == _ALL_ENVS
    assert mapping["AI_AST_CHUNK"] == "1"


def test_enable_all_does_not_mutate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    pilots.enable_all()
    import os

    for env in _ALL_ENVS:
        assert env not in os.environ


def test_export_lines_renders_posix_exports() -> None:
    lines = pilots.export_lines({"AI_MCP_RESOURCES": "1"})
    assert lines == ["export AI_MCP_RESOURCES=1"]


def test_doctor_check_pilots_is_always_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    check = doctor.check_pilots(tmp_path)
    assert check.name == "pilots"
    assert check.ok is True
    assert "/" in check.detail  # "<n>/<total> on"


def test_doctor_check_pilots_ok_when_all_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for env in _ALL_ENVS:
        monkeypatch.setenv(env, "0")
    check = doctor.check_pilots(tmp_path)
    assert check.ok is True
    assert check.detail.startswith("0/")


def test_doctor_check_pilots_in_run_checks(tmp_path: Path) -> None:
    checks = doctor.run_checks(tmp_path)
    pilot_checks = [c for c in checks if c.name == "pilots"]
    assert len(pilot_checks) == 1
    assert pilot_checks[0].ok is True
