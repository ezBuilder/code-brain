from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.hooks import _session_scope_summary  # noqa: E402


def _write_audit(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")


def _entry(action: str, *, kind: str | None = None, ts: str = "2026-05-22T00:00:00.000000Z") -> dict:
    payload: dict = {"agent": "unknown"}
    if kind is not None:
        payload["kind"] = kind
    return {
        "action": action,
        "category": "memory",
        "monotonic_ns": time.monotonic_ns(),
        "payload": payload,
        "prev_sha": None,
        "ts": ts,
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AI_SESSION_SCOPE_SUMMARY", "AI_SESSION_SCOPE_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)


def test_returns_empty_when_no_audit(tmp_path: Path) -> None:
    assert _session_scope_summary(tmp_path) == ""


def test_returns_empty_when_no_session_start_in_tail(tmp_path: Path) -> None:
    audit = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    _write_audit(audit, [_entry("event.append", kind="mcp.request") for _ in range(10)])
    assert _session_scope_summary(tmp_path) == ""


def test_returns_empty_below_threshold(tmp_path: Path) -> None:
    audit = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    entries = [_entry("event.append", kind="SessionStart")]
    entries.extend(_entry("event.append", kind="mcp.request") for _ in range(5))
    _write_audit(audit, entries)
    assert _session_scope_summary(tmp_path) == ""


def test_warns_when_threshold_crossed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_SESSION_SCOPE_THRESHOLD", "10")
    audit = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    entries = [_entry("event.append", kind="SessionStart")]
    entries.extend(_entry("event.append", kind="mcp.request") for _ in range(15))
    _write_audit(audit, entries)
    out = _session_scope_summary(tmp_path)
    assert out.startswith("cb-scope: 15 audit events since SessionStart")
    assert "/clear" in out


def test_resets_after_new_session_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_SESSION_SCOPE_THRESHOLD", "10")
    audit = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    entries = [_entry("event.append", kind="SessionStart")]
    entries.extend(_entry("event.append", kind="mcp.request") for _ in range(20))
    entries.append(_entry("event.append", kind="SessionStart"))
    entries.extend(_entry("event.append", kind="mcp.request") for _ in range(3))
    _write_audit(audit, entries)
    assert _session_scope_summary(tmp_path) == ""


def test_disabled_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_SESSION_SCOPE_SUMMARY", "0")
    monkeypatch.setenv("AI_SESSION_SCOPE_THRESHOLD", "5")
    audit = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    entries = [_entry("event.append", kind="SessionStart")]
    entries.extend(_entry("event.append", kind="mcp.request") for _ in range(20))
    _write_audit(audit, entries)
    assert _session_scope_summary(tmp_path) == ""


def test_threshold_floor_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Threshold values below 10 are clamped up to 10 to avoid noise."""
    monkeypatch.setenv("AI_SESSION_SCOPE_THRESHOLD", "1")
    audit = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    entries = [_entry("event.append", kind="SessionStart")]
    entries.extend(_entry("event.append", kind="mcp.request") for _ in range(5))
    _write_audit(audit, entries)
    assert _session_scope_summary(tmp_path) == ""
