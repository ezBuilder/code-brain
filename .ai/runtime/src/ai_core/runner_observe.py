from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .private_write import atomic_write_private_text
from .redact import redact_value

RUNNER_OBSERVATION_MAX_BYTES = 256_000
RUNNER_OUTPUT_TAIL_BYTES = 64_000
RUNNER_MARKER_OVERLAP_BYTES = 512
RUNNER_OBSERVATION_NAME = "diagnostics-runner-latest.json"

_MARKERS: dict[str, re.Pattern[bytes]] = {
    "killed_9_text": re.compile(rb"(?:Killed:\s*9|SIGKILL)", re.IGNORECASE),
    "run_not_found": re.compile(rb"RUN_NOT_FOUND", re.IGNORECASE),
    "transport_restart": re.compile(
        rb"(?:transport.{0,96}(?:restart|reset|reconnect|disconnect|closed|lost|unavailable)|"
        rb"(?:restart|reset|reconnect|disconnect).{0,96}transport)",
        re.IGNORECASE | re.DOTALL,
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def observation_path(root: Path) -> Path:
    return Path(root) / ".ai" / "cache" / "diagnostics" / RUNNER_OBSERVATION_NAME


def _bounded_tail_append(tail: bytearray, chunk: bytes) -> None:
    if len(chunk) >= RUNNER_OUTPUT_TAIL_BYTES:
        tail[:] = chunk[-RUNNER_OUTPUT_TAIL_BYTES:]
        return
    tail.extend(chunk)
    overflow = len(tail) - RUNNER_OUTPUT_TAIL_BYTES
    if overflow > 0:
        del tail[:overflow]


def _marker_counts(previous: bytes, chunk: bytes, totals: dict[str, int]) -> bytes:
    window = previous + chunk
    prefix_len = len(previous)
    for name, pattern in _MARKERS.items():
        for match in pattern.finditer(window):
            if match.end() > prefix_len:
                totals[name] += 1
    return window[-RUNNER_MARKER_OVERLAP_BYTES:]


def _normalize_exit_code(returncode: int) -> tuple[int, str | None]:
    if returncode < 0:
        signal_number = -returncode
        signal_name = f"SIG{signal_number}"
        try:
            import signal

            signal_name = signal.Signals(signal_number).name
        except (ImportError, ValueError):
            pass
        return 128 + signal_number, signal_name
    if returncode == 137:
        return returncode, "SIGKILL"
    return returncode, None


def _write_observation(root: Path, payload: dict[str, Any]) -> Path:
    path = observation_path(root)
    encoded = json.dumps(redact_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode("utf-8")) > RUNNER_OBSERVATION_MAX_BYTES:
        raise ValueError("runner observation exceeds bounded payload limit")
    atomic_write_private_text(path, encoded, root=Path(root))
    try:
        from .obs import prune_diagnostics

        prune_diagnostics(Path(root), preserve=(path,))
    except OSError:
        # The observation itself is already safely persisted. Retention failure is
        # reported independently by doctor.runtime_retention.
        pass
    return path


def observe_command(
    root: Path,
    command: Sequence[str],
    *,
    label: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("command must contain non-empty string arguments")
    root = Path(root).resolve()
    effective_cwd = (cwd or root).resolve()
    try:
        effective_cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError("runner observation cwd escapes project root") from exc

    started_at = _now_iso()
    started = time.monotonic()
    digest = hashlib.sha256()
    total_bytes = 0
    tail = bytearray()
    overlap = b""
    markers = {name: 0 for name in _MARKERS}
    process = subprocess.Popen(
        list(command),
        cwd=effective_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    read_chunk = getattr(process.stdout, "read1", process.stdout.read)
    while True:
        chunk = read_chunk(64 * 1024)
        if not chunk:
            break
        total_bytes += len(chunk)
        digest.update(chunk)
        overlap = _marker_counts(overlap, chunk, markers)
        _bounded_tail_append(tail, chunk)
        if stream:
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError):
                stream = False
    raw_returncode = process.wait()
    exit_code, signal_name = _normalize_exit_code(raw_returncode)
    killed_9 = bool(signal_name == "SIGKILL" or exit_code == 137 or markers["killed_9_text"])
    transport_restart = bool(markers["run_not_found"] or markers["transport_restart"])
    payload: dict[str, Any] = {
        "schema_version": 1,
        "ok": exit_code == 0 and not killed_9 and not transport_restart,
        "observed": True,
        "label": label[:200],
        "started_at": started_at,
        "finished_at": _now_iso(),
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "command": redact_value(list(command)),
        "cwd": effective_cwd.relative_to(root).as_posix() or ".",
        "raw_returncode": raw_returncode,
        "exit_code": exit_code,
        "signal": signal_name,
        "killed_9": killed_9,
        "transport_restart": transport_restart,
        "marker_counts": markers,
        "output_total_bytes": total_bytes,
        "output_sha256": digest.hexdigest(),
        "output_tail": redact_value(tail.decode("utf-8", errors="replace")),
        "policy": {
            "read_chunk_bytes": 64 * 1024,
            "tail_bytes": RUNNER_OUTPUT_TAIL_BYTES,
            "observation_max_bytes": RUNNER_OBSERVATION_MAX_BYTES,
        },
    }
    path = _write_observation(root, payload)
    payload["path"] = path.relative_to(root).as_posix()
    return payload


def _safe_read_observation(path: Path) -> tuple[dict[str, Any] | None, str | None]:
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
        if int(opened.st_size) > RUNNER_OBSERVATION_MAX_BYTES:
            return None, "observation_too_large"
        raw = os.read(descriptor, RUNNER_OBSERVATION_MAX_BYTES + 1)
        if len(raw) > RUNNER_OBSERVATION_MAX_BYTES:
            return None, "observation_too_large"
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


def observation_status(root: Path) -> dict[str, Any]:
    payload, reason = _safe_read_observation(observation_path(root))
    if payload is None:
        absent = reason == "not_observed"
        return {
            "ok": absent,
            "bounded": True,
            "observed": False,
            "reason": reason,
            "killed_9": False,
            "transport_restart": False,
            "exit_code": None,
            "marker_counts": {},
            "policy": {
                "observation_max_bytes": RUNNER_OBSERVATION_MAX_BYTES,
            },
        }
    required = {
        "schema_version",
        "ok",
        "observed",
        "label",
        "exit_code",
        "killed_9",
        "transport_restart",
        "marker_counts",
        "policy",
    }
    valid = required.issubset(payload) and payload.get("schema_version") == 1
    return redact_value(
        {
            "ok": bool(valid and payload.get("ok") is True),
            "bounded": True,
            "observed": True,
            "reason": None if valid else "invalid_schema",
            "label": payload.get("label"),
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "elapsed_ms": payload.get("elapsed_ms"),
            "exit_code": payload.get("exit_code"),
            "signal": payload.get("signal"),
            "killed_9": payload.get("killed_9") is True,
            "transport_restart": payload.get("transport_restart") is True,
            "marker_counts": payload.get("marker_counts") if isinstance(payload.get("marker_counts"), dict) else {},
            "output_total_bytes": payload.get("output_total_bytes"),
            "output_sha256": payload.get("output_sha256"),
            "policy": payload.get("policy") if isinstance(payload.get("policy"), dict) else {},
        }
    )


__all__ = ["observe_command", "observation_path", "observation_status"]
