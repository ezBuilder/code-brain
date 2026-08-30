from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import hooks, lessons  # noqa: E402


def test_global_lessons_context_keeps_only_actionable_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_LESSONS_INJECT", "1")
    monkeypatch.setattr(
        lessons,
        "score_lessons",
        lambda _root, **_kwargs: {
            "items": [
                {"confidence": 0.9, "command": "make doctor", "kind": "verification"},
                {"confidence": 0.8, "failure": "1 acceptance commands", "kind": "acceptance"},
                {
                    "confidence": 0.7,
                    "fix": "Run the closest targeted test before claiming the changed behavior works.",
                    "kind": "verification",
                },
            ]
        },
    )

    context = hooks._lessons_context(tmp_path, "SessionStart")

    assert "closest targeted test" in context
    assert "make doctor" not in context
    assert "acceptance commands" not in context


def test_global_lessons_context_omits_non_actionable_only_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_LESSONS_INJECT", "1")
    monkeypatch.setattr(
        lessons,
        "score_lessons",
        lambda _root, **_kwargs: {
            "items": [{"confidence": 0.95, "command": "make doctor", "kind": "verification"}]
        },
    )

    assert hooks._lessons_context(tmp_path, "SessionStart") == ""


def test_session_context_deduplicates_snapshot_against_live_memory(tmp_path: Path) -> None:
    memory = tmp_path / ".ai" / "memory"
    snapshot = memory / "sessions" / "prior" / "resume.json"
    snapshot.parent.mkdir(parents=True)
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    decision = "Keep native exact lookup on the fast path"
    todo = "Verify the context budget"
    tail = "- targeted verification passed"
    (memory / "decisions.jsonl").write_text(
        json.dumps({"decision": decision}) + "\n", encoding="utf-8"
    )
    (memory / "todos.jsonl").write_text(
        json.dumps({"title": todo, "status": "open"}) + "\n", encoding="utf-8"
    )
    (memory / "session-current.md").write_text(tail + "\n", encoding="utf-8")
    snapshot.write_text(
        json.dumps(
            {
                "session_id": "prior",
                "written_at": "2026-08-30T00:00:00Z",
                "decisions_tail": [{"decision": decision}],
                "todos_open": [{"title": todo}],
                "session_tail": tail,
                "handoff": {"next_step": "Continue the bounded optimization"},
            }
        ),
        encoding="utf-8",
    )

    context = hooks.build_context(
        "SessionStart",
        {"agent": "claude", "dry": True, "session_id": "current"},
        root=tmp_path,
    )

    assert "Continue the bounded optimization" in context
    assert context.count(decision) == 1
    assert context.count(todo) == 1
    assert context.count(tail) == 1
