from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import sandbox  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_ci(monkeypatch):
    """Ensure tests do not write audit events into the real repo."""
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_CI", "1")


def test_execute_runs_command_and_stores_output(tmp_path: Path) -> None:
    result = sandbox.execute(tmp_path, command=["echo", "hello"])
    assert result["ok"] is True
    # Short output → compact mode ("output" field). Either way, payload must contain "hello".
    combined = result.get("output") if "output" in result else "\n".join(result.get("first_lines", []))
    assert combined is not None and "hello" in combined

    out_path = tmp_path / ".ai" / "cache" / "sandbox" / f"{result['exec_id']}.txt"
    assert out_path.exists()
    mode = stat.S_IMODE(out_path.stat().st_mode)
    assert mode == 0o600
    assert out_path.read_text(encoding="utf-8") == "hello\n"


def test_execute_summary_excludes_full_output_when_large(tmp_path: Path) -> None:
    cmd = ["bash", "-c", "for i in $(seq 1 200); do echo line-$i; done"]
    result = sandbox.execute(tmp_path, command=cmd)
    assert result["ok"] is True
    assert result["total_lines"] == 200
    assert len(result["first_lines"]) == 30
    assert len(result["last_lines"]) == 5
    # Last 5 lines should be 196..200
    assert result["last_lines"][-1] == "line-200"


def test_execute_handles_timeout(tmp_path: Path) -> None:
    result = sandbox.execute(tmp_path, command=["sleep", "5"], timeout=1)
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert result["termination"]["classification"] == "timeout"
    assert result["elapsed_ms"] >= 900


def test_execute_handles_command_not_found(tmp_path: Path) -> None:
    result = sandbox.execute(tmp_path, command=["this-command-does-not-exist-xyz"])
    assert result["ok"] is False
    assert result["reason"] == "command_not_found"
    sandbox_dir = tmp_path / ".ai" / "cache" / "sandbox"
    assert list(sandbox_dir.glob("*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal return codes")
def test_execute_classifies_sigkill_and_persists_evidence(tmp_path: Path) -> None:
    result = sandbox.execute(tmp_path, command=["bash", "-c", "kill -9 $$"])

    assert result["ok"] is True
    assert result["command_ok"] is False
    assert result["exit_code"] == -9
    assert result["termination"]["signal"] == "SIGKILL"
    assert result["termination"]["classification"] in {
        "cgroup_oom_kill_confirmed",
        "cgroup_memory_limit_confirmed",
        "cgroup_memory_limit_likely",
        "host_memory_pressure_likely",
        "external_sigkill_or_execution_limit",
    }
    meta_path = tmp_path / ".ai" / "cache" / "sandbox" / f"{result['exec_id']}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["termination"]["signal"] == "SIGKILL"
    assert "memory_before" in meta
    assert "memory_after" in meta


def test_execute_streams_and_caps_large_output(tmp_path: Path) -> None:
    source_bytes = sandbox._CAPTURE_MAX_BYTES + 1_000_000
    result = sandbox.execute(
        tmp_path,
        command=[sys.executable, "-c", f"import sys; sys.stdout.write('x' * {source_bytes})"],
    )

    assert result["ok"] is True
    assert result["command_ok"] is True
    assert result["source_total_bytes"] == source_bytes
    assert result["output_truncated"] is True
    assert result["total_bytes"] <= sandbox._CAPTURE_MAX_BYTES + 256
    out_path = tmp_path / ".ai" / "cache" / "sandbox" / f"{result['exec_id']}.txt"
    assert out_path.stat().st_size <= sandbox._CAPTURE_MAX_BYTES + 256


def test_execute_stops_unbounded_output_at_source_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_SOURCE_CAPTURE_MAX_BYTES", 1_000_000)
    result = sandbox.execute(
        tmp_path,
        command=[
            sys.executable,
            "-c",
            "import sys; chunk='x'*65536\nwhile True: sys.stdout.write(chunk); sys.stdout.flush()",
        ],
        timeout=10,
    )

    assert result["ok"] is False
    assert result["command_ok"] is False
    assert result["reason"] == "output_limit_exceeded"
    assert result["termination"]["classification"] == "output_limit_exceeded"
    assert result["source_total_bytes"] > 1_000_000
    out_path = tmp_path / ".ai" / "cache" / "sandbox" / f"{result['exec_id']}.txt"
    assert out_path.stat().st_size <= 1_000_256


def test_execute_rejects_cwd_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    result = sandbox.execute(tmp_path, command=["pwd"], cwd=str(outside))
    assert result == {"ok": False, "reason": "cwd_outside_root"}


def test_execute_rejects_symlink_cwd_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    result = sandbox.execute(tmp_path, command=["pwd"], cwd="escape")
    assert result == {"ok": False, "reason": "cwd_outside_root"}


def test_execute_allows_relative_cwd_inside_repo(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    result = sandbox.execute(tmp_path, command=["pwd"], cwd="work")
    assert result["ok"] is True
    assert str(work) in result["output"]


def test_execute_rejects_invalid_timeout(tmp_path: Path) -> None:
    assert sandbox.execute(tmp_path, command=["echo", "x"], timeout=0)["reason"] == "invalid_timeout"
    assert sandbox.execute(tmp_path, command=["echo", "x"], timeout=901)["reason"] == "invalid_timeout"


def test_execute_redacts_secrets_in_summary(tmp_path: Path) -> None:
    secret = "AKIA" + "A" * 16
    result = sandbox.execute(tmp_path, command=["echo", secret])
    assert result["ok"] is True
    joined = result.get("output") if "output" in result else "\n".join(result.get("first_lines", []))
    assert joined is not None and secret not in joined
    assert "[REDACTED]" in joined

    out_path = tmp_path / ".ai" / "cache" / "sandbox" / f"{result['exec_id']}.txt"
    stored = out_path.read_text(encoding="utf-8")
    assert secret not in stored
    assert "[REDACTED]" in stored


def test_fetch_returns_line_range(tmp_path: Path) -> None:
    cmd = ["bash", "-c", "for i in $(seq 1 50); do echo line-$i; done"]
    result = sandbox.execute(tmp_path, command=cmd)
    fetched = sandbox.fetch(tmp_path, exec_id=result["exec_id"], line_start=10, line_end=15)
    assert fetched["ok"] is True
    assert len(fetched["lines"]) == 6
    assert fetched["lines"][0]["lineno"] == 10
    assert fetched["lines"][0]["text"] == "line-10"
    assert fetched["lines"][-1]["lineno"] == 15
    assert fetched["lines"][-1]["text"] == "line-15"


def test_fetch_grep_pattern_filters(tmp_path: Path) -> None:
    cmd = ["bash", "-c", "for i in $(seq 1 200); do echo line-$i; done"]
    result = sandbox.execute(tmp_path, command=cmd)
    fetched = sandbox.fetch(tmp_path, exec_id=result["exec_id"], grep_pattern="line-42")
    assert fetched["ok"] is True
    matches = fetched["matched_lines"]
    # "line-42" appears only once as exact match (line-142, line-242 etc not present in 1..200 except line-142)
    # Actually line-142 contains "line-42" as substring? No: "line-142" contains "line-14", not "line-42".
    # But "line-42" is plain substring. Does "line-142" contain "line-42"? No, it's "line-142", chars are l-i-n-e---1-4-2.
    # So only the literal "line-42" line matches.
    assert len(matches) == 1
    assert matches[0]["text"] == "line-42"
    assert matches[0]["lineno"] == 42


def test_fetch_missing_id_returns_not_found(tmp_path: Path) -> None:
    result = sandbox.fetch(tmp_path, exec_id="deadbeefdeadbeef")
    assert result["ok"] is False
    assert result["reason"] == "not_found"


def test_fetch_rejects_path_traversal_exec_id(tmp_path: Path) -> None:
    result = sandbox.fetch(tmp_path, exec_id="../../../victim")
    assert result == {"ok": False, "reason": "invalid_exec_id"}


def test_fetch_and_read_output_reject_symlink_artifact(tmp_path: Path) -> None:
    sandbox_dir = tmp_path / ".ai" / "cache" / "sandbox"
    sandbox_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    exec_id = "deadbeefdeadbeef"
    (sandbox_dir / f"{exec_id}.txt").symlink_to(outside)

    fetched = sandbox.fetch(tmp_path, exec_id=exec_id)
    assert fetched["ok"] is False
    assert fetched["reason"] == "invalid_artifact"
    assert sandbox.read_output(tmp_path, exec_id) is None


def test_fetch_caps_large_artifact_reads(tmp_path: Path) -> None:
    sandbox_dir = tmp_path / ".ai" / "cache" / "sandbox"
    sandbox_dir.mkdir(parents=True)
    exec_id = "0123456789abcdef"
    (sandbox_dir / f"{exec_id}.txt").write_bytes(b"x" * (sandbox._READ_OUTPUT_CAP_BYTES + 100))
    fetched = sandbox.fetch(tmp_path, exec_id=exec_id)
    assert fetched["ok"] is True
    assert fetched["truncated"] is True


def test_list_executions_orders_newest_first(tmp_path: Path) -> None:
    ids = []
    for i in range(3):
        # Ensure distinct created_at timestamps.
        time.sleep(0.01)
        result = sandbox.execute(tmp_path, command=["echo", f"run-{i}"])
        assert result["ok"] is True
        ids.append(result["exec_id"])

    listing = sandbox.list_executions(tmp_path)
    assert listing["ok"] is True
    assert listing["count"] == 3
    # Most recent (last appended) first.
    assert listing["items"][0]["exec_id"] == ids[-1]
    assert listing["items"][-1]["exec_id"] == ids[0]
    # command_summary is present and trimmed.
    assert "echo" in listing["items"][0]["command_summary"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal return codes")
def test_execution_diagnostics_classifies_recent_sigkill(tmp_path: Path) -> None:
    normal = sandbox.execute(tmp_path, command=["echo", "healthy"])
    killed = sandbox.execute(tmp_path, command=["bash", "-c", "kill -9 $$"])
    assert normal["command_ok"] is True
    assert killed["command_ok"] is False

    diagnostics = sandbox.execution_diagnostics(tmp_path)
    assert diagnostics["ok"] is True
    assert diagnostics["bounded"] is True
    assert diagnostics["metas_scanned"] == 2
    assert diagnostics["command_failures"] == 1
    assert diagnostics["killed_9"]["sigkill_total"] == 1
    assert sum(diagnostics["killed_9"].values()) >= 2
    assert diagnostics["examples"][0]["signal"] == "SIGKILL"


def test_execution_diagnostics_is_fixed_size_and_explicitly_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_META_CANDIDATE_LIMIT", 2)
    monkeypatch.setattr(sandbox, "_META_DIAGNOSTICS_LIMIT", 2)
    for index in range(5):
        result = sandbox.execute(tmp_path, command=["echo", str(index)])
        assert result["command_ok"] is True

    diagnostics = sandbox.execution_diagnostics(tmp_path)
    assert diagnostics["ok"] is True
    assert diagnostics["bounded"] is True
    assert diagnostics["complete"] is False
    assert diagnostics["partial"] is True
    assert diagnostics["metas_discovered"] == 5
    assert diagnostics["metas_scanned"] == 2
    assert diagnostics["skip_counts"]["candidate_limit"] == 3


def test_execution_diagnostics_rejects_oversized_or_symlink_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_META_MAX_BYTES", 128)
    sandbox_dir = tmp_path / ".ai" / "cache" / "sandbox"
    sandbox_dir.mkdir(parents=True)
    (sandbox_dir / "0123456789abcdef.meta.json").write_text(
        json.dumps({"exec_id": "0123456789abcdef", "padding": "x" * 256}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (sandbox_dir / "fedcba9876543210.meta.json").symlink_to(outside)

    diagnostics = sandbox.execution_diagnostics(tmp_path)
    assert diagnostics["ok"] is False
    assert diagnostics["bounded"] is True
    assert diagnostics["partial"] is True
    assert diagnostics["metas_scanned"] == 0
    assert diagnostics["skip_counts"]["metadata_too_large"] == 1
    assert diagnostics["skip_counts"]["unsafe_symlink"] == 1


def test_prune_removes_old_executions(tmp_path: Path) -> None:
    result = sandbox.execute(tmp_path, command=["echo", "old"])
    assert result["ok"] is True
    exec_id = result["exec_id"]

    sandbox_dir = tmp_path / ".ai" / "cache" / "sandbox"
    meta_path = sandbox_dir / f"{exec_id}.meta.json"
    txt_path = sandbox_dir / f"{exec_id}.txt"

    # Rewrite meta with an old created_at timestamp (1 hour ago).
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["created_at"] = "2020-01-01T00:00:00Z"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    # Mtime hack the file to be old as well, just in case.
    old_ts = time.time() - 3600
    os.utime(meta_path, (old_ts, old_ts))
    os.utime(txt_path, (old_ts, old_ts))

    pruned = sandbox.prune(tmp_path, older_than_seconds=10)
    assert pruned["ok"] is True
    assert pruned["removed_count"] == 1
    assert not meta_path.exists()
    assert not txt_path.exists()


def test_prune_ignores_malicious_metadata_exec_id(tmp_path: Path) -> None:
    sandbox_dir = tmp_path / ".ai" / "cache" / "sandbox"
    sandbox_dir.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    meta_path = sandbox_dir / "deadbeefdeadbeef.meta.json"
    meta_path.write_text(
        json.dumps({"exec_id": "../../../victim", "created_at": "2020-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    pruned = sandbox.prune(tmp_path, older_than_seconds=1)
    assert pruned["removed_count"] == 0
    assert pruned["kept_count"] == 1
    assert victim.read_text(encoding="utf-8") == "keep"

def test_execute_accepts_string_command(tmp_path: Path) -> None:
    # When command is a string, sandbox runs it via `bash -lc` so heredocs/pipes work.
    result = sandbox.execute(tmp_path, command="echo hi | tr a-z A-Z")
    assert result["ok"] is True
    payload = result.get("output") if "output" in result else "\n".join(result.get("first_lines", []))
    assert payload is not None and "HI" in payload


def test_execute_compact_mode_for_short_output(tmp_path: Path) -> None:
    result = sandbox.execute(tmp_path, command=["echo", "compact-me"])
    assert result["ok"] is True
    # Short payload → raw output field, no first/last_lines arrays.
    assert "output" in result
    assert "first_lines" not in result
    assert "last_lines" not in result
    assert "compact-me" in result["output"]


def test_execute_verbose_mode_for_large_output(tmp_path: Path) -> None:
    # >20 lines forces verbose mode with first_lines/last_lines split.
    cmd = ["bash", "-c", "for i in $(seq 1 50); do echo line-$i; done"]
    result = sandbox.execute(tmp_path, command=cmd)
    assert result["ok"] is True
    assert "output" not in result
    assert isinstance(result["first_lines"], list)
    assert isinstance(result["last_lines"], list)
