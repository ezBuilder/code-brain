from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_snapshot_memory_sources_never_follow_external_symlinks(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    external = tmp_path / "external-memory"
    external.mkdir()
    markers = {
        "decisions.jsonl": '{"id":"d1","text":"EXTERNAL_DECISION"}\n',
        "todos.jsonl": '{"id":"t1","status":"open","title":"EXTERNAL_TODO"}\n',
        "session-current.md": "EXTERNAL_SESSION_TAIL\n",
        "audit.jsonl": '{"action":"EXTERNAL_AUDIT"}\n',
    }
    for name, content in markers.items():
        target = external / name
        target.write_text(content, encoding="utf-8")
        link = mem / name
        link.symlink_to(target)

    result = write_snapshot(tmp_path, session_id="external-memory", agent="operator")
    raw = Path(result["path"]).read_text(encoding="utf-8")

    for marker in (
        "EXTERNAL_DECISION",
        "EXTERNAL_TODO",
        "EXTERNAL_SESSION_TAIL",
        "EXTERNAL_AUDIT",
    ):
        assert marker not in raw


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_snapshot_memory_sources_never_read_external_hardlinks(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    external = tmp_path / "external-decisions.jsonl"
    external.write_text('{"id":"d1","text":"EXTERNAL_HARDLINK"}\n', encoding="utf-8")
    linked = mem / "decisions.jsonl"
    os.link(external, linked)

    result = write_snapshot(tmp_path, session_id="hardlinked-memory", agent="operator")
    raw = Path(result["path"]).read_text(encoding="utf-8")

    assert "EXTERNAL_HARDLINK" not in raw


def test_write_snapshot_atomic_no_partial(tmp_path: Path) -> None:
    _memory(tmp_path)
    write_snapshot(tmp_path, session_id="atomic", agent="claude")
    session_dir = tmp_path / ".ai" / "memory" / "sessions" / "atomic"
    assert (session_dir / "resume.json").is_file()
    assert not (session_dir / "resume.json.tmp").exists()
    contents = sorted(p.name for p in session_dir.iterdir())
    assert contents == ["resume.json"]


def test_write_snapshot_maps_unsafe_session_id_to_confined_directory(tmp_path: Path) -> None:
    _memory(tmp_path)
    unsafe = "../../outside;rm -rf"

    result = write_snapshot(tmp_path, session_id=unsafe, agent="claude")

    path = Path(result["path"])
    sessions = (tmp_path / ".ai" / "memory" / "sessions").resolve()
    path.resolve().relative_to(sessions)
    assert path.parent.name.startswith("sid-")
    assert path.parent.name != unsafe
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["session_id"] == unsafe
    assert payload["resume_hint"] == "claude --resume"
    assert not (tmp_path.parent / "outside;rm -rf").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_write_snapshot_rejects_external_sessions_parent_symlink(tmp_path: Path) -> None:
    external = tmp_path.parent / (tmp_path.name + "-session-target")
    external.mkdir()
    sessions = tmp_path / ".ai" / "memory" / "sessions"
    sessions.parent.mkdir(parents=True)
    sessions.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError, match="escapes project root"):
        write_snapshot(tmp_path, session_id="safe", agent="operator")

    assert not (external / "safe").exists()


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


def test_concurrent_handoff_partial_updates_preserve_distinct_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_core.session_resume import read_handoff, write_handoff

    monkeypatch.setenv("AI_MACHINE_LABEL", "test-machine")

    def write_goal() -> None:
        write_handoff(tmp_path, goal="concurrent goal", agent="goal-agent")

    def write_next() -> None:
        write_handoff(tmp_path, next_step="concurrent next", agent="next-agent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        goal_future = pool.submit(write_goal)
        next_future = pool.submit(write_next)
        goal_future.result()
        next_future.result()

    handoff = read_handoff(tmp_path)
    assert handoff["goal"] == "concurrent goal"
    assert handoff["next_step"] == "concurrent next"
    assert handoff["machine_id"] == "test-machine"


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


def test_snapshot_exposes_context_budget_metadata(tmp_path: Path) -> None:
    _memory(tmp_path)
    write_snapshot(tmp_path, session_id="budget-meta", agent="codex", context_budget_mode="aggressive")
    snap = _read_snapshot(tmp_path, "budget-meta")

    assert snap["context_budget"]["mode"] == "aggressive"
    assert snap["context_budget"]["max_bytes"] <= RESUME_MAX_BYTES
    assert snap["context_budget"]["protected_signals"] == ["handoff", "rubric", "verdict", "blockers"]


def test_budget_shrink_preserves_handoff_rubric_verdict_blockers() -> None:
    import ai_core.session_resume as sr

    payload = {
        "handoff": {"goal": "keep handoff"},
        "rubric": "keep rubric",
        "verdict": "keep verdict",
        "blockers": ["keep blocker"],
        "audit_tail_actions": ["drop " + "a" * 200],
        "session_tail": "drop " + "b" * 200,
        "todos_open": [{"title": "drop " + "c" * 200}],
    }

    shrunk = sr._shrink_to_fit(payload, 256)

    assert shrunk["handoff"]["goal"] == "keep handoff"
    assert shrunk["rubric"] == "keep rubric"
    assert shrunk["verdict"] == "keep verdict"
    assert shrunk["blockers"] == ["keep blocker"]
    assert "audit_tail_actions" not in shrunk
    assert "session_tail" not in shrunk
    assert "todos_open" not in shrunk
    assert sr._payload_size(shrunk) <= 256


def test_budget_shrink_compacts_oversized_protected_values_to_hard_cap() -> None:
    import ai_core.session_resume as sr

    payload = {
        "schema_version": 1,
        "session_id": "safe",
        "written_at": "2026-07-20T00:00:00Z",
        "handoff": {"goal": "g" * 20_000, "next_step": "n" * 20_000},
        "rubric": "r" * 20_000,
        "verdict": "v" * 20_000,
        "blockers": ["b" * 20_000],
    }

    shrunk = sr._shrink_to_fit(payload, 512)

    assert sr._payload_size(shrunk) <= 512
    assert {"handoff", "rubric", "verdict", "blockers"}.issubset(shrunk)


def test_write_snapshot_hard_caps_extreme_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_core.session_resume as sr

    _memory(tmp_path)
    monkeypatch.setattr(sr, "RESUME_MAX_BYTES", 512)
    raw_session_id = "세션" * 50_000

    result = sr.write_snapshot(
        tmp_path,
        session_id=raw_session_id,
        agent="claude" * 1000,
    )
    path = Path(result["path"])
    raw = path.read_bytes()
    payload = json.loads(raw)

    assert len(raw) <= 512
    assert result["bytes_written"] == len(raw)
    assert payload["session_id"] != raw_session_id
    assert payload["session_id_sha256"] == hashlib.sha256(
        raw_session_id.encode("utf-8")
    ).hexdigest()
    assert payload["session_id_truncated"] is True
    assert len(payload.get("agent", "")) <= 64


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
    cache = tmp_path / ".ai" / "cache" / "machine_id"
    if os.name != "nt":
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert _resume_hint("claude", "S1") == "claude --resume S1"
    assert _resume_hint("antigravity", "C9") == "agy --conversation=C9"
    assert _resume_hint("codex", "anything") == "codex resume"


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_machine_id_replaces_external_symlink_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_core.session_resume import machine_id

    external = tmp_path / "external-machine-id"
    external.write_text("external-id", encoding="utf-8")
    cache = tmp_path / ".ai" / "cache" / "machine_id"
    cache.parent.mkdir(parents=True)
    cache.symlink_to(external)
    monkeypatch.setenv("AI_MACHINE_LABEL", "safe-local")

    result = machine_id(tmp_path)

    assert result == "safe-local"
    assert not cache.is_symlink()
    assert cache.read_text(encoding="utf-8") == "safe-local"
    assert external.read_text(encoding="utf-8") == "external-id"


@pytest.mark.skipif(os.name == "nt", reason="Unix mode semantics")
def test_machine_id_ignores_public_cache_and_rewrites_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_core.session_resume import machine_id

    cache = tmp_path / ".ai" / "cache" / "machine_id"
    cache.parent.mkdir(parents=True)
    cache.write_text("untrusted-id", encoding="utf-8")
    cache.chmod(0o644)
    monkeypatch.setenv("AI_MACHINE_LABEL", "trusted-id")

    assert machine_id(tmp_path) == "trusted-id"
    assert cache.read_text(encoding="utf-8") == "trusted-id"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_machine_id_concurrent_first_use_returns_one_stable_value(tmp_path: Path) -> None:
    from ai_core.session_resume import machine_id

    with ThreadPoolExecutor(max_workers=16) as pool:
        values = list(pool.map(lambda _index: machine_id(tmp_path), range(64)))

    assert len(set(values)) == 1
    assert values[0].startswith("cb-")
    assert machine_id(tmp_path) == values[0]
