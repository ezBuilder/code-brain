"""Tests for audit_repair.repair_audit_chain — recompute prev_sha after splice damage."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _make_audit(tmp: Path, lines: list[str]) -> Path:
    audit_dir = tmp / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    path = audit_dir / "2026.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _row(action: str, prev_sha: str | None) -> str:
    rec = {"action": action, "category": "test", "monotonic_ns": 1, "payload": {}, "ts": "2026-05-25T00:00:00Z"}
    if prev_sha is not None:
        rec["prev_sha"] = prev_sha
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_intact_chain_repaired_zero(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    head = _row("a", None)
    rows = [head]
    for action in ("b", "c", "d"):
        prev = _sha(rows[-1])
        rows.append(_row(action, prev))
    _make_audit(tmp_path, rows)
    result = repair_audit_chain(tmp_path)
    assert result["ok"]
    assert result["total_repaired"] == 0
    assert result["files"][0]["first_mismatch"] is None


def test_damaged_chain_recovers(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    head = _row("a", None)
    rows = [head]
    for action in ("b", "c", "d"):
        prev = _sha(rows[-1])
        rows.append(_row(action, prev))
    # Corrupt row 2's prev_sha (simulate stash union merge artifact)
    rec = json.loads(rows[2])
    rec["prev_sha"] = "0" * 64
    rows[2] = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path = _make_audit(tmp_path, rows)

    result = repair_audit_chain(tmp_path)
    assert result["ok"]
    assert result["total_repaired"] >= 1
    assert result["files"][0]["first_mismatch"] == 3  # 1-based

    # Re-verify chain
    out_lines = path.read_text(encoding="utf-8").splitlines()
    prev_text = out_lines[0]
    for ln in out_lines[1:]:
        rec = json.loads(ln)
        assert rec["prev_sha"] == _sha(prev_text)
        prev_text = ln


def test_first_chained_row_prev_sha_recovers_to_null(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    rows = [_row("a", "0" * 64), _row("b", "wrong")]
    path = _make_audit(tmp_path, rows)

    result = repair_audit_chain(tmp_path)

    assert result["ok"]
    assert result["total_repaired"] == 2
    assert result["files"][0]["first_mismatch"] == 1
    out_lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(out_lines[0])
    second = json.loads(out_lines[1])
    assert first["prev_sha"] is None
    assert second["prev_sha"] == _sha(out_lines[0])


def test_legacy_boundary_mismatch_is_reported_by_repair_and_doctor(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain
    from ai_core.doctor import check_audit_chain

    legacy = _row("legacy", None)
    chained = _row("new", "0" * 64)
    _make_audit(tmp_path, [legacy, chained])

    doctor = check_audit_chain(tmp_path)
    repair = repair_audit_chain(tmp_path)

    assert doctor.ok is False
    assert "prev_sha_mismatch" in doctor.detail
    assert repair["files"][0]["first_mismatch"] == 2


def test_invalid_json_boundary_is_not_an_amnesty_for_chain_mismatch(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain
    from ai_core.doctor import check_audit_chain

    legacy = _row("legacy", None)
    malformed = "{not-json"
    chained = _row("new", "0" * 64)
    _make_audit(tmp_path, [legacy, malformed, chained])

    doctor = check_audit_chain(tmp_path)
    repair = repair_audit_chain(tmp_path)

    assert doctor.ok is False
    assert "invalid_json" in doctor.detail
    assert "prev_sha_mismatch" in doctor.detail
    assert repair["files"][0]["first_mismatch"] == 3


def test_no_content_dropped(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    head = _row("a", None)
    rows = [head, _row("b", "wrong"), _row("c", "wrong"), _row("d", "wrong")]
    _make_audit(tmp_path, rows)
    actions_before = ["a", "b", "c", "d"]

    repair_audit_chain(tmp_path)
    out_lines = (tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl").read_text(encoding="utf-8").splitlines()
    actions_after = [json.loads(ln)["action"] for ln in out_lines]
    assert actions_before == actions_after


def test_missing_audit_dir(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    result = repair_audit_chain(tmp_path)
    assert result["ok"] is False
    assert "audit dir missing" in result.get("error", "")


def test_year_specific_repair(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    (audit_dir / "2025.jsonl").write_text(_row("y25", None) + "\n", encoding="utf-8")
    (audit_dir / "2026.jsonl").write_text(_row("y26", None) + "\n", encoding="utf-8")

    result = repair_audit_chain(tmp_path, year=2026)
    paths = [f["path"] for f in result["files"]]
    assert paths == [".ai/memory/audit/2026.jsonl"]


def test_empty_year_file(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    (audit_dir / "2026.jsonl").write_text("", encoding="utf-8")
    result = repair_audit_chain(tmp_path)
    assert result["ok"]
    assert result["total_repaired"] == 0


def test_segment_repair_refreshes_following_link_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_core import memory
    from ai_core.audit_repair import repair_audit_chain
    from ai_core.doctor import check_audit_chain

    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)
    for index in range(40):
        memory.append_audit(
            tmp_path,
            action="test.segment.repair",
            category="test",
            payload={"index": index, "padding": "x" * 80},
        )
    files = memory.all_audit_files(tmp_path)
    assert len(files) > 2

    first_segment = files[0]
    rows = [json.loads(line) for line in first_segment.read_text(encoding="utf-8").splitlines()]
    rows[1]["prev_sha"] = "0" * 64
    rows[1]["payload"]["padding"] = "y" * 80
    first_segment.write_text(
        "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    assert check_audit_chain(tmp_path).ok is False

    repaired = repair_audit_chain(tmp_path)

    assert repaired["ok"] is True
    assert repaired["total_repaired"] > 0
    assert check_audit_chain(tmp_path).ok is True
    assert not first_segment.exists()
    for path in memory.all_audit_files(tmp_path):
        key = memory._audit_file_sort_key(path.name)
        if key is not None and key[1] == 0:
            assert path.name.split(".")[2] == hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def test_repair_refuses_duplicate_segment_sequence_without_mutation(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    before: dict[Path, bytes] = {}
    for action in ("branch-a", "branch-b"):
        content = (json.dumps({"action": action}, separators=(",", ":")) + "\n").encode()
        digest = hashlib.sha256(content).hexdigest()[:12]
        path = audit_dir / f"2026.000001.{digest}.jsonl"
        path.write_bytes(content)
        before[path] = content

    result = repair_audit_chain(tmp_path)

    assert result["ok"] is False
    assert "duplicate audit segment sequence" in result["error"]
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("missing_position", [0, 1], ids=["first", "middle"])
def test_repair_refuses_missing_segment_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_position: int
) -> None:
    from ai_core import memory
    from ai_core.audit_repair import repair_audit_chain

    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)
    for index in range(60):
        memory.append_audit(
            tmp_path,
            action="test.segment.loss",
            category="test",
            payload={"index": index, "padding": "x" * 80},
        )
    segments = [
        path
        for path in memory.all_audit_files(tmp_path)
        if (memory._audit_file_sort_key(path.name) or (0, 1, 0))[1] == 0
    ]
    assert len(segments) >= 3
    segments[missing_position].unlink()
    before = {path: path.read_bytes() for path in memory.all_audit_files(tmp_path)}

    result = repair_audit_chain(tmp_path)

    assert result["ok"] is False
    assert "restoring the missing raw segment" in result["error"]
    assert {path: path.read_bytes() for path in before} == before


def test_repair_refuses_orphan_current_marker_without_mutation(tmp_path: Path) -> None:
    from ai_core.audit_repair import repair_audit_chain

    current = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    current.parent.mkdir(parents=True)
    line = json.dumps(
        {
            "action": "audit.segment_started",
            "category": "storage",
            "payload": {"previous_segment": ".ai/memory/audit/2026.000001.missing.jsonl"},
            "prev_sha": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    current.write_text(line + "\n", encoding="utf-8")
    before = current.read_bytes()

    result = repair_audit_chain(tmp_path)

    assert result["ok"] is False
    assert "orphan audit segment marker" in result["error"]
    assert current.read_bytes() == before
