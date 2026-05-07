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


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat().replace("+00:00", "Z")


def queue_root(root: Path) -> Path:
    return root / ".ai" / "memory" / "queue"


def ensure_queue_dirs(root: Path) -> None:
    for name in (".tmp", "processing", "dead"):
        (queue_root(root) / name).mkdir(parents=True, exist_ok=True)


def enqueue(root: Path, priority: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if priority not in PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
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
        job.update({"status": "dead", "failed_at": now_iso(), "failure_reason": reason, "updated_at": now_iso()})
        target = queue_root(root) / "dead" / path.name
        write_job(target, job)
        path.unlink()
        append_audit(root, action="queue.fail", category="queue", payload={"job_id": job_id, "reason": reason})
        return {"ok": True, "job_id": job_id, "status": "dead"}


def recover_expired(root: Path) -> dict[str, Any]:
    with queue_lock(root):
        recovered = 0
        current = now()
        for path in sorted((queue_root(root) / "processing").glob("*.json")):
            job = read_job(path)
            expires = parse_iso(str(job.get("lease_expires_at", "")))
            if expires and expires < current:
                job.update({"status": "pending", "lease_id": None, "worker_id": None, "updated_at": now_iso()})
                target = queue_root(root) / path.name
                write_job(target, job)
                path.unlink()
                recovered += 1
        if recovered:
            append_audit(root, action="queue.recover_expired", category="queue", payload={"recovered": recovered})
        return {"ok": True, "recovered": recovered}


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


def status(root: Path) -> dict[str, Any]:
    ensure_queue_dirs(root)
    return {
        "ok": True,
        "pending": len(list(queue_root(root).glob("*.json"))),
        "processing": len(list((queue_root(root) / "processing").glob("*.json"))),
        "dead": len(list((queue_root(root) / "dead").glob("*.json"))),
    }


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
