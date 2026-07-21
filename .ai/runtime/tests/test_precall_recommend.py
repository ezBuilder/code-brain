from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import precall_recommend  # noqa: E402
from ai_core.precall_recommend import (  # noqa: E402
    accept,
    activate,
    canonicalize_pattern,
    cluster_bash_patterns,
    disable,
    is_safe_pattern,
    list_catalog,
    list_visible,
    load_active_rules,
    record_dry_run_observation,
    record_user_override,
    recommend,
    reject,
    _candidate_id,
    _extract_claude_transcript_bash,
    _extract_codex_transcript_bash,
)
from ai_core.portable import hyphen_encode_path  # noqa: E402
from ai_core.precall import evaluate  # noqa: E402
from ai_core.memory import append_audit, append_event  # noqa: E402


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True)
    (tmp_path / ".ai" / "memory" / "audit-index.jsonl").touch()
    (tmp_path / ".ai" / "memory" / "events").mkdir(parents=True)
    return tmp_path


def _seed_pretooluse_bash(root: Path, command: str, count: int) -> None:
    for _ in range(count):
        append_event(
            root,
            {
                "hook": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
        )


def test_canonicalize_dedup():
    a = canonicalize_pattern(r"^flutter\s+test\b")
    b = canonicalize_pattern(r"^flutter test")
    assert a == b


def test_candidate_id_deterministic_via_canonical():
    a = _candidate_id("long_output_custom", canonicalize_pattern(r"^flutter\s+test\b"))
    b = _candidate_id("long_output_custom", canonicalize_pattern(r"^flutter test"))
    assert a == b


def test_safe_pattern_rejects_catch_all():
    ok, why = is_safe_pattern(r"^.*")
    assert not ok and why == "catch_all_rejected"
    ok, why = is_safe_pattern(r"^.+")
    assert not ok


def test_safe_pattern_rejects_unanchored():
    ok, why = is_safe_pattern(r"flutter test")
    assert not ok and why == "pattern_must_be_anchored"


def test_safe_pattern_rejects_whitelist_match():
    # `^echo\b` matches the safe probe `echo ok` → must be rejected.
    ok, why = is_safe_pattern(r"^echo\b")
    assert not ok and why.startswith("matches_safe_probe")
    # `^ls\b` matches `ls` → reject
    ok, why = is_safe_pattern(r"^ls\b")
    assert not ok


def test_safe_pattern_accepts_realistic_rule():
    ok, _ = is_safe_pattern(r"^flutter\s+test\b")
    assert ok
    ok, _ = is_safe_pattern(r"^cargo\s+build\b")
    assert ok


def test_cluster_finds_bigram(tmp_root: Path):
    invocations = ["flutter test --tags integration"] * 5 + ["flutter run --debug"]
    cands = cluster_bash_patterns(invocations, min_signal=5)
    kinds = {c.kind for c in cands}
    assert "long_output_custom" in kinds
    assert any("flutter" in c.pattern and "test" in c.pattern for c in cands)


def test_recommend_persists_pending(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "flutter test --tags integration", 6)
    out = recommend(tmp_root, min_signal=5)
    assert out["ok"]
    assert any(c["kind"] == "long_output_custom" for c in out["candidates"])
    cat = list_catalog(tmp_root)
    assert any(e.status == "pending" for e in cat)


def test_recommend_dedup_after_reject(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "cargo build --release", 6)
    first = recommend(tmp_root, min_signal=5)
    cid = first["candidates"][0]["id"]
    reject(tmp_root, cid)
    _seed_pretooluse_bash(tmp_root, "cargo build --release", 3)
    second = recommend(tmp_root, min_signal=5)
    assert all(c["id"] != cid for c in second["candidates"])


def test_lifecycle_pending_to_active(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "pytest -v tests/", 6)
    rec = recommend(tmp_root, min_signal=5)
    cid = rec["candidates"][0]["id"]

    res = accept(tmp_root, cid)
    assert res["ok"] and res["status"] == "dry_run"

    res = activate(tmp_root, cid)
    assert not res["ok"] and res["reason"] == "insufficient_observations"

    for _ in range(5):
        record_dry_run_observation(tmp_root, cid)

    res = activate(tmp_root, cid)
    assert res["ok"] and res["status"] == "active"


def test_active_rule_blocks_via_evaluate(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "pytest -v tests/", 6)
    rec = recommend(tmp_root, min_signal=5)
    cid = rec["candidates"][0]["id"]
    accept(tmp_root, cid)
    for _ in range(5):
        record_dry_run_observation(tmp_root, cid)
    activate(tmp_root, cid)

    rules = load_active_rules(tmp_root)
    decision = evaluate(
        "Bash",
        {"command": "pytest -v tests/integration"},
        extra_rules=rules,
    )
    assert decision["action"] == "block"
    assert decision.get("rule_id") == cid


def test_dry_run_observes_but_does_not_block(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "cargo build --release", 6)
    rec = recommend(tmp_root, min_signal=5)
    cid = rec["candidates"][0]["id"]
    accept(tmp_root, cid)

    rules = load_active_rules(tmp_root)
    decision = evaluate("Bash", {"command": "cargo build --release"}, extra_rules=rules)
    assert decision["action"] == "observe"
    assert decision.get("rule_id") == cid


def test_hatch_overrides_user_rule(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "cargo build", 6)
    rec = recommend(tmp_root, min_signal=5)
    cid = rec["candidates"][0]["id"]
    accept(tmp_root, cid)
    for _ in range(5):
        record_dry_run_observation(tmp_root, cid)
    activate(tmp_root, cid)

    rules = load_active_rules(tmp_root)
    decision = evaluate(
        "Bash",
        {"command": "cargo build | head -20"},
        extra_rules=rules,
    )
    assert decision["action"] == "allow"
    assert decision["reason"] == "hatch_detected"


def test_hardcoded_wins_over_user(tmp_root: Path):
    decision = evaluate(
        "Bash",
        {"command": "rg pattern src/"},
        extra_rules=[],
    )
    assert decision["action"] == "block"
    assert decision["reason"].startswith("long_output_binary")


def test_user_override_auto_disable(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "pytest -v tests/", 6)
    rec = recommend(tmp_root, min_signal=5)
    cid = rec["candidates"][0]["id"]
    accept(tmp_root, cid)
    for _ in range(5):
        record_dry_run_observation(tmp_root, cid)
    activate(tmp_root, cid)

    cmd = "pytest -v tests/"
    record_user_override(tmp_root, cid, cmd)
    record_user_override(tmp_root, cid, cmd)
    res = record_user_override(tmp_root, cid, cmd)
    assert res.get("auto_disabled") is True

    cat = next(e for e in list_catalog(tmp_root) if e.id == cid)
    assert cat.status == "disabled"


def test_accept_rejects_unsafe_pattern(tmp_root: Path):
    # Manually craft a pending entry with an unsafe pattern.
    _seed_pretooluse_bash(tmp_root, "flutter test", 6)
    rec = recommend(tmp_root, min_signal=5)
    cid = rec["candidates"][0]["id"]

    # Mutate the catalog row's pattern to a catch-all.
    from ai_core.precall_recommend import catalog_path
    rows = []
    for line in catalog_path(tmp_root).read_text(encoding="utf-8").splitlines():
        rec_obj = json.loads(line)
        if rec_obj.get("id") == cid:
            rec_obj["pattern"] = r"^.*"
        rows.append(rec_obj)
    catalog_path(tmp_root).write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    res = accept(tmp_root, cid)
    assert res["ok"] is False
    assert res["reason"] == "catch_all_rejected"


def test_list_visible_includes_all_states(tmp_root: Path):
    _seed_pretooluse_bash(tmp_root, "pytest tests/", 6)
    rec = recommend(tmp_root, min_signal=5)
    accept(tmp_root, rec["candidates"][0]["id"])
    visible = list_visible(tmp_root)
    statuses = {v["status"] for v in visible}
    assert "dry_run" in statuses


def _write_claude_bash_transcript(
    home: Path,
    root: Path,
    commands: list[str],
) -> Path:
    path = home / "projects" / hyphen_encode_path(str(root)) / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                        for command in commands
                    ]
                }
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_claude_transcript_extraction_streams_without_path_read_text(
    tmp_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "claude"
    transcript = _write_claude_bash_transcript(home, tmp_root, ["pytest -q"])
    original_read_text = Path.read_text

    def guarded_read_text(self: Path, *args, **kwargs):
        if self == transcript:
            raise AssertionError("precall transcript extraction must stream")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert _extract_claude_transcript_bash(tmp_root, home=home) == ["pytest -q"]


def test_claude_transcript_extraction_enforces_file_and_command_limits(
    tmp_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "claude"
    transcript = _write_claude_bash_transcript(
        home,
        tmp_root,
        ["pytest first", "pytest second"],
    )
    monkeypatch.setattr(precall_recommend, "PRECALL_TRANSCRIPT_MAX_COMMANDS", 1)
    assert _extract_claude_transcript_bash(tmp_root, home=home) == ["pytest first"]

    monkeypatch.setattr(
        precall_recommend,
        "PRECALL_TRANSCRIPT_MAX_FILE_BYTES",
        max(1, transcript.stat().st_size - 1),
    )
    assert _extract_claude_transcript_bash(tmp_root, home=home) == []


def _write_codex_rollout(home: Path, name: str, cwd: Path, command: str, *, mtime: int) -> Path:
    path = home / "sessions" / "2026" / "07" / "21" / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cwd": str(cwd)}, separators=(",", ":"))
        + "\n"
        + json.dumps(
            {"payload": {"command": ["bash", "-lc", command]}},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


def test_codex_transcript_cwd_filter_rejects_prefix_sibling(
    tmp_root: Path,
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex"
    _write_codex_rollout(home, "wrong", Path(str(tmp_root) + "-other"), "echo wrong", mtime=2)
    _write_codex_rollout(home, "right", tmp_root, "echo right", mtime=1)

    commands = _extract_codex_transcript_bash(tmp_root, home=home)

    assert "bash -lc echo right" in commands
    assert "bash -lc echo wrong" not in commands
