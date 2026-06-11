"""events.jsonl rotation — telemetry must stay bounded (only recent patterns are mined)."""
from __future__ import annotations

import json
from pathlib import Path

from ai_core import memory


def _events_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "events" / "events.jsonl"


def test_append_event_rotates_to_recent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "EVENTS_MAX_BYTES", 2000)
    monkeypatch.setattr(memory, "EVENTS_KEEP", 10)
    for i in range(300):
        memory.append_event(tmp_path, {"hook": "PreToolUse", "i": i})
    lines = _events_path(tmp_path).read_text(encoding="utf-8").splitlines()
    # bounded well below the 300 appended, and the most recent event is retained
    assert len(lines) <= 60, len(lines)
    assert json.loads(lines[-1])["payload"]["i"] == 299


def test_append_event_no_rotate_when_small(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "EVENTS_MAX_BYTES", 10_000_000)
    for i in range(20):
        memory.append_event(tmp_path, {"hook": "PreToolUse", "i": i})
    lines = _events_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20  # under threshold → nothing dropped
