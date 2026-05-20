from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.obs import _cache_hit_metrics, _usage_totals_only, usage_report  # noqa: E402


def test_cache_hit_metrics_zero_input_returns_zero_ratio() -> None:
    out = _cache_hit_metrics({})
    assert out["cache_hit_ratio"] == 0.0
    assert out["total_input_with_cache"] == 0
    assert out["effective_input_tokens"] == 0
    assert out["cache_read_input_tokens"] == 0
    assert out["cache_creation_input_tokens"] == 0


def test_cache_hit_metrics_full_hit() -> None:
    out = _cache_hit_metrics(
        {"input_tokens": 0, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}
    )
    assert out["cache_hit_ratio"] == 1.0
    assert out["total_input_with_cache"] == 1000
    # cache reads cost ~10% of fresh input tokens.
    assert out["effective_input_tokens"] == pytest.approx(100.0)


def test_cache_hit_metrics_partial_hit() -> None:
    out = _cache_hit_metrics(
        {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}
    )
    assert out["cache_hit_ratio"] == 0.9
    assert out["total_input_with_cache"] == 1000
    # 100 + 900*0.1 = 190
    assert out["effective_input_tokens"] == pytest.approx(190.0)


def test_cache_hit_metrics_with_cache_creation_penalty() -> None:
    out = _cache_hit_metrics(
        {"input_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 200}
    )
    # ratio: 0 / (100 + 0 + 200) = 0.0
    assert out["cache_hit_ratio"] == 0.0
    # effective: 100 + 0 + 200*1.25 = 350
    assert out["effective_input_tokens"] == pytest.approx(350.0)


def test_usage_totals_only_preserves_existing_fields_and_adds_cache_metrics() -> None:
    raw = {
        "ok": True,
        "source": "claude_transcript",
        "sessions_scanned": 3,
        "sessions_matched": 2,
        "messages": 5,
        "tokens": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
        "total_observed_tokens": 1050,
    }
    compact = _usage_totals_only(raw)
    # existing fields preserved
    assert compact["ok"] is True
    assert compact["source"] == "claude_transcript"
    assert compact["sessions_scanned"] == 3
    assert compact["sessions_matched"] == 2
    assert compact["messages"] == 5
    assert compact["tokens"] == raw["tokens"]
    assert compact["total_observed_tokens"] == 1050
    # new field
    assert "cache_metrics" in compact
    assert compact["cache_metrics"]["cache_hit_ratio"] == 0.9
    assert compact["cache_metrics"]["total_input_with_cache"] == 1000


def test_usage_totals_only_codex_shape_keys_preserved() -> None:
    raw = {
        "ok": True,
        "source": "codex_transcript",
        "sessions_scanned": 1,
        "sessions_matched": 1,
        "messages": 0,
        "user_messages": 2,
        "agent_messages": 3,
        "turns": 1,
        "tokens": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "total_observed_tokens": 15,
    }
    compact = _usage_totals_only(raw)
    assert compact["user_messages"] == 2
    assert compact["agent_messages"] == 3
    assert compact["turns"] == 1
    # codex token dict lacks cache_* fields; metrics fall back to zero cleanly.
    assert compact["cache_metrics"]["cache_hit_ratio"] == 0.0
    assert compact["cache_metrics"]["effective_input_tokens"] == pytest.approx(10.0)


def _write_claude_transcript(home: Path, repo: Path, tokens: dict[str, int]) -> None:
    transcript = home / "projects" / "fake-proj" / "session-1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "requestId": "req-1",
        "sessionId": "session-1",
        "timestamp": "2026-05-19T00:00:00.000Z",
        "cwd": str(repo),
        "message": {
            "model": "claude-opus-4-7",
            "usage": tokens,
        },
    }
    transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_usage_report_includes_kpi_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude_home = tmp_path / "claude-home"
    codex_home = tmp_path / "no-codex-home"
    _write_claude_transcript(
        claude_home,
        repo,
        tokens={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    )
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = usage_report(repo)
    assert result["ok"] is True
    assert "kpi" in result
    kpi = result["kpi"]
    # 900 / (100 + 900 + 0) = 0.9
    assert kpi["claude_cache_hit_ratio"] == 0.9
    # 1050 total / 1 message = 1050
    assert kpi["tokens_per_message"] == 1050
    # 100 + 900*0.1 = 190
    assert kpi["effective_input_tokens"] == pytest.approx(190.0)
    # compact mode also carries cache_metrics on the claude block.
    assert "cache_metrics" in result["actual_token_usage"]["claude"]
    assert result["actual_token_usage"]["claude"]["cache_metrics"]["cache_hit_ratio"] == 0.9


def test_usage_report_kpi_zero_messages_no_division(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude_home = tmp_path / "claude-home"
    codex_home = tmp_path / "no-codex-home"
    # No transcript files → messages=0, total_observed_tokens=0.
    (claude_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = usage_report(repo)
    kpi = result["kpi"]
    assert kpi["tokens_per_message"] == 0
    assert kpi["claude_cache_hit_ratio"] == 0.0
    assert kpi["effective_input_tokens"] == 0


def test_usage_report_include_sessions_still_has_kpi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude_home = tmp_path / "claude-home"
    codex_home = tmp_path / "no-codex-home"
    _write_claude_transcript(
        claude_home,
        repo,
        tokens={
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    )
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = usage_report(repo, include_sessions=True)
    assert result["kpi"]["claude_cache_hit_ratio"] == 0.9
    # include_sessions=True returns the raw payload (with "sessions") rather than the compact one.
    assert "sessions" in result["actual_token_usage"]["claude"]
