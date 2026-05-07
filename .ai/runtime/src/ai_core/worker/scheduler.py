from __future__ import annotations

import json
import secrets
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_core.memory import append_audit
from ai_core.redact import redact_value
from ai_core.worker.lock import queue_lock

PRIORITIES = {"P0", "P1", "P2", "P3"}
LEASE_TTL_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 50
RECOVERY_SWEEP_INTERVAL_SECONDS = 60
RECOVERY_STALE_SECONDS = 3600
QUEUE_PENDING_AGE_STALE_SECONDS = 86400
QUEUE_PROCESSING_AGE_STALE_SECONDS = LEASE_TTL_SECONDS * 2


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat().replace("+00:00", "Z")


def queue_root(root: Path) -> Path:
    return root / ".ai" / "memory" / "queue"


def ensure_queue_dirs(root: Path) -> None:
    for name in (".tmp", "processing", "dead"):
        (queue_root(root) / name).mkdir(parents=True, exist_ok=True)


def enqueue(root: Path, priority: str, kind: str, payload: dict[str, Any], *, max_attempts: int | None = None) -> dict[str, Any]:
    if priority not in PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
    attempts_limit = max_attempts if max_attempts is not None else DEFAULT_MAX_ATTEMPTS
    if attempts_limit < 1 or attempts_limit > MAX_ATTEMPTS_LIMIT:
        raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}")
    with queue_lock(root):
        ensure_queue_dirs(root)
        job_id = f"{priority.lower()}-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        job = {
            "id": job_id,
            "priority": priority,
            "kind": kind,
            "payload": redact_value(payload),
            "status": "pending",
            "attempts": 0,
            "max_attempts": attempts_limit,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        tmp = queue_root(root) / ".tmp" / f"{job_id}.json.tmp"
        final = queue_root(root) / f"{job_id}.json"
        tmp.write_text(json.dumps(job, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(final)
        append_audit(root, action="queue.enqueue", category="queue", payload={"job_id": job_id, "priority": priority, "kind": kind})
        return {"ok": True, "job": job}


def lease_next(root: Path, worker_id: str, *, priority: str | None = None) -> dict[str, Any]:
    sweep_if_due(root)
    with queue_lock(root):
        ensure_queue_dirs(root)
        candidates = sorted(path for path in queue_root(root).glob("*.json") if not priority or path.name.startswith(priority.lower() + "-"))
        for source in candidates:
            target = queue_root(root) / "processing" / source.name
            try:
                source.rename(target)
            except FileNotFoundError:
                continue
            job = read_job(target)
            lease_id = secrets.token_hex(16)
            job.update(
                {
                    "status": "processing",
                    "lease_id": lease_id,
                    "worker_id": worker_id,
                    "leased_at": now_iso(),
                    "lease_expires_at": (now() + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat().replace("+00:00", "Z"),
                    "attempts": int(job.get("attempts", 0)) + 1,
                    "updated_at": now_iso(),
                }
            )
            write_job(target, job)
            append_audit(root, action="queue.lease", category="queue", payload={"job_id": job["id"], "worker_id": worker_id})
            return {"ok": True, "job": job}
        return {"ok": True, "job": None}


def complete(root: Path, job_id: str, lease_id: str) -> dict[str, Any]:
    with queue_lock(root):
        path = find_processing(root, job_id)
        job = read_job(path)
        require_lease(job, lease_id)
        path.unlink()
        append_audit(root, action="queue.complete", category="queue", payload={"job_id": job_id})
        return {"ok": True, "job_id": job_id, "status": "completed"}


def fail(root: Path, job_id: str, lease_id: str, reason: str) -> dict[str, Any]:
    with queue_lock(root):
        path = find_processing(root, job_id)
        job = read_job(path)
        require_lease(job, lease_id)
        append_attempt_history(job, outcome="failed", reason=reason)
        job.update({"status": "dead", "failed_at": now_iso(), "failure_reason": reason, "updated_at": now_iso()})
        target = queue_root(root) / "dead" / path.name
        write_job(target, job)
        path.unlink()
        append_audit(root, action="queue.fail", category="queue", payload={"job_id": job_id, "reason": reason})
        return {"ok": True, "job_id": job_id, "status": "dead"}


def recover_expired(root: Path) -> dict[str, Any]:
    with queue_lock(root):
        recovered = 0
        dead_lettered = 0
        skipped = 0
        current = now()
        for path in sorted((queue_root(root) / "processing").glob("*.json")):
            job = read_job(path)
            try:
                expires = parse_iso(str(job.get("lease_expires_at", "")))
            except ValueError:
                skipped += 1
                append_audit(
                    root,
                    action="queue.recovery_skip",
                    category="queue",
                    payload={"job_id": job.get("id"), "reason": "invalid_expires_at"},
                )
                continue
            if expires and expires < current:
                attempts = int(job.get("attempts", 0) or 0)
                max_attempts = int(job.get("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
                if attempts >= max_attempts:
                    reason = f"max_attempts_exceeded:{attempts}/{max_attempts}"
                    append_attempt_history(job, outcome="promoted_dead", expired_at=expires.isoformat().replace("+00:00", "Z"))
                    job.update(
                        {
                            "status": "dead",
                            "failed_at": now_iso(),
                            "failure_reason": reason,
                            "updated_at": now_iso(),
                        }
                    )
                    target = queue_root(root) / "dead" / path.name
                    write_job(target, job)
                    dead_lettered += 1
                    append_audit(
                        root,
                        action="queue.dead_letter_promote",
                        category="queue",
                        payload={"job_id": job.get("id"), "attempts": attempts, "max_attempts": max_attempts, "reason": reason},
                    )
                else:
                    append_attempt_history(job, outcome="expired", expired_at=expires.isoformat().replace("+00:00", "Z"))
                    job.update(
                        {
                            "status": "pending",
                            "lease_id": None,
                            "worker_id": None,
                            "leased_at": None,
                            "lease_expires_at": None,
                            "last_recovery_at": now_iso(),
                            "updated_at": now_iso(),
                        }
                    )
                    target = queue_root(root) / path.name
                    write_job(target, job)
                    recovered += 1
                path.unlink()
        if recovered or dead_lettered:
            append_audit(root, action="queue.recover_expired", category="queue", payload={"recovered": recovered, "dead_lettered": dead_lettered})
        state = write_recovery_state(root, recovered=recovered, dead_lettered=dead_lettered, skipped=skipped)
        return {
            "ok": True,
            "recovered": recovered,
            "dead_lettered": dead_lettered,
            "promoted": dead_lettered,
            "skipped": skipped,
            "recovery": state,
        }


def archive_dead(root: Path, *, older_than_days: int = 30) -> dict[str, Any]:
    with queue_lock(root):
        archived = 0
        cutoff = now() - timedelta(days=older_than_days)
        for path in sorted((queue_root(root) / "dead").glob("*.json")):
            job = read_job(path)
            failed = parse_iso(str(job.get("failed_at", job.get("updated_at", ""))))
            if failed and failed <= cutoff:
                month = failed.strftime("%Y-%m")
                target_dir = root / ".ai" / "cache" / "archive" / "dead" / month
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target_dir / path.name))
                archived += 1
        if archived:
            append_audit(root, action="queue.archive_dead", category="queue", payload={"archived": archived})
        return {"ok": True, "archived": archived}


def list_dead(root: Path, *, limit: int = 50, since_iso: str | None = None) -> dict[str, Any]:
    if limit < 0 or limit > 500:
        raise ValueError("limit must be between 0 and 500")
    ensure_queue_dirs(root)
    since = parse_iso(since_iso) if since_iso else None
    current = now()
    items: list[dict[str, Any]] = []
    skipped = 0
    total_files = 0
    for path in sorted((queue_root(root) / "dead").glob("*.json")):
        total_files += 1
        try:
            job = read_job(path)
            failed = parse_iso(str(job.get("failed_at", job.get("updated_at", ""))))
        except (OSError, ValueError, json.JSONDecodeError):
            skipped += 1
            continue
        if since and (not failed or failed < since):
            continue
        items.append(
            {
                "id": job.get("id") or path.stem,
                "priority": job.get("priority"),
                "kind": job.get("kind"),
                "status": job.get("status", "dead"),
                "attempts": int(job.get("attempts", 0) or 0),
                "max_attempts": int(job.get("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS),
                "failed_at": job.get("failed_at"),
                "failure_reason": job.get("failure_reason"),
                "age_seconds": max(0, int((current - failed).total_seconds())) if failed else None,
                "path": path.relative_to(root).as_posix(),
            }
        )
    items.sort(key=lambda item: item.get("failed_at") or "", reverse=True)
    limited = items[:limit]
    return {
        "ok": True,
        "count": total_files,
        "matched": len(items),
        "returned": len(limited),
        "skipped": skipped,
        "items": limited,
    }


def status(root: Path) -> dict[str, Any]:
    ensure_queue_dirs(root)
    expired_processing = expired_processing_jobs(root)
    recovery = recovery_status(root)
    ages = queue_age_stats(root)
    return {
        "ok": True,
        "pending": len(list(queue_root(root).glob("*.json"))),
        "processing": len(list((queue_root(root) / "processing").glob("*.json"))),
        "dead": len(list((queue_root(root) / "dead").glob("*.json"))),
        "expired_processing": len(expired_processing),
        "recovery": recovery,
        **ages,
    }


def recovery_state_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "run" / "queue.recovery.json"


def read_recovery_state(root: Path) -> dict[str, Any] | None:
    path = recovery_state_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_recovery_state"}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_recovery_state"}


def write_recovery_state(root: Path, *, recovered: int, dead_lettered: int, skipped: int) -> dict[str, Any]:
    ensure_queue_dirs(root)
    expired_remaining = len(expired_processing_jobs(root))
    state = {
        "ok": True,
        "last_run_at": now_iso(),
        "last_recovered": recovered,
        "last_dead_lettered": dead_lettered,
        "last_promoted": dead_lettered,
        "last_skipped": skipped,
        "processing_remaining": len(list((queue_root(root) / "processing").glob("*.json"))),
        "expired_remaining": expired_remaining,
    }
    path = recovery_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)
    return state


def recovery_status(root: Path) -> dict[str, Any]:
    state = read_recovery_state(root)
    expired = expired_processing_jobs(root)
    if not state:
        return {"ok": True, "state": "missing", "lag_seconds": None, "expired_processing": len(expired)}
    if state.get("ok") is False:
        return {"ok": False, "state": "invalid", "lag_seconds": None, "expired_processing": len(expired)}
    try:
        last_run = parse_iso(str(state.get("last_run_at", "")))
    except ValueError:
        return {"ok": False, "state": "invalid", "lag_seconds": None, "expired_processing": len(expired)}
    lag = int((now() - last_run).total_seconds()) if last_run else None
    return {
        "ok": lag is not None and lag <= RECOVERY_STALE_SECONDS,
        "state": "present",
        "lag_seconds": lag,
        "expired_processing": len(expired),
        "last_recovered": state.get("last_recovered", 0),
        "last_dead_lettered": state.get("last_dead_lettered", state.get("last_promoted", 0)),
        "last_skipped": state.get("last_skipped", 0),
    }


def sweep_if_due(root: Path) -> None:
    state = read_recovery_state(root)
    if state and state.get("ok") is not False:
        try:
            last_run = parse_iso(str(state.get("last_run_at", "")))
        except ValueError:
            last_run = None
        if last_run and (now() - last_run).total_seconds() < RECOVERY_SWEEP_INTERVAL_SECONDS:
            return
    recover_expired(root)


def append_attempt_history(job: dict[str, Any], *, outcome: str, expired_at: str | None = None, reason: str | None = None) -> None:
    history = job.get("attempt_history")
    if not isinstance(history, list):
        history = []
    record = {"attempt": int(job.get("attempts", 0) or 0), "outcome": outcome, "at": now_iso()}
    if expired_at:
        record["expired_at"] = expired_at
    if reason:
        record["reason"] = reason
    history.append(record)
    job["attempt_history"] = history[-10:]


def expired_processing_jobs(root: Path) -> list[dict[str, Any]]:
    current = now()
    expired: list[dict[str, Any]] = []
    processing = queue_root(root) / "processing"
    if not processing.exists():
        return expired
    for path in sorted(processing.glob("*.json")):
        try:
            job = read_job(path)
            expires = parse_iso(str(job.get("lease_expires_at", "")))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if expires and expires < current:
            expired.append(
                {
                    "id": job.get("id"),
                    "attempts": int(job.get("attempts", 0) or 0),
                    "max_attempts": int(job.get("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS),
                    "lease_expires_at": job.get("lease_expires_at"),
                    "path": path.relative_to(root).as_posix(),
                }
            )
    return expired


def queue_age_stats(root: Path) -> dict[str, Any]:
    ensure_queue_dirs(root)
    current = now()
    skipped = 0
    pending_age, pending_id, pending_path, skipped_pending = oldest_age(
        sorted(queue_root(root).glob("*.json")), "created_at", current=current, root=root
    )
    processing_age, processing_id, processing_path, skipped_processing = oldest_age(
        sorted((queue_root(root) / "processing").glob("*.json")), "leased_at", current=current, root=root
    )
    skipped += skipped_pending + skipped_processing
    return {
        "oldest_pending_age_seconds": pending_age,
        "oldest_pending_job_id": pending_id,
        "oldest_pending_job_path": pending_path,
        "oldest_processing_age_seconds": processing_age,
        "oldest_processing_job_id": processing_id,
        "oldest_processing_job_path": processing_path,
        "age_stats_skipped": skipped,
    }


def oldest_age(paths: list[Path], timestamp_field: str, *, current: datetime, root: Path) -> tuple[int, str | None, str | None, int]:
    oldest_seconds = 0
    oldest_id: str | None = None
    oldest_path: str | None = None
    skipped = 0
    for path in paths:
        try:
            job = read_job(path)
            stamp = parse_iso(str(job.get(timestamp_field, "")))
        except (OSError, ValueError, json.JSONDecodeError):
            skipped += 1
            continue
        if not stamp:
            skipped += 1
            continue
        age = max(0, int((current - stamp).total_seconds()))
        if age >= oldest_seconds:
            oldest_seconds = age
            oldest_id = str(job.get("id") or path.stem)
            oldest_path = path.relative_to(root).as_posix()
    return oldest_seconds, oldest_id, oldest_path, skipped


def find_processing(root: Path, job_id: str) -> Path:
    path = queue_root(root) / "processing" / f"{job_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"processing job not found: {job_id}")
    return path


def require_lease(job: dict[str, Any], lease_id: str) -> None:
    if job.get("lease_id") != lease_id:
        raise PermissionError("lease_id mismatch")


def read_job(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_job(path: Path, job: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
