from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ai_core import doctor, hooks


def _subprocess_limits(general: str, session: str) -> tuple[int, int]:
    env = os.environ.copy()
    env["AI_INJECTION_MAX_BYTES"] = general
    env["AI_SESSION_START_MAX_BYTES"] = session
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from ai_core.hooks import MAX_INJECTION_BYTES, SESSION_START_MAX_INJECTION_BYTES; "
                "print(json.dumps([MAX_INJECTION_BYTES, SESSION_START_MAX_INJECTION_BYTES]))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    general_limit, session_limit = json.loads(result.stdout)
    return int(general_limit), int(session_limit)


def test_general_injection_budget_is_a_hard_utf8_boundary(tmp_path: Path, monkeypatch) -> None:
    """The budget is a hard ceiling, not a quota to fill exactly.

    This used to assert ``== 256`` because the composer truncated the joined string blindly,
    which always landed mid-section. Composition is now section-aware, so it stops at the
    last section that fits rather than emitting a half-sentence directive. The invariant
    that matters to the host is the ceiling.
    """
    monkeypatch.setattr(hooks, "MAX_INJECTION_BYTES", 256)

    context = hooks.build_context("UserPromptSubmit", {"dry": True}, root=tmp_path)

    assert len(context.encode("utf-8")) <= 256
    # Whatever survives must be whole: never a dangling partial directive.
    assert not context.rstrip().endswith(("hook=", "agent=", "network=", "writes="))


def test_session_start_uses_its_separate_larger_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hooks, "MAX_INJECTION_BYTES", 256)
    monkeypatch.setattr(hooks, "SESSION_START_MAX_INJECTION_BYTES", 1024)

    prompt_context = hooks.build_context("UserPromptSubmit", {"dry": True}, root=tmp_path)
    session_context = hooks.build_context("SessionStart", {"dry": True}, root=tmp_path)

    assert len(prompt_context.encode("utf-8")) <= 256
    assert len(prompt_context.encode("utf-8")) < len(session_context.encode("utf-8")) <= 1024


def test_injection_budget_environment_values_are_clamped() -> None:
    assert _subprocess_limits("1", "1") == (256, 256)
    assert _subprocess_limits("999999", "999999") == (8192, 32768)
    assert _subprocess_limits("6000", "100") == (6000, 6000)
    assert _subprocess_limits("invalid", "invalid") == (2048, 8192)


def test_repeated_user_prompt_context_uses_delta_notice(tmp_path: Path) -> None:
    full_context = "stable context"
    first = hooks._maybe_apply_delta(tmp_path, "UserPromptSubmit", full_context)
    second = hooks._maybe_apply_delta(tmp_path, "UserPromptSubmit", full_context)

    assert first == (full_context, False, len(full_context.encode("utf-8")))
    assert second == (hooks.DELTA_NOTICE_SHORT, True, len(full_context.encode("utf-8")))


def test_doctor_surfaces_injected_context_budget_contract(tmp_path: Path) -> None:
    check = doctor.check_injected_context_budget(tmp_path)

    assert check.name == "injected_context_budget"
    assert check.ok is True
    assert f"general={hooks.MAX_INJECTION_BYTES}B" in check.detail
    assert f"session_start={hooks.SESSION_START_MAX_INJECTION_BYTES}B" in check.detail
