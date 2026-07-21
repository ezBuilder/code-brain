#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

# Set deliberately tight, production-supported policies before importing modules
# whose bounded constants are initialized from the environment.
os.environ.setdefault("AI_SANDBOX_META_MAX_BYTES", "16000")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_FILES", "32")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_CANDIDATES", "64")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_BYTES", "262144")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_SECONDS", "2")
os.environ.setdefault("AI_TRANSCRIPT_MAX_FILE_BYTES", "1000000")
os.environ.setdefault("AI_TRANSCRIPT_MAX_LINE_BYTES", "64000")
os.environ.setdefault("AI_TRANSCRIPT_MAX_SCAN_BYTES", "4000000")
os.environ.setdefault("AI_TRANSCRIPT_MAX_SESSIONS", "32")
os.environ.setdefault("AI_TRANSCRIPT_MAX_CANDIDATES", "64")
os.environ.setdefault("AI_TRANSCRIPT_MAX_SCAN_SECONDS", "3")
os.environ.setdefault("AI_TRANSCRIPT_MAX_DEDUPE_KEYS", "100")
os.environ.setdefault("AI_LOG_RETENTION_DAYS", "100000")
os.environ.setdefault("AI_LOG_MAX_FILES", "4")
os.environ.setdefault("AI_LOG_MAX_BYTES", "4096")
os.environ.setdefault("AI_DIAGNOSTICS_RETENTION_DAYS", "100000")
os.environ.setdefault("AI_DIAGNOSTICS_MAX_FILES", "3")
os.environ.setdefault("AI_DIAGNOSTICS_MAX_BYTES", "4096")
os.environ.setdefault("AI_UPGRADE_BACKUP_RETENTION_DAYS", "100000")
os.environ.setdefault("AI_UPGRADE_BACKUP_MAX_FILES", "2")
os.environ.setdefault("AI_UPGRADE_BACKUP_MAX_BYTES", "4096")

from ai_core import memory, obs, sandbox, search, upgrade  # noqa: E402
from ai_core.audit_repair import repair_audit_chain  # noqa: E402
from ai_core.doctor import check_audit_chain, check_audit_index, check_index_storage  # noqa: E402
from ai_core.transcripts import claude_usage_summary, codex_usage_summary  # noqa: E402


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _sandbox_fixture(root: Path, files: int) -> None:
    target = root / ".ai" / "cache" / "sandbox"
    target.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        exec_id = f"{index:016x}"
        killed = index == files - 1
        _write_json(
            target / f"{exec_id}.meta.json",
            {
                "exec_id": exec_id,
                "created_at": f"2026-07-21T00:{index % 60:02d}:00Z",
                "command_ok": not killed,
                "exit_code": -9 if killed else 0,
                "peak_rss_kib": 2048 + index,
                "source_total_bytes": 128,
                "termination": {
                    "classification": "external_sigkill_or_execution_limit" if killed else "exit_code",
                    "signal": "SIGKILL" if killed else None,
                },
            },
        )


def _transcript_fixtures(root: Path, claude_home: Path, codex_home: Path, files: int) -> None:
    claude_project = claude_home / "projects" / "stress-project"
    codex_sessions = codex_home / "sessions" / "2026" / "07" / "21"
    claude_project.mkdir(parents=True, exist_ok=True)
    codex_sessions.mkdir(parents=True, exist_ok=True)
    root_text = str(root)
    for index in range(files):
        stamp = f"2026-07-21T00:{index % 60:02d}:00Z"
        claude = {
            "sessionId": f"claude-{index}",
            "requestId": f"request-{index}",
            "cwd": root_text,
            "timestamp": stamp,
            "message": {
                "id": f"message-{index}",
                "model": "stress",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
        (claude_project / f"session-{index:08d}.jsonl").write_text(
            json.dumps(claude, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        codex_records = [
            {
                "timestamp": stamp,
                "type": "session_meta",
                "payload": {"id": f"codex-{index}", "cwd": root_text, "model_provider": "stress"},
            },
            {
                "timestamp": stamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "cached_input_tokens": 0,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 2,
                        }
                    },
                },
            },
        ]
        (codex_sessions / f"rollout-{index:08d}.jsonl").write_text(
            "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in codex_records),
            encoding="utf-8",
        )


def _write_bounded_file(path: Path, *, serial: int, size: int = 900) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{serial:08d}:".encode("ascii")
    path.write_bytes((token * ((size // len(token)) + 1))[:size])
    timestamp = time.time() + serial
    os.utime(path, (timestamp, timestamp))


def _assert_retention_status(name: str, status: dict[str, Any]) -> None:
    if status.get("ok") is not True:
        raise AssertionError(f"{name}: unhealthy retention status: {status.get('violations')}")
    if int(status.get("count") or 0) > int(status.get("max_files") or 0):
        raise AssertionError(f"{name}: file count exceeds policy")
    if int(status.get("bytes") or 0) > int(status.get("max_bytes") or 0):
        raise AssertionError(f"{name}: byte count exceeds policy")


def _retention_plateau(root: Path, files: int, iterations: int) -> dict[str, Any]:
    fixture_files = max(32, min(files, 256))
    cycles = max(2, min(iterations, 10))
    batch_size = max(1, (fixture_files + cycles - 1) // cycles)
    logs = root / ".ai" / "cache" / "logs"
    diagnostics = root / ".ai" / "cache" / "diagnostics"
    backups = root / ".ai" / "cache" / "upgrade"
    stores = {
        "logs": {
            "directory": logs,
            "path": lambda serial: logs / f"stress-log-{serial:08d}.jsonl",
            "prune": lambda: obs.prune_logs(root),
            "status": lambda: obs.logs_retention_status(root),
        },
        "diagnostics": {
            "directory": diagnostics,
            "path": lambda serial: diagnostics / f"diagnostics-stress-{serial:08d}.zip",
            "prune": lambda: obs.prune_diagnostics(root),
            "status": lambda: obs.diagnostics_retention_status(root),
        },
        "upgrade_backups": {
            "directory": backups,
            "path": lambda serial: backups / f"rollback-stress-{serial:08d}.json",
            "prune": lambda: upgrade.prune_upgrade_backups(root),
            "status": lambda: upgrade.upgrade_backup_retention_status(root),
        },
    }
    history: dict[str, list[dict[str, int]]] = {name: [] for name in stores}
    removed: dict[str, int] = {name: 0 for name in stores}
    serial = 0
    for _cycle in range(cycles):
        remaining = fixture_files - serial
        if remaining > 0:
            create_count = min(batch_size, remaining)
            for _ in range(create_count):
                for store in stores.values():
                    _write_bounded_file(store["path"](serial), serial=serial)
                serial += 1
        for name, store in stores.items():
            result = store["prune"]()
            if result.get("ok") is not True:
                raise AssertionError(f"{name}: prune failed: {result.get('errors')}")
            removed[name] += int(result.get("removed_count") or 0)
            status = store["status"]()
            _assert_retention_status(name, status)
            history[name].append(
                {
                    "count": int(status.get("count") or 0),
                    "bytes": int(status.get("bytes") or 0),
                }
            )

    result: dict[str, Any] = {}
    for name, store in stores.items():
        final = store["status"]()
        _assert_retention_status(name, final)
        repeat = store["prune"]()
        repeated = store["status"]()
        _assert_retention_status(name, repeated)
        if repeat.get("ok") is not True or int(repeat.get("removed_count") or 0) != 0:
            raise AssertionError(f"{name}: second prune was not idempotent")
        if (final.get("count"), final.get("bytes")) != (
            repeated.get("count"),
            repeated.get("bytes"),
        ):
            raise AssertionError(f"{name}: retained disk footprint changed without new writes")
        samples = history[name]
        result[name] = {
            "created": fixture_files,
            "removed": removed[name],
            "cycles": cycles,
            "final_count": int(final.get("count") or 0),
            "final_bytes": int(final.get("bytes") or 0),
            "max_observed_count": max((sample["count"] for sample in samples), default=0),
            "max_observed_bytes": max((sample["bytes"] for sample in samples), default=0),
            "max_files": int(final.get("max_files") or 0),
            "max_bytes": int(final.get("max_bytes") or 0),
            "idempotent": True,
        }
    return result


def _audit_recovery(root: Path, files: int) -> dict[str, Any]:
    records = max(64, min(files, 256))
    original_max = memory.AUDIT_MAX_BYTES
    original_keep = memory.AUDIT_KEEP_BYTES
    memory.AUDIT_MAX_BYTES = 12_000
    memory.AUDIT_KEEP_BYTES = 4_000
    try:
        for index in range(records):
            memory.append_audit(
                root,
                action="stress.audit",
                category="stress",
                payload={"index": index, "value": "x" * 180},
            )
        path = memory.audit_path(root)
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if path.stat().st_size > memory.AUDIT_MAX_BYTES:
            raise AssertionError("audit: active file exceeds compacted byte cap")
        if not any(json.loads(line).get("action") == "audit.retention_compact" for line in lines):
            raise AssertionError("audit: compaction checkpoint was not retained")
        if not check_audit_chain(root).ok or not check_audit_index(root).ok:
            raise AssertionError("audit: chain or index unhealthy after compaction")

        damaged_index = max(1, len(lines) // 2)
        damaged = json.loads(lines[damaged_index])
        damaged["prev_sha"] = "0" * 64
        lines[damaged_index] = json.dumps(
            damaged,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if check_audit_chain(root).ok:
            raise AssertionError("audit: injected chain damage was not detected")

        repaired = repair_audit_chain(root)
        memory.rebuild_audit_index(root)
        if repaired.get("ok") is not True or int(repaired.get("total_repaired") or 0) < 1:
            raise AssertionError("audit: repair did not rewrite the damaged suffix")
        if not check_audit_chain(root).ok or not check_audit_index(root).ok:
            raise AssertionError("audit: chain or index remained unhealthy after repair")
        repeat = repair_audit_chain(root)
        if repeat.get("ok") is not True or int(repeat.get("total_repaired") or 0) != 0:
            raise AssertionError("audit: repair was not idempotent")

        memory.append_audit(
            root,
            action="stress.audit.recovered",
            category="stress",
            payload={"records": records},
        )
        if not check_audit_chain(root).ok or not check_audit_index(root).ok:
            raise AssertionError("audit: post-repair append broke chain or index")
        return {
            "records_appended": records + 1,
            "final_bytes": path.stat().st_size,
            "max_bytes": memory.AUDIT_MAX_BYTES,
            "compacted": True,
            "damage_detected": True,
            "repaired_records": int(repaired.get("total_repaired") or 0),
            "repair_idempotent": True,
            "chain_ok": True,
            "index_ok": True,
        }
    finally:
        memory.AUDIT_MAX_BYTES = original_max
        memory.AUDIT_KEEP_BYTES = original_keep


def _sqlite_recovery(root: Path, files: int) -> dict[str, Any]:
    source_count = max(32, min(files, 128))
    source_root = root / "stress-index-source"
    source_root.mkdir(parents=True, exist_ok=True)
    for index in range(source_count):
        (source_root / f"item_{index:04d}.py").write_text(
            f"VALUE_{index} = {'z' * 2000!r}\n",
            encoding="utf-8",
        )
    initial = search.rebuild(root, force=True)
    if initial.get("ok") is not True:
        raise AssertionError(f"sqlite: initial rebuild failed: {initial.get('error')}")

    deleted_target = (source_count * 3) // 4
    for index in range(deleted_target):
        (source_root / f"item_{index:04d}.py").unlink()
    original_min_pages = search.INDEX_VACUUM_MIN_FREE_PAGES
    original_free_ratio = search.INDEX_VACUUM_FREE_RATIO
    try:
        search.INDEX_VACUUM_MIN_FREE_PAGES = 0
        search.INDEX_VACUUM_FREE_RATIO = 0.0
        recovered = search.rebuild(root, incremental=True, force=True)
    finally:
        search.INDEX_VACUUM_MIN_FREE_PAGES = original_min_pages
        search.INDEX_VACUUM_FREE_RATIO = original_free_ratio
    if recovered.get("ok") is not True:
        raise AssertionError(f"sqlite: recovery rebuild failed: {recovered.get('error')}")
    if int(recovered.get("deleted") or 0) != deleted_target:
        raise AssertionError("sqlite: incremental deletion count mismatch")
    recovered_storage = recovered.get("storage", {})
    if recovered_storage.get("vacuumed") is not True or recovered_storage.get("within_limit") is not True:
        raise AssertionError("sqlite: WAL checkpoint/vacuum recovery was not completed")

    stable = search.rebuild(root, incremental=True, force=True)
    if stable.get("ok") is not True or int(stable.get("deleted") or 0) != 0:
        raise AssertionError("sqlite: no-change rebuild was not stable")
    stable_storage = stable.get("storage", {})
    growth_allowance = max(4096, int(stable_storage.get("page_size") or 0) * 4)
    if int(stable_storage.get("total_bytes") or 0) > int(recovered_storage.get("total_bytes") or 0) + growth_allowance:
        raise AssertionError("sqlite: disk footprint grew after no-change rebuild")

    original_limit = search.INDEX_MAX_BYTES
    total_bytes = int(stable_storage.get("total_bytes") or 0)
    try:
        search.INDEX_MAX_BYTES = max(1, total_bytes - 1)
        oversized = search.index_storage(root)
        oversized_doctor = check_index_storage(root)
        if oversized.get("within_limit") is not False or oversized_doctor.ok:
            raise AssertionError("sqlite: injected absolute-size violation was not detected")
    finally:
        search.INDEX_MAX_BYTES = original_limit
    healthy = search.index_storage(root)
    healthy_doctor = check_index_storage(root)
    if healthy.get("within_limit") is not True or not healthy_doctor.ok:
        raise AssertionError("sqlite: storage status did not recover after restoring policy")
    return {
        "source_files": source_count,
        "deleted": deleted_target,
        "initial_bytes": int(initial.get("storage", {}).get("total_bytes") or 0),
        "recovered_bytes": int(recovered_storage.get("total_bytes") or 0),
        "stable_bytes": int(stable_storage.get("total_bytes") or 0),
        "reclaimed_bytes": int(recovered_storage.get("reclaimed_bytes") or 0),
        "vacuumed": True,
        "no_change_stable": True,
        "size_violation_detected": True,
        "policy_recovery_ok": True,
    }


def _assert_scan_bounds(name: str, payload: dict[str, Any], files: int) -> dict[str, Any]:
    scan = payload.get("scan")
    if not isinstance(scan, dict):
        raise AssertionError(f"{name}: missing scan payload")
    policy = scan.get("policy")
    if not isinstance(policy, dict):
        raise AssertionError(f"{name}: missing scan policy")
    max_sessions = int(policy["max_sessions"])
    max_candidates = int(policy["max_candidates"])
    max_bytes = int(policy["max_scan_bytes"])
    max_seconds = float(policy["max_scan_seconds"])
    discovered = int(scan.get("sessions_discovered") or 0)
    scanned = int(scan.get("sessions_scanned") or 0)
    bytes_scanned = int(scan.get("bytes_scanned") or 0)
    elapsed_ms = int(scan.get("elapsed_ms") or 0)
    if discovered != files:
        raise AssertionError(f"{name}: discovered={discovered}, expected={files}")
    if scanned > max_sessions:
        raise AssertionError(f"{name}: scanned={scanned} exceeds max_sessions={max_sessions}")
    if bytes_scanned > max_bytes:
        raise AssertionError(f"{name}: bytes_scanned={bytes_scanned} exceeds max_scan_bytes={max_bytes}")
    if elapsed_ms > int((max_seconds + 2.0) * 1000):
        raise AssertionError(f"{name}: elapsed_ms={elapsed_ms} exceeds bounded allowance")
    if files > max_candidates and int(scan.get("skip_counts", {}).get("candidate_limit", 0)) == 0:
        raise AssertionError(f"{name}: candidate limit was not reported")
    return {
        "complete": scan.get("complete"),
        "discovered": discovered,
        "scanned": scanned,
        "bytes_scanned": bytes_scanned,
        "elapsed_ms": elapsed_ms,
        "candidate_limit_skips": int(scan.get("skip_counts", {}).get("candidate_limit", 0)),
        "policy": policy,
    }


def run(files: int, iterations: int, max_peak_mib: int) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="code-brain-stress-bounds-") as temp:
        base = Path(temp)
        project = base / "project"
        claude_home = base / "claude"
        codex_home = base / "codex"
        project.mkdir()
        _write_json(project / ".ai" / "config.yaml", {"project_name": "stress-bounds"})
        _sandbox_fixture(project, files)
        _transcript_fixtures(project, claude_home, codex_home, files)
        retention_payload = _retention_plateau(project, files, iterations)
        audit_payload = _audit_recovery(project, files)
        sqlite_payload = _sqlite_recovery(project, files)

        tracemalloc.start()
        sandbox_payload: dict[str, Any] = {}
        claude_payload: dict[str, Any] = {}
        codex_payload: dict[str, Any] = {}
        for _ in range(iterations):
            sandbox_payload = sandbox.execution_diagnostics(project)
            claude_payload = claude_usage_summary(project, home=claude_home)
            codex_payload = codex_usage_summary(project, home=codex_home)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        sandbox_policy = sandbox_payload.get("policy", {})
        if sandbox_payload.get("bounded") is not True:
            raise AssertionError("sandbox diagnostics did not report bounded=true")
        if int(sandbox_payload.get("metas_discovered") or 0) != files:
            raise AssertionError("sandbox metadata discovery count mismatch")
        if int(sandbox_payload.get("metas_scanned") or 0) > int(sandbox_policy["max_files"]):
            raise AssertionError("sandbox metadata file limit exceeded")
        if int(sandbox_payload.get("bytes_scanned") or 0) > int(sandbox_policy["max_scan_bytes"]):
            raise AssertionError("sandbox metadata byte limit exceeded")
        if files > int(sandbox_policy["max_candidates"]) and int(
            sandbox_payload.get("skip_counts", {}).get("candidate_limit", 0)
        ) == 0:
            raise AssertionError("sandbox candidate limit was not reported")
        if int(sandbox_payload.get("killed_9", {}).get("sigkill_total", 0)) != 1:
            raise AssertionError("sandbox SIGKILL evidence was not preserved")

        peak_mib = round(peak / (1024 * 1024), 3)
        if peak_mib > max_peak_mib:
            raise AssertionError(f"traced peak {peak_mib} MiB exceeds {max_peak_mib} MiB")

        return {
            "ok": True,
            "files_per_store": files,
            "iterations": iterations,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            "tracemalloc_peak_mib": peak_mib,
            "sandbox": {
                "complete": sandbox_payload.get("complete"),
                "metas_discovered": sandbox_payload.get("metas_discovered"),
                "metas_scanned": sandbox_payload.get("metas_scanned"),
                "bytes_scanned": sandbox_payload.get("bytes_scanned"),
                "candidate_limit_skips": sandbox_payload.get("skip_counts", {}).get("candidate_limit", 0),
                "killed_9": sandbox_payload.get("killed_9"),
                "policy": sandbox_policy,
            },
            "retention": retention_payload,
            "audit": audit_payload,
            "sqlite": sqlite_payload,
            "claude": _assert_scan_bounds("claude", claude_payload, files),
            "codex": _assert_scan_bounds("codex", codex_payload, files),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stress bounded diagnostics, retention, audit recovery, and SQLite maintenance in isolation."
    )
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--max-peak-mib", type=int, default=64)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.files < 65:
        parser.error("--files must be at least 65 so candidate truncation is exercised")
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.max_peak_mib < 1:
        parser.error("--max-peak-mib must be positive")
    try:
        payload = run(args.files, args.iterations, args.max_peak_mib)
    except (AssertionError, OSError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"stress-bounds failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "stress-bounds ok "
            f"files={payload['files_per_store']} iterations={payload['iterations']} "
            f"peak_mib={payload['tracemalloc_peak_mib']} elapsed_ms={payload['elapsed_ms']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
