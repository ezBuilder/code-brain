from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_config
from .private_write import atomic_write_private_text
from .redact import redact_value

PROGRESS_SCHEMA_VERSION = 1
PROGRESS_MAX_BYTES = 64_000
DEFAULTS = {
    "enabled": True,
    "auto_rebuild": True,
    "max_files": 200_000,
    "max_candidates": 400_000,
    "max_candidate_bytes": 64_000_000,
    "max_source_bytes": 1_000_000_000,
    "max_seconds": 300,
    "stall_seconds": 30,
}
RANGES = {
    "max_files": (1, 2_000_000),
    "max_candidates": (1, 4_000_000),
    "max_candidate_bytes": (1_000_000, 1_000_000_000),
    "max_source_bytes": (1_000_000, 16_000_000_000),
    "max_seconds": (1, 86_400),
    "stall_seconds": (1, 3_600),
}
ENV_NAMES = {
    "enabled": "AI_INDEX_ENABLED",
    "auto_rebuild": "AI_INDEX_AUTO_REBUILD",
    "max_files": "AI_INDEX_MAX_FILES",
    "max_candidates": "AI_INDEX_MAX_CANDIDATES",
    "max_candidate_bytes": "AI_INDEX_MAX_CANDIDATE_BYTES",
    "max_source_bytes": "AI_INDEX_MAX_SOURCE_BYTES",
    "max_seconds": "AI_INDEX_MAX_SECONDS",
    "stall_seconds": "AI_INDEX_STALL_SECONDS",
}


class IndexScanLimit(RuntimeError):
    def __init__(self, limit: str, current: int | float, maximum: int | float) -> None:
        self.limit = limit
        self.current = current
        self.maximum = maximum
        super().__init__(f"{limit} exceeded: {current}>{maximum}")


def _parse_bool(value: object, *, field: str, errors: list[str]) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    errors.append(f"{field} must be a boolean")
    return None


def _parse_int(value: object, *, field: str, errors: list[str]) -> int | None:
    if isinstance(value, bool):
        errors.append(f"{field} must be an integer")
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer")
        return None
    minimum, maximum = RANGES[field]
    if parsed < minimum or parsed > maximum:
        errors.append(f"{field} must be between {minimum} and {maximum}")
        return None
    return parsed


def policy(root: Path, *, max_seconds: int | None = None) -> dict[str, Any]:
    errors: list[str] = []
    values: dict[str, Any] = dict(DEFAULTS)
    sources = {name: "default" for name in DEFAULTS}
    try:
        config = load_config(root)
    except Exception as exc:
        config = {}
        errors.append(str(exc))
    search = config.get("search", {}) if isinstance(config, dict) else {}
    if not isinstance(search, dict):
        errors.append("search config must be a mapping")
        search = {}
    indexing = search.get("indexing", {})
    if indexing is None:
        indexing = {}
    if not isinstance(indexing, dict):
        errors.append("search.indexing must be a mapping")
        indexing = {}

    for field in ("enabled", "auto_rebuild"):
        if field in indexing:
            parsed = _parse_bool(indexing[field], field=f"search.indexing.{field}", errors=errors)
            if parsed is not None:
                values[field] = parsed
                sources[field] = "config"
    for field in RANGES:
        if field in indexing:
            parsed = _parse_int(indexing[field], field=field, errors=errors)
            if parsed is not None:
                values[field] = parsed
                sources[field] = "config"

    for field, env_name in ENV_NAMES.items():
        if env_name not in os.environ:
            continue
        raw = os.environ.get(env_name)
        if field in {"enabled", "auto_rebuild"}:
            parsed = _parse_bool(raw, field=env_name, errors=errors)
        else:
            parsed = _parse_int(raw, field=field, errors=errors)
        if parsed is not None:
            values[field] = parsed
            sources[field] = env_name

    if max_seconds is not None:
        parsed = _parse_int(max_seconds, field="max_seconds", errors=errors)
        if parsed is not None:
            values["max_seconds"] = parsed
            sources["max_seconds"] = "argument"

    if int(values["max_candidates"]) < int(values["max_files"]):
        errors.append("max_candidates must be greater than or equal to max_files")
    return {
        "ok": not errors,
        **values,
        "errors": errors,
        "sources": sources,
    }


def progress_path(root: Path) -> Path:
    return Path(root) / ".ai" / "cache" / "index-progress.json"


def write_progress(root: Path, payload: dict[str, Any]) -> Path:
    path = progress_path(root)
    safe = redact_value(payload)
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > PROGRESS_MAX_BYTES:
        raise ValueError("index progress payload exceeds bounded size")
    atomic_write_private_text(path, encoded, root=Path(root))
    return path


def _safe_read(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None, "not_observed"
    except OSError:
        return None, "open_error"
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None, "not_regular"
        if int(getattr(opened, "st_nlink", 1)) != 1:
            return None, "unsafe_hardlink"
        if int(opened.st_size) > PROGRESS_MAX_BYTES:
            return None, "progress_too_large"
        raw = os.read(descriptor, PROGRESS_MAX_BYTES + 1)
        if len(raw) > PROGRESS_MAX_BYTES:
            return None, "progress_too_large"
        final = os.fstat(descriptor)
        if (
            int(final.st_dev) != int(opened.st_dev)
            or int(final.st_ino) != int(opened.st_ino)
            or int(final.st_mtime_ns) != int(opened.st_mtime_ns)
            or int(final.st_size) != int(opened.st_size)
        ):
            return None, "changed_during_read"
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "not_object"
    return payload, None


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def progress_status(root: Path, *, effective_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    current_policy = effective_policy or policy(root)
    payload, reason = _safe_read(progress_path(root))
    if payload is None:
        absent = reason == "not_observed"
        return {
            "ok": absent,
            "bounded": True,
            "observed": False,
            "state": "idle",
            "reason": reason,
            "stalled": False,
            "orphaned": False,
            "age_seconds": None,
        }
    valid = payload.get("schema_version") == PROGRESS_SCHEMA_VERSION and payload.get("state") in {
        "running",
        "complete",
        "failed",
        "skipped",
    }
    if not valid:
        return {
            "ok": False,
            "bounded": True,
            "observed": True,
            "state": str(payload.get("state") or "invalid"),
            "reason": "invalid_schema",
            "stalled": False,
            "orphaned": False,
            "age_seconds": None,
        }
    now = time.time()
    updated = payload.get("updated_at_unix")
    age = max(0.0, now - float(updated)) if isinstance(updated, (int, float)) else None
    state = str(payload["state"])
    running = state == "running"
    stall_seconds = int(current_policy.get("stall_seconds") or DEFAULTS["stall_seconds"])
    stalled = bool(running and age is not None and age > stall_seconds)
    orphaned = bool(running and not _pid_alive(payload.get("pid")))
    failed = state == "failed"
    skipped_ok = state == "skipped" and payload.get("error") in {"INDEXING_DISABLED", "AUTO_REBUILD_DISABLED"}
    ok = bool(not stalled and not orphaned and not failed and (state != "skipped" or skipped_ok))
    result = {
        "ok": ok,
        "bounded": True,
        "observed": True,
        "state": state,
        "reason": payload.get("error") or payload.get("reason"),
        "stalled": stalled,
        "orphaned": orphaned,
        "age_seconds": round(age, 3) if age is not None else None,
        "operation": payload.get("operation"),
        "phase": payload.get("phase"),
        "pid": payload.get("pid"),
        "started_at_unix": payload.get("started_at_unix"),
        "updated_at_unix": payload.get("updated_at_unix"),
        "finished_at_unix": payload.get("finished_at_unix"),
        "scanned_files": payload.get("scanned_files", 0),
        "indexed_files": payload.get("indexed_files", 0),
        "source_bytes": payload.get("source_bytes", 0),
        "candidate_files": payload.get("candidate_files", 0),
        "candidate_bytes": payload.get("candidate_bytes", 0),
        "current_path": payload.get("current_path"),
        "complete": payload.get("complete") is True,
        "partial": payload.get("partial") is True,
        "committed": payload.get("committed") is True,
        "limit": payload.get("limit"),
        "policy": payload.get("policy") if isinstance(payload.get("policy"), dict) else {},
    }
    return redact_value(result)


@dataclass
class IndexProgress:
    root: Path
    operation: str
    effective_policy: dict[str, Any]
    started_monotonic: float = 0.0
    started_unix: float = 0.0
    last_write_monotonic: float = 0.0
    scanned_files: int = 0
    indexed_files: int = 0
    source_bytes: int = 0
    candidate_files: int = 0
    candidate_bytes: int = 0
    current_path: str | None = None
    phase: str = "starting"
    last_write_units: int = 0
    persist: bool = True

    def begin(self) -> None:
        self.started_monotonic = time.monotonic()
        self.started_unix = time.time()
        self.last_write_monotonic = self.started_monotonic
        if self.persist:
            self._write("running", complete=False, partial=False, committed=False)

    @property
    def deadline(self) -> float:
        return self.started_monotonic + float(self.effective_policy["max_seconds"])

    def _check_time(self) -> None:
        elapsed = time.monotonic() - self.started_monotonic
        if elapsed > float(self.effective_policy["max_seconds"]):
            raise IndexScanLimit("max_seconds", round(elapsed, 3), self.effective_policy["max_seconds"])

    def candidate(self, *, size: int, path: str) -> None:
        self._check_time()
        next_files = self.candidate_files + 1
        next_bytes = self.candidate_bytes + max(0, int(size))
        if next_files > int(self.effective_policy["max_candidates"]):
            raise IndexScanLimit("max_candidates", next_files, self.effective_policy["max_candidates"])
        if next_bytes > int(self.effective_policy["max_candidate_bytes"]):
            raise IndexScanLimit(
                "max_candidate_bytes",
                next_bytes,
                self.effective_policy["max_candidate_bytes"],
            )
        self.candidate_files = next_files
        self.candidate_bytes = next_bytes
        self.current_path = path[:500]
        self.phase = "discovering"
        self.heartbeat()

    def scan(self, *, size: int, path: str) -> None:
        self._check_time()
        next_files = self.scanned_files + 1
        next_bytes = self.source_bytes + max(0, int(size))
        if next_files > int(self.effective_policy["max_files"]):
            raise IndexScanLimit("max_files", next_files, self.effective_policy["max_files"])
        if next_bytes > int(self.effective_policy["max_source_bytes"]):
            raise IndexScanLimit("max_source_bytes", next_bytes, self.effective_policy["max_source_bytes"])
        self.scanned_files = next_files
        self.source_bytes = next_bytes
        self.current_path = path[:500]
        self.phase = "indexing"
        self.heartbeat()

    def indexed(self) -> None:
        self.indexed_files += 1
        self.heartbeat()

    def heartbeat(self, *, force: bool = False) -> None:
        now = time.monotonic()
        units = self.candidate_files + self.scanned_files + self.indexed_files
        periodic = units > 0 and units != self.last_write_units and units % 25 == 0
        if force or periodic or now - self.last_write_monotonic >= 1.0:
            self.last_write_monotonic = now
            self.last_write_units = units
            if self.persist:
                self._write("running", complete=False, partial=False, committed=False)

    def complete(self, *, committed: bool, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        self.phase = "complete"
        payload = self._payload("complete", complete=True, partial=False, committed=committed)
        if extra:
            payload.update(extra)
        if self.persist:
            write_progress(self.root, payload)
            return progress_status(self.root, effective_policy=self.effective_policy)
        return redact_value(payload)

    def fail(self, error: str, *, limit: IndexScanLimit | None = None) -> dict[str, Any]:
        self.phase = "failed"
        payload = self._payload("failed", complete=False, partial=False, committed=False)
        payload["error"] = error[:200]
        if limit is not None:
            payload["limit"] = {
                "name": limit.limit,
                "current": limit.current,
                "maximum": limit.maximum,
            }
        if self.persist:
            write_progress(self.root, payload)
            return progress_status(self.root, effective_policy=self.effective_policy)
        return redact_value(payload)

    def _payload(self, state: str, *, complete: bool, partial: bool, committed: bool) -> dict[str, Any]:
        now = time.time()
        return {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "state": state,
            "operation": self.operation,
            "phase": self.phase,
            "pid": os.getpid(),
            "started_at_unix": self.started_unix,
            "updated_at_unix": now,
            "finished_at_unix": now if state != "running" else None,
            "elapsed_ms": max(0, int((time.monotonic() - self.started_monotonic) * 1000)),
            "scanned_files": self.scanned_files,
            "indexed_files": self.indexed_files,
            "source_bytes": self.source_bytes,
            "candidate_files": self.candidate_files,
            "candidate_bytes": self.candidate_bytes,
            "current_path": self.current_path,
            "complete": complete,
            "partial": partial,
            "committed": committed,
            "policy": {
                key: self.effective_policy[key]
                for key in (
                    "enabled",
                    "auto_rebuild",
                    "max_files",
                    "max_candidates",
                    "max_candidate_bytes",
                    "max_source_bytes",
                    "max_seconds",
                    "stall_seconds",
                )
            },
        }

    def _write(self, state: str, *, complete: bool, partial: bool, committed: bool) -> None:
        write_progress(
            self.root,
            self._payload(state, complete=complete, partial=partial, committed=committed),
        )


__all__ = [
    "DEFAULTS",
    "IndexProgress",
    "IndexScanLimit",
    "policy",
    "progress_path",
    "progress_status",
    "write_progress",
]
