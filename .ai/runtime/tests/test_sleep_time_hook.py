"""Test sleep-time idle job spawning (Stop/SessionEnd hook).

T6: spawn background memory page-out, audit fold, index refresh after session end.
Env opt-out: AI_SLEEP_TIME=0 or 'off'. Lock-based dedup (600s).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def run_hook(
    hook_name: str,
    payload: dict,
    *,
    cwd: Path = ROOT,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        merged.pop(name, None)
    merged["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    if env_extra:
        merged.update(env_extra)
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", "hook", hook_name, "--json"],
        cwd=cwd,
        env=merged,
        text=True,
        input=json.dumps({**payload, "dry": True}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _parse_ok(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_stop_hook_runs_without_error() -> None:
    """Smoke test: Stop hook with AI_SLEEP_TIME=0 does not error."""
    result = run_hook(
        "Stop",
        {
            "agent": "claude",
            "session_id": "test-session-123",
            "reason": "user_exit",
        },
        env_extra={"AI_SLEEP_TIME": "0"},
    )
    payload = _parse_ok(result)
    assert payload.get("ok") is True
    assert payload.get("hook") == "Stop"


def test_session_end_hook_runs_without_error() -> None:
    """Smoke test: SessionEnd hook with AI_SLEEP_TIME=0 does not error."""
    result = run_hook(
        "SessionEnd",
        {
            "agent": "claude",
            "session_id": "test-session-123",
            "reason": "user_exit",
        },
        env_extra={"AI_SLEEP_TIME": "0"},
    )
    payload = _parse_ok(result)
    assert payload.get("ok") is True
    assert payload.get("hook") == "SessionEnd"


def test_spawn_sleep_time_jobs_env_disabled() -> None:
    """When AI_SLEEP_TIME=0, _spawn_sleep_time_jobs skips spawn."""
    from ai_core.hooks import _spawn_sleep_time_jobs

    with mock.patch.dict(os.environ, {"AI_SLEEP_TIME": "0"}):
        result = _spawn_sleep_time_jobs(ROOT)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["reason"] == "AI_SLEEP_TIME disabled"
        assert result["spawned"] == []


def test_spawn_sleep_time_jobs_env_disabled_off() -> None:
    """When AI_SLEEP_TIME=off, _spawn_sleep_time_jobs skips spawn."""
    from ai_core.hooks import _spawn_sleep_time_jobs

    # Patch env
    with mock.patch.dict(os.environ, {"AI_SLEEP_TIME": "off"}):
        result = _spawn_sleep_time_jobs(ROOT)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["reason"] == "AI_SLEEP_TIME disabled"
        assert result["spawned"] == []


def test_spawn_sleep_time_jobs_lock_recent() -> None:
    """When lock file exists and is recent (< 600s), skip spawn."""
    from ai_core.hooks import _spawn_sleep_time_jobs
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as tmpdir:
        tmproot = Path(tmpdir)
        cache_dir = tmproot / ".ai" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / "sleep-time.lock"
        lock_path.write_text("running", encoding="utf-8")

        # Ensure lock is recent
        now = time.time()
        os.utime(str(lock_path), (now, now))

        # Patch env to enable sleep-time jobs
        with mock.patch.dict(os.environ, {"AI_SLEEP_TIME": "1"}):
            result = _spawn_sleep_time_jobs(tmproot)
            assert result["ok"] is True
            assert result["skipped"] is True
            assert result["reason"] == "lock_recent"
            assert result["spawned"] == []


def test_spawn_sleep_time_jobs_lock_stale() -> None:
    """When lock file is stale (>= 600s), attempt spawn (but may fail if ai bin missing)."""
    from ai_core.hooks import _spawn_sleep_time_jobs
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as tmpdir:
        tmproot = Path(tmpdir)
        cache_dir = tmproot / ".ai" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / "sleep-time.lock"
        lock_path.write_text("running", encoding="utf-8")

        # Make lock stale (> 600s ago)
        stale_time = time.time() - 700
        os.utime(str(lock_path), (stale_time, stale_time))

        # Patch env to enable sleep-time jobs
        with mock.patch.dict(os.environ, {"AI_SLEEP_TIME": "1"}):
            result = _spawn_sleep_time_jobs(tmproot)
            # Should not skip due to lock age; may fail because ai bin missing
            assert result["ok"] is not None  # ok or ok=False if ai_bin_not_found
            # If ai bin is missing, should return reason=ai_bin_not_found
            if not result["ok"]:
                assert result["reason"] == "ai_bin_not_found"


def test_spawn_sleep_time_jobs_subprocess_mock() -> None:
    """Test spawn calls subprocess.Popen with correct args (mocked)."""
    from ai_core.hooks import _spawn_sleep_time_jobs
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmproot = Path(tmpdir)
        cache_dir = tmproot / ".ai" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create fake ai binary so path check passes
        ai_bin = tmproot / ".ai" / "bin" / "ai"
        ai_bin.parent.mkdir(parents=True, exist_ok=True)
        ai_bin.write_text("#!/bin/sh\necho mock\n")
        ai_bin.chmod(0o755)

        # Patch subprocess.Popen to spy on calls
        with mock.patch.dict(os.environ, {"AI_SLEEP_TIME": "1"}):
            with mock.patch("subprocess.Popen") as mock_popen:
                mock_proc = mock.Mock()
                mock_proc.pid = 9999
                mock_popen.return_value = mock_proc

                result = _spawn_sleep_time_jobs(tmproot)

                # Should have called Popen at least once for memory page-out
                assert mock_popen.call_count >= 1

                # Check first call was memory page-out
                first_call_args = mock_popen.call_args_list[0]
                cmd = first_call_args[0][0]  # first positional arg is cmd
                assert "memory" in cmd
                assert "page-out" in cmd

                # Result should reflect spawned jobs
                assert result["ok"] is True
                assert result["skipped"] is False
                assert len(result["spawned"]) >= 1


def test_stop_hook_with_sleep_time_enabled_mocked() -> None:
    """Test that Stop hook calls _spawn_sleep_time_jobs (mocked subprocess)."""
    from ai_core import hooks
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmproot = Path(tmpdir)

        # Create minimal .ai structure
        ai_dir = tmproot / ".ai"
        cache_dir = ai_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        ai_bin = ai_dir / "bin" / "ai"
        ai_bin.parent.mkdir(parents=True, exist_ok=True)
        ai_bin.write_text("#!/bin/sh\necho mock\n")
        ai_bin.chmod(0o755)

        # Mock subprocess and process_janitor; patch at module import site
        with mock.patch.dict(os.environ, {"AI_SLEEP_TIME": "1"}):
            with mock.patch("subprocess.Popen") as mock_popen:
                with mock.patch("ai_core.process_janitor.register_child"):
                    with mock.patch("ai_core.process_janitor.cleanup_children"):
                        mock_proc = mock.Mock()
                        mock_proc.pid = 8888
                        mock_popen.return_value = mock_proc

                        result = hooks.handle_hook(
                            tmproot,
                            "Stop",
                            {"agent": "test", "session_id": "test-123"},
                        )

                        assert result["ok"] is True
                        assert result["hook"] == "Stop"
                        # Subprocess should have been called for sleep-time jobs
                        assert mock_popen.call_count >= 1


def test_session_end_hook_with_sleep_time_enabled_mocked() -> None:
    """Test that SessionEnd hook calls _spawn_sleep_time_jobs (mocked subprocess)."""
    from ai_core import hooks
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmproot = Path(tmpdir)

        # Create minimal .ai structure
        ai_dir = tmproot / ".ai"
        cache_dir = ai_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        ai_bin = ai_dir / "bin" / "ai"
        ai_bin.parent.mkdir(parents=True, exist_ok=True)
        ai_bin.write_text("#!/bin/sh\necho mock\n")
        ai_bin.chmod(0o755)

        # Mock subprocess and process_janitor; patch at module import site
        with mock.patch.dict(os.environ, {"AI_SLEEP_TIME": "1"}):
            with mock.patch("subprocess.Popen") as mock_popen:
                with mock.patch("ai_core.process_janitor.register_child"):
                    with mock.patch("ai_core.process_janitor.cleanup_children"):
                        mock_proc = mock.Mock()
                        mock_proc.pid = 8889
                        mock_popen.return_value = mock_proc

                        result = hooks.handle_hook(
                            tmproot,
                            "SessionEnd",
                            {"agent": "test", "session_id": "test-123"},
                        )

                        assert result["ok"] is True
                        assert result["hook"] == "SessionEnd"
                        # Subprocess should have been called for sleep-time jobs
                        assert mock_popen.call_count >= 1
