from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ai_core import doctor
from ai_core import memory
from ai_core import search
from ai_core import storage_lifecycle
from ai_core.obs import diagnostics, write_log
from ai_core.upgrade import upgrade_apply


def _private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)


def test_append_jsonl_enforces_automatic_byte_and_line_caps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "_JSONL_AUTO_MAX_BYTES", 500)
    monkeypatch.setattr(memory, "_JSONL_AUTO_KEEP_BYTES", 260)
    monkeypatch.setattr(memory, "_JSONL_AUTO_KEEP_LINES", 3)
    path = tmp_path / ".ai" / "memory" / "records.jsonl"

    for idx in range(20):
        memory.append_jsonl(path, {"idx": idx, "payload": "x" * 80})

    assert path.stat().st_size <= 500
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) <= 3
    assert rows[-1]["idx"] == 19


def test_audit_rotation_preserves_chain_and_rebuilds_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_KEEP_BYTES", 1_000)
    monkeypatch.setattr(memory, "_AUDIT_KEEP_LINES", 8)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)

    for idx in range(40):
        memory.append_audit(tmp_path, action="test.event", category="test", payload={"idx": idx, "v": "x" * 80})

    path = memory.audit_path(tmp_path)
    assert path.stat().st_size <= 2_000
    assert doctor.check_audit_chain(tmp_path).ok is True
    assert doctor.check_audit_index(tmp_path).ok is True
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(row.get("action") == "audit.storage_rotated" for row in rows)
    assert rows[-1]["payload"]["idx"] == 39


def test_audit_retention_removes_expired_year_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "_AUDIT_RETENTION_YEARS", 2)
    old = tmp_path / ".ai" / "memory" / "audit" / "2020.jsonl"
    _private_file(old, b'{"ts":"2020-01-01T00:00:00Z"}\n')

    memory.append_audit(tmp_path, action="current", category="test", payload={})

    assert not old.exists()
    assert memory.audit_path(tmp_path).exists()


def test_log_diagnostics_and_upgrade_backups_prune_automatically(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(storage_lifecycle, "LOG_MAX_FILES", 2)
    monkeypatch.setattr(storage_lifecycle, "LOG_MAX_TOTAL_BYTES", 1_000)
    monkeypatch.setattr(storage_lifecycle, "DIAGNOSTIC_MAX_FILES", 2)
    monkeypatch.setattr(storage_lifecycle, "UPGRADE_BACKUP_MAX_FILES", 2)

    logs = tmp_path / ".ai" / "cache" / "logs"
    for idx in range(5):
        path = logs / f"2020-01-0{idx + 1}.jsonl"
        _private_file(path, b"{}\n")
        os.utime(path, (1, 1))
    write_log(tmp_path, "info", "fresh", {})
    assert len(list(logs.glob("*.jsonl"))) <= 2

    monkeypatch.setattr("ai_core.obs.metrics", lambda *_args, **_kwargs: {"ok": True})
    diagnostics(tmp_path, include_doctor=False, include_usage=False)
    diag_root = tmp_path / ".ai" / "cache" / "diagnostics"
    assert len(list(diag_root.iterdir())) <= 2

    generated = tmp_path / ".ai" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "runtime_version": "0.6.4"}
    monkeypatch.setattr("ai_core.upgrade.now_stamp", lambda: "20260720T000000Z")
    monkeypatch.setattr("ai_core.upgrade.build_manifest", lambda _root: manifest)
    monkeypatch.setattr("ai_core.upgrade.migrate", lambda _root: {"ok": True})
    monkeypatch.setattr("ai_core.upgrade.render", lambda _root: {"ok": True})
    monkeypatch.setattr("ai_core.upgrade.append_audit", lambda *_args, **_kwargs: {})
    result = upgrade_apply(tmp_path, target_version="0.6.4")
    assert result["ok"] is True
    assert result["retention"]["kept"] <= 2


def test_sqlite_index_file_cap_resets_oversized_storage_and_sets_page_limit(tmp_path: Path, monkeypatch) -> None:
    db = search.db_path(tmp_path)
    _private_file(db, b"x" * 9_000)
    monkeypatch.setattr(search, "INDEX_DB_MAX_BYTES", 8_192)
    conn = search.connect(tmp_path)
    page_size = int(conn.execute("pragma page_size").fetchone()[0])
    max_pages = int(conn.execute("pragma max_page_count").fetchone()[0])
    conn.close()

    assert db.stat().st_size <= 8_192
    assert max_pages * page_size <= 8_192


def test_doctor_reports_storage_policy_violations(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / ".ai" / "cache" / "logs"
    _private_file(logs / "2020-01-01.jsonl", b"{}\n")
    os.utime(logs / "2020-01-01.jsonl", (1, 1))
    monkeypatch.setattr(storage_lifecycle, "LOG_RETENTION_DAYS", 1)

    result = doctor.check_storage_limits(tmp_path)

    assert result.ok is False
    assert "expired" in result.detail


def _workspace_storage_fixture(monkeypatch) -> None:
    monkeypatch.setattr(storage_lifecycle, "_tracked_top_entries", lambda _root, _directory: (set(), True))


def test_workspace_storage_prunes_tmp_by_age_and_size(tmp_path: Path, monkeypatch) -> None:
    _workspace_storage_fixture(monkeypatch)
    monkeypatch.setattr(storage_lifecycle, "TMP_RETENTION_DAYS", 1)
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_ENTRIES", 3)
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_TOTAL_BYTES", 250)
    monkeypatch.setattr(storage_lifecycle, "OUTPUT_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage_lifecycle, "AI_MAX_TOTAL_BYTES", 20_000)

    tmp = tmp_path / ".ai" / "tmp"
    tmp.mkdir(parents=True)
    old = tmp / "old.bin"
    recent_a = tmp / "recent-a.bin"
    recent_b = tmp / "recent-b.bin"
    old.write_bytes(b"o" * 100)
    recent_a.write_bytes(b"a" * 100)
    recent_b.write_bytes(b"b" * 100)
    os.utime(old, (1, 1))

    result = storage_lifecycle.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert not old.exists()
    assert recent_a.exists()
    assert recent_b.exists()
    assert result["status"]["tmp_bytes"] <= 250


def test_workspace_storage_prunes_oldest_unpinned_output_and_preserves_tracked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        storage_lifecycle,
        "_tracked_top_entries",
        lambda _root, directory: ({"tracked.bin"}, True) if directory.name == "outputs" else (set(), True),
    )
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage_lifecycle, "OUTPUT_MAX_ENTRIES", 3)
    monkeypatch.setattr(storage_lifecycle, "OUTPUT_MAX_TOTAL_BYTES", 250)
    monkeypatch.setattr(storage_lifecycle, "AI_MAX_TOTAL_BYTES", 20_000)

    outputs = tmp_path / ".ai" / "outputs"
    outputs.mkdir(parents=True)
    old = outputs / "old.bin"
    new = outputs / "new.bin"
    tracked = outputs / "tracked.bin"
    old.write_bytes(b"o" * 100)
    new.write_bytes(b"n" * 100)
    tracked.write_bytes(b"t" * 100)
    os.utime(old, (1, 1))

    result = storage_lifecycle.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert not old.exists()
    assert new.exists()
    assert tracked.exists()
    assert result["status"]["output_bytes"] <= 250


def test_workspace_storage_preserves_companion_keep_marker_across_runs(tmp_path: Path, monkeypatch) -> None:
    _workspace_storage_fixture(monkeypatch)
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage_lifecycle, "OUTPUT_MAX_ENTRIES", 2)
    monkeypatch.setattr(storage_lifecycle, "OUTPUT_MAX_TOTAL_BYTES", 150)
    monkeypatch.setattr(storage_lifecycle, "AI_MAX_TOTAL_BYTES", 20_000)

    outputs = tmp_path / ".ai" / "outputs"
    outputs.mkdir(parents=True)
    artifact = outputs / "artifact.bin"
    marker = outputs / "artifact.bin.keep"
    expendable = outputs / "expendable.bin"
    artifact.write_bytes(b"a" * 100)
    marker.write_text("")
    expendable.write_bytes(b"x" * 100)
    os.utime(marker, (1, 1))

    first = storage_lifecycle.enforce_workspace_storage(tmp_path)
    second = storage_lifecycle.enforce_workspace_storage(tmp_path)

    assert first["ok"] is True
    assert second["ok"] is True
    assert artifact.exists()
    assert marker.exists()
    assert not expendable.exists()


def test_workspace_storage_total_quota_reclaims_tmp_before_outputs(tmp_path: Path, monkeypatch) -> None:
    _workspace_storage_fixture(monkeypatch)
    monkeypatch.setattr(storage_lifecycle, "TMP_RETENTION_DAYS", 3650)
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage_lifecycle, "OUTPUT_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage_lifecycle, "AI_MAX_TOTAL_BYTES", 300)

    tmp = tmp_path / ".ai" / "tmp"
    outputs = tmp_path / ".ai" / "outputs"
    tmp.mkdir(parents=True)
    outputs.mkdir(parents=True)
    scratch = tmp / "scratch.bin"
    artifact = outputs / "artifact.bin"
    scratch.write_bytes(b"s" * 200)
    artifact.write_bytes(b"a" * 200)
    os.utime(scratch, (1, 1))

    result = storage_lifecycle.enforce_workspace_storage(tmp_path)

    assert result["ok"] is True
    assert not scratch.exists()
    assert artifact.exists()
    assert result["status"]["ai_bytes"] <= 300


def test_workspace_storage_unlinks_symlink_without_touching_target(tmp_path: Path, monkeypatch) -> None:
    _workspace_storage_fixture(monkeypatch)
    monkeypatch.setattr(storage_lifecycle, "TMP_RETENTION_DAYS", 0)
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_ENTRIES", 0)
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_TOTAL_BYTES", 0)
    monkeypatch.setattr(storage_lifecycle, "OUTPUT_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(storage_lifecycle, "AI_MAX_TOTAL_BYTES", 20_000)

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    tmp = tmp_path / ".ai" / "tmp"
    tmp.mkdir(parents=True)
    link = tmp / "outside-link"
    link.symlink_to(outside)

    storage_lifecycle.enforce_workspace_storage(tmp_path)

    assert not link.exists()
    assert outside.read_bytes() == b"keep"


def test_doctor_reports_workspace_total_quota_without_deleting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_TOTAL_BYTES", 100)
    monkeypatch.setattr(storage_lifecycle, "AI_MAX_TOTAL_BYTES", 100)
    tmp = tmp_path / ".ai" / "tmp"
    tmp.mkdir(parents=True)
    oversized = tmp / "oversized.bin"
    oversized.write_bytes(b"x" * 200)

    result = doctor.check_storage_limits(tmp_path)

    assert result.ok is False
    assert ".ai/tmp:total-bytes" in result.detail
    assert oversized.exists()


def test_doctor_reports_workspace_top_level_entry_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(storage_lifecycle, "TMP_MAX_ENTRIES", 1)
    tmp = tmp_path / ".ai" / "tmp"
    tmp.mkdir(parents=True)
    (tmp / "one").write_text("1")
    (tmp / "two").write_text("2")

    result = doctor.check_storage_limits(tmp_path)

    assert result.ok is False
    assert ".ai/tmp:file-count" in result.detail


def test_github_upgrade_uses_single_low_memory_activation() -> None:
    root = Path(__file__).resolve().parents[3]
    bootstrap = (root / "bootstrap-code-brain.sh").read_text()
    installer = (root / "scripts" / "install-into.sh").read_text()
    upgrader = (root / "scripts" / "upgrade-from-github.sh").read_text()

    assert "UV_CONCURRENT_DOWNLOADS" in bootstrap
    assert "--low-memory" in bootstrap
    assert 'EXISTING_PYTHON=".ai/runtime/.venv/bin/python"' in bootstrap
    assert "retaining the verified existing runtime" in bootstrap
    preflight = (root / "scripts" / "preflight.sh").read_text()
    env_check = (root / "scripts" / "env-check.sh").read_text()
    assert preflight.index('.venv/bin/python') < preflight.index('command -v uv')
    assert 'installed_python = next(' in env_check
    for launcher_name in ("ai", "ai-hook", "ai-mcp"):
        launcher = (root / ".ai" / "bin" / launcher_name).read_text()
        assert "import ai_core.cli' >/dev/null" not in launcher
    assert "preflight-proof.json >/dev/null" not in bootstrap
    assert "bootstrap-code-brain.sh --skip-doctor --skip-render --low-memory >/dev/null" not in installer
    assert ".ai/bin/ai doctor --strict --json >/dev/null" not in upgrader
    smoke = (root / "scripts" / "smoke.sh").read_text()
    assert 'QUIET_LOG="$TMP/quiet.log"' in smoke
    assert "AI_BOOTSTRAP_LOW_MEMORY=1" in installer
    assert 'AI_INSTALL_DEFER_RUNTIME=1 bash "$CHECKOUT/scripts/install-into.sh" upgrade' in upgrader
    assert upgrader.count("bash ./bootstrap-code-brain.sh") == 1
    assert "session start --agent operator --rebuild auto" in upgrader
