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
