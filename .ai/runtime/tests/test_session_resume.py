from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.session_resume import (  # noqa: E402
    RESUME_MAX_BYTES,
    RESUME_RETENTION_DAYS,
    prune_snapshots,
    read_latest_snapshot,
    write_snapshot,
)


def _memory(root: Path) -> Path:
    mem = root / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return mem


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _read_snapshot(root: Path, session_id: str) -> dict:
    path = root / ".ai" / "memory" / "sessions" / session_id / "resume.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_snapshot_creates_file_with_schema_version_and_session_id(tmp_path: Path) -> None:
    _memory(tmp_path)
    sid = "sess-001"
    res = write_snapshot(tmp_path, session_id=sid, agent="claude")
    assert res["ok"] is True
    snap_path = tmp_path / ".ai" / "memory" / "sessions" / sid / "resume.json"
    assert snap_path.is_file()
    assert Path(res["path"]) == snap_path
    payload = json.loads(snap_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["session_id"] == sid
    assert payload["agent"] == "claude"
    assert "written_at" in payload


def test_write_snapshot_pulls_decisions_tail_last_five(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    decisions = [{"id": f"d{i}", "text": f"decision-{i}"} for i in range(8)]
    _write_jsonl(mem / "decisions.jsonl", decisions)
    write_snapshot(tmp_path, session_id="s2", agent="claude")
    payload = _read_snapshot(tmp_path, "s2")
    tail = payload["decisions_tail"]
    assert len(tail) == 5
    assert [d["id"] for d in tail] == ["d3", "d4", "d5", "d6", "d7"]


def test_write_snapshot_filters_done_todos(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    todos = [
        {"id": "t1", "status": "open", "text": "a"},
        {"id": "t2", "status": "done", "text": "b"},
        {"id": "t3", "status": "in_progress", "text": "c"},
        {"id": "t4", "status": "closed", "text": "d"},
        {"id": "t5", "status": "open", "text": "e"},
        {"id": "t6", "status": "completed", "text": "f"},
        {"id": "t7", "status": "open", "text": "g"},
        {"id": "t8", "status": "cancelled", "text": "h"},
        {"id": "t9", "status": "canceled", "text": "i"},
        {"id": "t10", "status": "open", "text": "j"},
        {"id": "t11", "status": "open", "text": "k"},
        {"id": "t12", "status": "open", "text": "l"},
    ]
    _write_jsonl(mem / "todos.jsonl", todos)
    write_snapshot(tmp_path, session_id="s3", agent="codex")
    payload = _read_snapshot(tmp_path, "s3")
    open_ids = [t["id"] for t in payload["todos_open"]]
    # Only open-ish items, capped at 5, preserving order
    assert open_ids == ["t5", "t7", "t10", "t11", "t12"]


def test_write_snapshot_uses_latest_todo_status_per_id(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    _write_jsonl(
        mem / "todos.jsonl",
        [
            {"id": "t1", "status": "open", "text": "stale open"},
            {"id": "t1", "status": "done", "text": "closed"},
            {"id": "t2", "status": "open", "text": "still open"},
        ],
    )
    write_snapshot(tmp_path, session_id="latest-todo", agent="codex")
    payload = _read_snapshot(tmp_path, "latest-todo")
    open_ids = [t["id"] for t in payload["todos_open"]]
    assert open_ids == ["t2"]


def test_write_snapshot_includes_session_tail_last_12_lines(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    lines = [f"line-{i}" for i in range(30)]
    (mem / "session-current.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_snapshot(tmp_path, session_id="s4", agent="claude")
    payload = _read_snapshot(tmp_path, "s4")
    tail = payload["session_tail"]
    expected = "\n".join([f"line-{i}" for i in range(18, 30)])
    assert tail == expected


def test_write_snapshot_caps_size_dropping_fields_in_priority(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    # decisions: small but present
    _write_jsonl(mem / "decisions.jsonl", [{"id": f"d{i}", "text": "ok"} for i in range(5)])
    # huge audit.jsonl with many distinct actions
    audit_rows = [{"action": f"action_name_{i}_" + ("x" * 200)} for i in range(50)]
    _write_jsonl(mem / "audit.jsonl", audit_rows)
    # huge todos
    todos = [{"id": f"t{i}", "status": "open", "text": "y" * 500} for i in range(20)]
    _write_jsonl(mem / "todos.jsonl", todos)
    # huge session_tail
    (mem / "session-current.md").write_text(
        "\n".join("z" * 400 for _ in range(40)),
        encoding="utf-8",
    )

    write_snapshot(tmp_path, session_id="big", agent="claude")
    snap_path = tmp_path / ".ai" / "memory" / "sessions" / "big" / "resume.json"
    raw = snap_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    # Size is bounded
    assert len(raw.encode("utf-8")) <= RESUME_MAX_BYTES
    # decisions_tail must remain
    assert "decisions_tail" in payload
    assert len(payload["decisions_tail"]) == 5
    # session_id retained
    assert payload["session_id"] == "big"
    # audit_tail_actions dropped first
    assert "audit_tail_actions" not in payload


def test_write_snapshot_redacts_secrets(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    secret = "AKIA" + ("A" * 16)
    decisions = [{"id": "d1", "text": f"key={secret}"}]
    _write_jsonl(mem / "decisions.jsonl", decisions)
    write_snapshot(tmp_path, session_id="redact", agent="claude")
    snap_path = tmp_path / ".ai" / "memory" / "sessions" / "redact" / "resume.json"
    raw = snap_path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "[REDACTED]" in raw


def test_write_snapshot_atomic_no_partial(tmp_path: Path) -> None:
    _memory(tmp_path)
    write_snapshot(tmp_path, session_id="atomic", agent="claude")
    session_dir = tmp_path / ".ai" / "memory" / "sessions" / "atomic"
    assert (session_dir / "resume.json").is_file()
    assert not (session_dir / "resume.json.tmp").exists()
    contents = sorted(p.name for p in session_dir.iterdir())
    assert contents == ["resume.json"]


def test_read_latest_snapshot_returns_newest(tmp_path: Path) -> None:
    _memory(tmp_path)
    write_snapshot(tmp_path, session_id="old", agent="claude")
    write_snapshot(tmp_path, session_id="middle", agent="claude")
    write_snapshot(tmp_path, session_id="new", agent="claude")
    base = tmp_path / ".ai" / "memory" / "sessions"
    now = time.time()
    os.utime(base / "old" / "resume.json", (now - 300, now - 300))
    os.utime(base / "middle" / "resume.json", (now - 200, now - 200))
    os.utime(base / "new" / "resume.json", (now - 100, now - 100))
    got = read_latest_snapshot(tmp_path)
    assert got is not None
    assert got["session_id"] == "new"


def test_read_latest_snapshot_excludes_current_session(tmp_path: Path) -> None:
    _memory(tmp_path)
    write_snapshot(tmp_path, session_id="older", agent="claude")
    write_snapshot(tmp_path, session_id="newer", agent="claude")
    base = tmp_path / ".ai" / "memory" / "sessions"
    now = time.time()
    os.utime(base / "older" / "resume.json", (now - 500, now - 500))
    os.utime(base / "newer" / "resume.json", (now - 50, now - 50))
    got = read_latest_snapshot(tmp_path, exclude_session_id="newer")
    assert got is not None
    assert got["session_id"] == "older"


def test_prune_snapshots_deletes_old_kept_recent(tmp_path: Path) -> None:
    _memory(tmp_path)
    write_snapshot(tmp_path, session_id="recent", agent="claude")
    write_snapshot(tmp_path, session_id="ancient", agent="claude")
    base = tmp_path / ".ai" / "memory" / "sessions"
    old_ts = time.time() - 30 * 86400
    os.utime(base / "ancient" / "resume.json", (old_ts, old_ts))
    res = prune_snapshots(tmp_path, older_than_days=14)
    assert res["ok"] is True
    assert res["removed"] == 1
    assert res["kept"] == 1
    assert (base / "recent" / "resume.json").is_file()
    assert not (base / "ancient" / "resume.json").exists()
    # session directory itself must remain (we delete the file, not the dir)
    assert (base / "ancient").is_dir()


# ---- P1/P2: handoff + cross-machine provenance ----

def test_write_handoff_roundtrip_and_embedded_in_snapshot(tmp_path: Path) -> None:
    from ai_core.session_resume import read_handoff, write_handoff

    _memory(tmp_path)
    write_handoff(
        tmp_path,
        goal="ship cross-machine continuity",
        next_step="implement P1",
        plan=["P1 handoff", "P2 pointers"],
        agent="claude",
    )
    h = read_handoff(tmp_path)
    assert h["goal"].startswith("ship") and h["next_step"] == "implement P1"
    assert h["plan"] == ["P1 handoff", "P2 pointers"]
    assert h["agent"] == "claude" and h["machine_id"]

    write_snapshot(tmp_path, session_id="s-h", agent="claude")
    snap = _read_snapshot(tmp_path, "s-h")
    assert snap["handoff"]["goal"].startswith("ship")
    assert snap["machine_id"] == h["machine_id"]
    assert snap["resume_hint"] == "claude --resume s-h"


def test_handoff_partial_update_and_clear(tmp_path: Path) -> None:
    from ai_core.session_resume import read_handoff, write_handoff

    write_handoff(tmp_path, goal="G", next_step="N1")
    write_handoff(tmp_path, next_step="N2")  # only next_step changes
    h = read_handoff(tmp_path)
    assert h["goal"] == "G" and h["next_step"] == "N2"
    write_handoff(tmp_path, clear=True)
    assert not read_handoff(tmp_path).get("goal")


def test_handoff_survives_size_cap(tmp_path: Path, monkeypatch) -> None:
    import ai_core.session_resume as sr

    # handoff is NOT in the drop order → never dropped by _shrink_to_fit.
    assert "handoff" not in sr._DROP_ORDER

    mem = _memory(tmp_path)
    _write_jsonl(mem / "todos.jsonl", [{"id": f"t{i}", "title": "todo " + "z" * 80, "status": "open"} for i in range(20)])
    (mem / "session-current.md").write_text(
        "\n".join(f"- [2026-05-29T00:00:00Z] bulky note {i} " + "x" * 80 for i in range(40)),
        encoding="utf-8",
    )
    sr.write_handoff(tmp_path, goal="KEEPME", next_step="KEEP_NEXT")
    monkeypatch.setattr(sr, "RESUME_MAX_BYTES", 600)  # force shrinking
    sr.write_snapshot(tmp_path, session_id="s-cap", agent="codex")
    snap = _read_snapshot(tmp_path, "s-cap")
    assert snap["handoff"]["goal"] == "KEEPME"  # protected from the cap
    # a bulky droppable field was sacrificed instead
    assert "session_tail" not in snap or "todos_open" not in snap


def test_handoff_fallback_from_open_todo(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    _write_jsonl(mem / "todos.jsonl", [{"id": "t1", "title": "finish the migration", "status": "open"}])
    write_snapshot(tmp_path, session_id="s-fb", agent="claude")
    snap = _read_snapshot(tmp_path, "s-fb")
    assert snap["handoff"]["next_step"] == "finish the migration"
    assert snap["handoff"]["derived_from"] == "open_todo"


def test_machine_id_stable_and_resume_hints(tmp_path: Path) -> None:
    from ai_core.session_resume import _resume_hint, machine_id

    first = machine_id(tmp_path)
    assert first and machine_id(tmp_path) == first  # cached/stable
    assert _resume_hint("claude", "S1") == "claude --resume S1"
    assert _resume_hint("antigravity", "C9") == "agy --conversation=C9"
    assert _resume_hint("codex", "anything") == "codex resume"
