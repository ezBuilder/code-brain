"""-004/-008: hard-forget for decisions (tombstone + compaction) and session notes.

Design gates from the 2026-08-01 memory round, each guarded here:
  - sentinel text absent from EVERY purge surface (raw bytes, both decision
    readers, the shared live filter, HOT-cache scoring, resume snapshot,
    rendered SessionStart context);
  - audit hash chain stays valid across compaction (audit rows carry ids only);
  - no-match forget leaves the file byte-identical;
  - suppression is order-independent (tombstone BEFORE its target — the shape a
    union merge produces — and a union-merge "restore" after the fact);
  - a forget never evicts real decisions from the tail window (DECISIONS_TAIL);
  - a hard-forgotten id cannot be reborn by the 32-bit id generator;
  - CLI is the only surface: --yes + --confirm-id required, CI-rejected, and no
    MCP tool exposes forget.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import cli, doctor, hooks, memory, memory_tier, mcp_server  # noqa: E402
from ai_core import session_resume as sr  # noqa: E402
from ai_core.policy import PERMISSION_DENIED, USAGE_ERROR  # noqa: E402

SENTINEL = "SENTINEL-FORGET-ME-7f3a"


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _decisions_raw(root: Path) -> str:
    p = memory.decisions_path(root)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _all_surfaces_text(root: Path) -> str:
    """Concatenated text of every decision-surfacing path."""
    plain, failures = memory.read_decisions_for_surface(root, limit=50)
    filtered = memory.read_decisions_filtered(
        root, limit=100, include_retired=True, include_expired=True
    )
    rows = memory.read_jsonl_all(memory.decisions_path(root))
    live_all = memory.live_decision_records(rows, include_retired=True, include_expired=True)
    scored = memory_tier.scored_durable_items(root)
    resume_tail = sr._decisions_tail(root)
    context = hooks.build_context("SessionStart", {"agent": "operator", "dry": True}, root=root)
    return json.dumps([plain, failures, filtered, live_all, scored, resume_tail]) + context


def test_forget_removes_body_from_every_surface_and_keeps_chain(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    keep = memory.append_decision(root, text="keep me")["record"]
    victim = memory.append_decision(root, text=f"decision {SENTINEL}")["record"]
    fail_victim = memory.append_decision(
        root, text=f"failure {SENTINEL}", kind="failure", status="confirmed"
    )["record"]

    r1 = memory.forget_decision(root, target_id=victim["id"], reason="test purge")
    r2 = memory.forget_decision(root, target_id=fail_victim["id"])
    assert r1["ok"] and r2["ok"]
    assert r1["removed_rows"] == 1
    assert r1["union_merge_restorable"] is True
    assert r1["tombstone_id"].startswith("tomb-")
    assert r1["tombstone_id"] != victim["id"]

    assert SENTINEL not in _decisions_raw(root), "compaction must remove the body bytes"
    assert SENTINEL not in _all_surfaces_text(root)
    surfaces = _all_surfaces_text(root)
    assert "keep me" in surfaces, "unrelated decisions must survive"
    assert doctor.check_audit_chain(root).ok is True


def test_forget_no_match_leaves_file_byte_identical(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="stable decision")
    before = _decisions_raw(root)

    result = memory.forget_decision(root, target_id="dec-00000000")

    assert result == {"ok": False, "reason": "no_match", "target_id": "dec-00000000"}
    assert _decisions_raw(root) == before


def test_tombstone_preceding_target_suppresses_after_union_merge(tmp_path: Path) -> None:
    """A union merge can land the tombstone BEFORE the body it kills."""
    root = _seed(tmp_path)
    path = memory.decisions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": "tomb-aaaa1111", "kind": "tombstone", "target_id": "dec-dead0001",
         "decided_at": memory.now_iso(), "source": "operator"},
        {"id": "dec-dead0001", "decided_at": memory.now_iso(),
         "decision": f"restored body {SENTINEL}", "tags": [], "source": "operator"},
        {"id": "dec-live0001", "decided_at": memory.now_iso(),
         "decision": "still alive", "tags": [], "source": "operator"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    path.chmod(0o600)

    assert SENTINEL not in _all_surfaces_text(root)
    assert "still alive" in _all_surfaces_text(root)


def test_union_merge_restore_after_forget_stays_suppressed(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    victim = memory.append_decision(root, text=f"merge restore {SENTINEL}")["record"]
    victim_line = json.dumps(
        victim, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert memory.forget_decision(root, target_id=victim["id"])["ok"]

    # merge=union re-adds the old line from a peer clone
    with memory.decisions_path(root).open("a", encoding="utf-8") as fh:
        fh.write(victim_line + "\n")

    assert SENTINEL in _decisions_raw(root), "precondition: body physically restored"
    assert SENTINEL not in _all_surfaces_text(root), "tombstone must keep suppressing it"


def test_forget_never_consumes_the_decisions_tail_window(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    victims = [memory.append_decision(root, text=f"victim {i}")["record"] for i in range(3)]
    for i in range(3):
        memory.append_decision(root, text=f"real decision {i}")
    for v in victims:
        assert memory.forget_decision(root, target_id=v["id"])["ok"]

    plain, _ = memory.read_decisions_for_surface(root, limit=hooks.DECISIONS_TAIL)
    texts = [str(r.get("decision")) for r in plain]
    assert texts == ["real decision 0", "real decision 1", "real decision 2"], (
        "three forgets must not evict real decisions from the SessionStart tail"
    )


def test_forget_preserves_unparseable_lines_and_later_appends(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    victim = memory.append_decision(root, text=f"bye {SENTINEL}")["record"]
    path = memory.decisions_path(root)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")

    assert memory.forget_decision(root, target_id=victim["id"])["ok"]
    assert "{not json at all" in _decisions_raw(root), "never destroy what we cannot parse"

    added = memory.append_decision(root, text="post-forget append")
    assert added["ok"]
    assert "post-forget append" in _all_surfaces_text(root)


def test_forgotten_id_is_never_reborn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _seed(tmp_path)
    victim = memory.append_decision(root, text="soon gone")["record"]
    assert memory.forget_decision(root, target_id=victim["id"])["ok"]

    ids = iter([victim["id"], "dec-fresh002"])
    monkeypatch.setattr(memory, "_short_id", lambda prefix: next(ids) if prefix == "dec" else f"{prefix}-x")
    record = memory.append_decision(root, text="new life")["record"]

    assert record["id"] == "dec-fresh002", "generator must skip tombstoned ids"
    assert "new life" in _all_surfaces_text(root)


def test_cli_forget_requires_yes_and_matching_confirm_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _seed(tmp_path)
    victim = memory.append_decision(root, text=f"cli {SENTINEL}")["record"]
    monkeypatch.setattr(cli, "find_repo_root", lambda: root)
    for env in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        monkeypatch.delenv(env, raising=False)

    assert cli.main(["memory", "forget", "--id", victim["id"], "--confirm-id", victim["id"], "--json"]) == USAGE_ERROR
    assert cli.main(["memory", "forget", "--id", victim["id"], "--confirm-id", "dec-other", "--yes", "--json"]) == USAGE_ERROR
    assert SENTINEL in _decisions_raw(root), "refused runs must not touch the file"

    monkeypatch.setenv("CI", "1")
    assert cli.main(["memory", "forget", "--id", victim["id"], "--confirm-id", victim["id"], "--yes", "--json"]) == PERMISSION_DENIED
    monkeypatch.delenv("CI")

    capsys.readouterr()
    assert cli.main(["memory", "forget", "--id", victim["id"], "--confirm-id", victim["id"], "--yes", "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True and receipt["union_merge_restorable"] is True
    assert SENTINEL not in _decisions_raw(root)


def test_forget_is_not_exposed_over_mcp() -> None:
    names = {tool["name"] for tool in mcp_server.TOOLS}
    assert not any("forget" in name for name in names), (
        "destructive memory ops are CLI-only; the MCP tool table would advertise "
        "them with destructiveHint=false"
    )


def test_forget_session_notes_purges_log_and_snapshots(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_session_note(root, text=f"secret milestone {SENTINEL}")
    memory.append_session_note(root, text="normal milestone")
    snap_dir = root / ".ai" / "memory" / "sessions" / "abc-123"
    snap_dir.mkdir(parents=True)
    (snap_dir / "resume.json").write_text(
        json.dumps({"session_note_tail": f"copied {SENTINEL}"}), encoding="utf-8"
    )
    keep_dir = root / ".ai" / "memory" / "sessions" / "def-456"
    keep_dir.mkdir(parents=True)
    (keep_dir / "resume.json").write_text(json.dumps({"session_note_tail": "clean"}), encoding="utf-8")

    receipt = memory.forget_session_notes(root, contains=SENTINEL)

    assert receipt["ok"] is True
    assert receipt["removed_lines"] == 1
    assert receipt["removed_snapshots"] == ["".join(".ai/memory/sessions/abc-123/resume.json")]
    assert receipt["git_history_restorable"] is True
    note_text = memory.session_current_path(root).read_text(encoding="utf-8")
    assert SENTINEL not in note_text
    assert "normal milestone" in note_text
    assert not (snap_dir / "resume.json").exists()
    assert (keep_dir / "resume.json").exists()
    assert doctor.check_audit_chain(root).ok is True


def test_forget_session_notes_rejects_short_needle(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_session_note(root, text="do not gut me")
    receipt = memory.forget_session_notes(root, contains="e")
    assert receipt["ok"] is False and "needle_too_short" in receipt["reason"]
    assert "do not gut me" in memory.session_current_path(root).read_text(encoding="utf-8")


def test_cli_forget_note_requires_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    root = _seed(tmp_path)
    memory.append_session_note(root, text=f"note {SENTINEL}")
    monkeypatch.setattr(cli, "find_repo_root", lambda: root)
    for env in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        monkeypatch.delenv(env, raising=False)

    assert cli.main(["memory", "forget-note", "--contains", SENTINEL, "--json"]) == USAGE_ERROR
    assert SENTINEL in memory.session_current_path(root).read_text(encoding="utf-8")

    capsys.readouterr()
    assert cli.main(["memory", "forget-note", "--contains", SENTINEL, "--yes", "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["removed_lines"] == 1
    assert SENTINEL not in memory.session_current_path(root).read_text(encoding="utf-8")
