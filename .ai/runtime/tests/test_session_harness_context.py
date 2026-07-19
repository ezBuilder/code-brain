from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_core import hooks, plan_state


def test_session_harness_context_avoids_detailed_repo_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("SessionStart harness hint must not spawn git status")

    monkeypatch.setattr(subprocess, "run", unexpected_git)

    line = hooks._session_harness_context(tmp_path)

    assert line.startswith("cb-harness: target=95%")
    assert "verify" in line
    assert "mode=" not in line
    assert "src=" not in line
    assert "tests=" not in line
    assert "dirty=" not in line


def test_session_harness_context_keeps_active_plan_progress(tmp_path: Path) -> None:
    plan_state.init_plan(tmp_path, plan_id="feat-x", steps=["a", "b"])
    plan_state.mark_step(tmp_path, plan_id="feat-x", index=1)

    line = hooks._session_harness_context(tmp_path)

    assert "plan feat-x: 1/2 done" in line
    assert "next: b" in line


def test_session_start_build_context_uses_lightweight_harness_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "cb-harness: lightweight-sentinel"
    monkeypatch.setattr(hooks, "_session_harness_context", lambda _root: sentinel)

    context = hooks.build_context("SessionStart", {"agent": "operator", "dry": True}, root=tmp_path)

    assert sentinel in context