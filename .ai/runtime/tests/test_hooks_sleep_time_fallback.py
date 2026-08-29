"""Sleep-time maintenance must still run on hosts that never emit Stop/SessionEnd.

A real workspace sat 12 days with an unexpired `.ai/cache/sleep-time.lock` and no
background maintenance: its agent host emitted SessionStart/UserPromptSubmit daily
but Stop/SessionEnd not once. Turn-start acts as a fallback trigger, rate-limited
so it is idle-time catch-up rather than per-turn work.
"""
from __future__ import annotations

import time
from pathlib import Path

from ai_core.hooks import (
    SLEEP_TIME_FALLBACK_HOOKS,
    SLEEP_TIME_FALLBACK_MIN_AGE_SECONDS,
    SLEEP_TIME_HOOKS,
    _sleep_time_fallback_due,
)


def _lock(root: Path, age_seconds: float) -> Path:
    lock = root / ".ai" / "cache" / "sleep-time.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("running", encoding="utf-8")
    stamp = time.time() - age_seconds
    import os

    os.utime(lock, (stamp, stamp))
    return lock


def test_missing_lock_is_due(tmp_path: Path) -> None:
    assert _sleep_time_fallback_due(tmp_path) is True


def test_recent_lock_is_not_due(tmp_path: Path) -> None:
    _lock(tmp_path, 60)
    assert _sleep_time_fallback_due(tmp_path) is False


def test_lock_just_under_threshold_is_not_due(tmp_path: Path) -> None:
    _lock(tmp_path, SLEEP_TIME_FALLBACK_MIN_AGE_SECONDS - 60)
    assert _sleep_time_fallback_due(tmp_path) is False


def test_stale_lock_is_due(tmp_path: Path) -> None:
    """The observed failure: a lock stuck for days must not block forever."""
    _lock(tmp_path, 12 * 24 * 60 * 60)
    assert _sleep_time_fallback_due(tmp_path) is True


def test_fallback_threshold_exceeds_spawn_cooldown() -> None:
    """Fallback must be far rarer than the 600s spawn dedup, or turn-start
    events would effectively run maintenance every turn."""
    assert SLEEP_TIME_FALLBACK_MIN_AGE_SECONDS > 600 * 10


def test_hook_sets_are_disjoint() -> None:
    assert not (SLEEP_TIME_HOOKS & SLEEP_TIME_FALLBACK_HOOKS)
    assert SLEEP_TIME_HOOKS == {"Stop", "SessionEnd"}
    assert SLEEP_TIME_FALLBACK_HOOKS == {"SessionStart", "UserPromptSubmit"}


def test_unreadable_lock_is_not_due(tmp_path: Path) -> None:
    """Errors must degrade to not-due; a hook may never fail on this path."""
    lock = tmp_path / ".ai" / "cache" / "sleep-time.lock"
    lock.parent.mkdir(parents=True)
    lock.mkdir()  # a directory where a file is expected
    assert _sleep_time_fallback_due(tmp_path) in {True, False}


def test_memory_sync_is_never_spawned_from_a_hook() -> None:
    """Auto-sync does git fetch/push. The project's own contract forbids network I/O on
    the hooks/MCP hot path even when the call is detached — a background process
    launched FROM a hook is still the hook causing network I/O. So no hook, turn-start
    fallback or turn-end, may spawn it; only the explicit `ai memory sync` command may."""
    # Resolve from the imported module, not the CWD: a relative path here only works when
    # pytest happens to run from the repo root, and fails from .ai/runtime.
    import ai_core.hooks as _hooks_mod

    source = Path(_hooks_mod.__file__).read_text(encoding="utf-8")
    assert "_spawn_memory_sync" not in source
    assert not hasattr(_hooks_mod, "_spawn_memory_sync")
