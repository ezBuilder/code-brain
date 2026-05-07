from __future__ import annotations

import json
import platform
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .doctor import as_payload, run_checks
from .redact import redact_value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_path(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return root / ".ai" / "cache" / "logs" / f"{stamp}.jsonl"


def write_log(root: Path, level: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "ts": now_iso(),
        "level": level,
        "event": event,
        "payload": redact_value(payload),
    }
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.open("a", encoding="utf-8").write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "path": path.relative_to(root).as_posix(), "record": record}


def metrics(root: Path) -> dict[str, Any]:
    from .worker.scheduler import queue_age_stats, recovery_status

    queue_root = root / ".ai" / "memory" / "queue"
    recovery = recovery_status(root)
    ages = queue_age_stats(root)
    return {
        "ok": True,
        "runtime_version": __version__,
        "queue": {
            "pending": len(list(queue_root.glob("*.json"))),
            "processing": len(list((queue_root / "processing").glob("*.json"))),
            "dead": len(list((queue_root / "dead").glob("*.json"))),
            "expired_processing": recovery["expired_processing"],
            "recovery_lag_seconds": recovery["lag_seconds"],
            "last_recovered": recovery.get("last_recovered", 0),
            "last_dead_lettered": recovery.get("last_dead_lettered", 0),
            **ages,
        },
        "cache": {
            "code_sqlite_exists": (root / ".ai" / "cache" / "code.sqlite").exists(),
        },
    }


def health_summary(root: Path) -> dict[str, Any]:
    from .worker.lock import lock_status
    from .worker.scheduler import QUEUE_PENDING_AGE_STALE_SECONDS, QUEUE_PROCESSING_AGE_STALE_SECONDS, status as queue_status

    doctor = as_payload(run_checks(root))
    failed_checks = [check["name"] for check in doctor.get("checks", []) if not check.get("ok")]
    worker = lock_status(root)
    queue = queue_status(root)
    pending_age = int(queue.get("oldest_pending_age_seconds", 0) or 0)
    processing_age = int(queue.get("oldest_processing_age_seconds", 0) or 0)
    payload = {
        "ok": bool(
            doctor.get("ok")
            and not worker.get("stale")
            and not worker.get("cross_host")
            and pending_age <= QUEUE_PENDING_AGE_STALE_SECONDS
            and processing_age <= QUEUE_PROCESSING_AGE_STALE_SECONDS
        ),
        "doctor": {"ok": bool(doctor.get("ok")), "failed": failed_checks},
        "worker": {
            "locked": bool(worker.get("locked")),
            "stale": bool(worker.get("stale")),
            "cross_host": bool(worker.get("cross_host")),
            "reason": worker.get("reason"),
            "pid": worker.get("pid"),
        },
        "queue": {
            "pending": int(queue.get("pending", 0) or 0),
            "processing": int(queue.get("processing", 0) or 0),
            "dead": int(queue.get("dead", 0) or 0),
            "expired_processing": int(queue.get("expired_processing", 0) or 0),
            "oldest_pending_age_seconds": pending_age,
            "oldest_processing_age_seconds": processing_age,
            "age_stats_skipped": int(queue.get("age_stats_skipped", 0) or 0),
        },
        "release_artifacts": release_artifact_summary(root),
    }
    return redact_value(payload)


def release_artifact_summary(root: Path) -> dict[str, Any]:
    summary_path = root / "dist" / "release-gate.summary.json"
    if not summary_path.exists():
        return {
            "summary_path": None,
            "release_ready": None,
            "all_present": None,
            "all_valid": None,
            "all_current": None,
        }
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    artifacts = summary.get("release_artifacts", {}) if isinstance(summary, dict) else {}
    return {
        "summary_path": summary_path.relative_to(root).as_posix(),
        "release_ready": summary.get("release_ready") if isinstance(summary, dict) else None,
        "all_present": artifacts.get("all_present") if isinstance(artifacts, dict) else None,
        "all_valid": artifacts.get("all_valid") if isinstance(artifacts, dict) else None,
        "all_current": artifacts.get("all_current") if isinstance(artifacts, dict) else None,
    }


def slo_bench(root: Path, iterations: int = 10) -> dict[str, Any]:
    from .hooks import handle_hook

    elapsed: list[int] = []
    for _ in range(iterations):
        result = handle_hook(root, "SLOBaseline", {"agent": "bench", "dry": True})
        elapsed.append(int(result["elapsed_ms"]))
    p95 = sorted(elapsed)[max(0, int(len(elapsed) * 0.95) - 1)] if elapsed else 0
    return {"ok": p95 <= 200, "iterations": iterations, "p95_ms": p95, "target_ms": 200, "samples_ms": elapsed}


def diagnostics(root: Path, *, dry_run: bool = False, include_doctor: bool = True) -> dict[str, Any]:
    checks = as_payload(run_checks(root)) if include_doctor else {"ok": True, "checks": []}
    bundle = {
        "created_at": now_iso(),
        "runtime_version": __version__,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
        },
        "doctor": redact_value(checks),
        "metrics": redact_value(metrics(root)),
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "bundle": bundle}
    diag_root = root / ".ai" / "cache" / "diagnostics"
    diag_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = diag_root / f"diagnostics-{stamp}.json"
    zip_path = diag_root / f"diagnostics-{stamp}.zip"
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(json_path, json_path.name)
    return {"ok": True, "dry_run": False, "path": zip_path.relative_to(root).as_posix(), "retention_days": 30}


def prune_diagnostics(root: Path, *, keep_days: int = 30) -> dict[str, Any]:
    cutoff = time.time() - keep_days * 86400
    removed = 0
    diag_root = root / ".ai" / "cache" / "diagnostics"
    if not diag_root.exists():
        return {"ok": True, "removed": 0}
    for path in diag_root.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    for path in diag_root.iterdir():
        if path.is_dir() and path.stat().st_mtime < cutoff:
            shutil.rmtree(path)
            removed += 1
    return {"ok": True, "removed": removed}
