from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_core import transcripts


def _claude_record(repo: Path, *, request_id: str, tokens: int = 1) -> dict:
    return {
        "requestId": request_id,
        "sessionId": request_id,
        "timestamp": "2026-07-21T00:00:00Z",
        "cwd": str(repo),
        "message": {
            "model": "claude-test",
            "usage": {
                "input_tokens": tokens,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def _write_claude(home: Path, repo: Path, name: str, records: list[dict], *, mtime: int) -> Path:
    path = home / "projects" / "project" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_claude_summary_skips_oversized_file_and_marks_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "claude"
    _write_claude(home, repo, "small", [_claude_record(repo, request_id="small", tokens=7)], mtime=1)
    large = home / "projects" / "project" / "large.jsonl"
    large.write_bytes(b"x" * 2048)
    os.utime(large, (2, 2))
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_FILE_BYTES", 1024)
    result = transcripts.claude_usage_summary(repo, home=home)

    assert result["ok"] is True
    assert result["complete"] is False
    assert result["partial"] is True
    assert result["sessions_scanned"] == 1
    assert result["sessions_matched"] == 1
    assert result["tokens"]["input_tokens"] == 7
    assert result["scan"]["sessions_discovered"] == 2
    assert result["scan"]["skip_counts"] == {"file_too_large": 1}
    assert result["scan"]["bytes_skipped"] >= 2048


def test_oversized_jsonl_line_is_not_materialized_or_counted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "claude"
    path = home / "projects" / "project" / "huge-line.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"blob": "x" * 4000}) + "\n", encoding="utf-8")
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_LINE_BYTES", 128)
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_FILE_BYTES", 10_000)

    result = transcripts.claude_usage_summary(repo, home=home)

    assert result["complete"] is False
    assert result["sessions_scanned"] == 1
    assert result["sessions_matched"] == 0
    assert result["scan"]["sessions_skipped"] == 0
    assert result["scan"]["sessions_partial"] == 1
    assert result["scan"]["skip_counts"] == {}
    assert result["scan"]["warning_counts"] == {"line_byte_limit": 1}
    assert result["total_observed_tokens"] == 0


def test_invalid_json_line_marks_session_partial_instead_of_silent_undercount(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "claude"
    path = _write_claude(
        home,
        repo,
        "invalid",
        [_claude_record(repo, request_id="valid", tokens=50)],
        mtime=1,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"unfinished":\n')

    result = transcripts.claude_usage_summary(repo, home=home)

    assert result["complete"] is False
    assert result["sessions_scanned"] == 1
    assert result["sessions_matched"] == 1
    assert result["total_observed_tokens"] == 50
    assert result["scan"]["invalid_lines"] == 1
    assert result["scan"]["sessions_partial"] == 1
    assert result["scan"]["skip_counts"] == {}
    assert result["scan"]["warning_counts"] == {"invalid_json_line": 1}


def test_latest_sessions_are_selected_when_session_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "claude"
    for index in range(3):
        _write_claude(
            home,
            repo,
            f"session-{index}",
            [_claude_record(repo, request_id=f"req-{index}", tokens=index + 1)],
            mtime=index + 1,
        )
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_SESSIONS", 2)
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_CANDIDATES", 10)

    result = transcripts.claude_usage_summary(repo, home=home)

    assert result["complete"] is False
    assert result["sessions_scanned"] == 2
    assert result["sessions_matched"] == 2
    assert result["tokens"]["input_tokens"] == 5
    assert result["scan"]["skip_counts"] == {"session_limit": 1}


def test_dedupe_budget_stops_session_instead_of_silently_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "claude"
    _write_claude(
        home,
        repo,
        "dedupe",
        [
            _claude_record(repo, request_id="one", tokens=10),
            _claude_record(repo, request_id="two", tokens=20),
        ],
        mtime=1,
    )
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_DEDUPE_KEYS", 1)

    result = transcripts.claude_usage_summary(repo, home=home)

    assert result["complete"] is False
    assert result["sessions_scanned"] == 0
    assert result["sessions_matched"] == 0
    assert result["total_observed_tokens"] == 0
    assert result["scan"]["skip_counts"] == {"dedupe_key_limit": 1}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_transcript_symlink_is_reported_and_never_followed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "claude"
    project = home / "projects" / "project"
    project.mkdir(parents=True)
    external = tmp_path / "external.jsonl"
    external.write_text(json.dumps(_claude_record(repo, request_id="external", tokens=99)) + "\n", encoding="utf-8")
    (project / "linked.jsonl").symlink_to(external)

    result = transcripts.claude_usage_summary(repo, home=home)

    assert result["complete"] is False
    assert result["sessions_scanned"] == 0
    assert result["total_observed_tokens"] == 0
    assert result["scan"]["skip_counts"] == {"unsafe_symlink": 1}
    assert result["scan"]["skipped"][0]["path"].endswith("linked.jsonl")


def test_codex_summary_exposes_complete_scan_contract_when_home_missing(tmp_path: Path) -> None:
    result = transcripts.codex_usage_summary(tmp_path, home=tmp_path / "missing")

    assert result["complete"] is True
    assert result["partial"] is False
    assert result["scan"]["sessions_discovered"] == 0
    assert result["scan"]["policy"]["max_file_bytes"] == transcripts.TRANSCRIPT_MAX_FILE_BYTES
