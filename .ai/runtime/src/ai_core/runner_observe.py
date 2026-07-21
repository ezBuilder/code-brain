from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .private_write import atomic_write_private_text
from .redact import redact_value
from .resource_diag import classify_termination, process_tree_rss_kib, system_memory_snapshot

RUNNER_OBSERVATION_SCHEMA_VERSION = 5
RUNNER_OBSERVATION_MAX_BYTES = 256_000
RUNNER_OUTPUT_TAIL_BYTES = 64_000
RUNNER_MARKER_OVERLAP_BYTES = 512
RUNNER_COMMAND_MAX_BYTES = 16_000
RUNNER_COMMAND_PREVIEW_BYTES = 4_000
RUNNER_RSS_SAMPLE_INTERVAL_SECONDS = 0.05
RUNNER_TERMINATE_GRACE_SECONDS = 1.0
RUNNER_WATCHER_JOIN_SECONDS = (RUNNER_TERMINATE_GRACE_SECONDS * 2) + 0.25
RUNNER_SPAWN_FAILURE_EXIT_CODE = 127
RUNNER_OBSERVER_FAILURE_EXIT_CODE = 70
RUNNER_TIMEOUT_EXIT_CODE = 124
RUNNER_OBSERVATION_NAME = "diagnostics-runner-latest.json"
RUNNER_EVIDENCE_TOKEN_MAX_BYTES = 4096
_OBSERVED_PARENT_SIGNALS = (signal.SIGINT, signal.SIGTERM)

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


def _signal_name(signal_number: int) -> str:
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return f"SIG{signal_number}"


def _bounded_command(command: Sequence[str]) -> Any:
    cleaned = redact_value(list(command))
    encoded = json.dumps(
        cleaned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= RUNNER_COMMAND_MAX_BYTES:
        return cleaned
    return {
        "truncated": True,
        "argc": len(command),
        "original_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preview": encoded[:RUNNER_COMMAND_PREVIEW_BYTES].decode("utf-8", errors="ignore"),
    }


def _sample_peak_rss(
    pid: int,
    stop: threading.Event,
    state: dict[str, int | None],
) -> None:
    while True:
        try:
            value = process_tree_rss_kib(pid)
        except Exception:
            value = None
            state["rss_sample_errors"] = int(state.get("rss_sample_errors") or 0) + 1
        if value is not None:
            current = state.get("peak_rss_kib")
            state["peak_rss_kib"] = value if current is None else max(current, value)
            state["rss_samples"] = int(state.get("rss_samples") or 0) + 1
        if stop.wait(RUNNER_RSS_SAMPLE_INTERVAL_SECONDS):
            break


def _observer_error(exc: BaseException, *, phase: str) -> dict[str, Any]:
    errno_value = getattr(exc, "errno", None)
    return redact_value(
        {
            "phase": phase,
            "type": type(exc).__name__,
            "errno": int(errno_value) if isinstance(errno_value, int) else None,
            "message": str(exc)[:1000],
        }
    )


def _normalize_timeout_seconds(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a finite positive number")
    normalized = float(timeout_seconds)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    return normalized


def _evidence_token_sha256(evidence_token: str | None) -> str | None:
    if evidence_token is None or evidence_token == "":
        return None
    if not isinstance(evidence_token, str):
        raise ValueError("evidence_token must be a string")
    encoded = evidence_token.encode("utf-8")
    if len(encoded) > RUNNER_EVIDENCE_TOKEN_MAX_BYTES:
        raise ValueError("evidence_token exceeds bounded input limit")
    return hashlib.sha256(encoded).hexdigest()


def _send_observed_signal(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, sig)
            return
        except (OSError, ProcessLookupError, PermissionError):
            pass
    if process.poll() is not None:
        return
    try:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (OSError, ProcessLookupError, PermissionError):
        pass


def _observed_process_group_alive(process: subprocess.Popen[bytes]) -> bool | None:
    if os.name == "nt":
        return None
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _wait_observed_cleanup(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        leader_done = process.poll() is not None
        group_alive = _observed_process_group_alive(process)
        if leader_done and group_alive is not True:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not leader_done:
            try:
                process.wait(timeout=min(0.05, remaining))
                continue
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        time.sleep(min(0.02, remaining))


def _terminate_observed_process(
    process: subprocess.Popen[bytes],
    *,
    initial_signal: signal.Signals = signal.SIGTERM,
) -> bool:
    _send_observed_signal(process, initial_signal)
    if _wait_observed_cleanup(process, RUNNER_TERMINATE_GRACE_SECONDS):
        return True
    _send_observed_signal(process, signal.SIGKILL)
    return _wait_observed_cleanup(process, RUNNER_TERMINATE_GRACE_SECONDS)


def _install_observer_signal_handlers(
    trigger: threading.Event,
    state: dict[str, Any],
) -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}

    previous: dict[int, Any] = {}

    def handle(signum: int, _frame: Any) -> None:
        state["count"] = int(state.get("count") or 0) + 1
        if state.get("signal_number") is None:
            state["signal_number"] = int(signum)
        trigger.set()

    try:
        for observed_signal in _OBSERVED_PARENT_SIGNALS:
            signal_number = int(observed_signal)
            previous[signal_number] = signal.getsignal(observed_signal)
            signal.signal(observed_signal, handle)
    except BaseException:
        for signal_number, old_handler in previous.items():
            try:
                signal.signal(signal_number, old_handler)
            except BaseException:
                pass
        raise
    return previous


def _restore_observer_signal_handlers(previous: dict[int, Any]) -> None:
    for signal_number, old_handler in previous.items():
        signal.signal(signal_number, old_handler)


def _watch_observer_interrupt(
    process: subprocess.Popen[bytes],
    trigger: threading.Event,
    stop: threading.Event,
    state: dict[str, Any],
) -> None:
    try:
        trigger.wait()
        if stop.is_set() or state.get("signal_number") is None:
            return
        signal_number = int(state["signal_number"])
        try:
            initial_signal = signal.Signals(signal_number)
        except ValueError:
            initial_signal = signal.SIGTERM
        if os.name == "nt":
            initial_signal = signal.SIGTERM
        state["cleanup_completed"] = _terminate_observed_process(
            process,
            initial_signal=initial_signal,
        )
        if state["cleanup_completed"] is not True and process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass
    except BaseException as exc:
        state["error"] = _observer_error(exc, phase="interrupt_watcher")
        try:
            state["cleanup_completed"] = _terminate_observed_process(process)
        except BaseException:
            state["cleanup_completed"] = False
        if process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass


def _watch_observed_timeout(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
    stop: threading.Event,
    state: dict[str, Any],
) -> None:
    try:
        if stop.wait(timeout_seconds) or process.poll() is not None:
            return
        state["timed_out"] = True
        state["cleanup_completed"] = _terminate_observed_process(process)
        if state["cleanup_completed"] is not True and process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass
    except BaseException as exc:
        state["error"] = _observer_error(exc, phase="timeout_watcher")
        try:
            state["cleanup_completed"] = _terminate_observed_process(process)
        except BaseException:
            state["cleanup_completed"] = False
        if process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass


def _termination_with_observer_error(
    *,
    raw_returncode: int | None,
    observer_error: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    peak_rss_kib: int | None,
    stderr: str,
    timed_out: bool,
) -> dict[str, Any]:
    child = classify_termination(
        returncode=raw_returncode,
        timed_out=timed_out,
        before=before,
        after=after,
        peak_rss_kib=peak_rss_kib,
        stderr=stderr,
    )
    phase = str(observer_error.get("phase") or "unknown")
    classification = "spawn_error" if phase == "spawn" else "observer_failure"
    return {
        "classification": classification,
        "confidence": "high",
        "returncode": raw_returncode,
        "signal": child.get("signal"),
        "signal_number": child.get("signal_number"),
        "shell_mapped": child.get("shell_mapped") is True,
        "child_classification": child.get("classification"),
        "evidence": [
            f"observer_phase={phase}",
            f"observer_error={observer_error.get('type') or 'unknown'}",
            *(child.get("evidence") if isinstance(child.get("evidence"), list) else []),
        ],
        "recommendations": [
            "inspect observer_error and verify the command, cwd, and local process I/O",
            *(child.get("recommendations") if isinstance(child.get("recommendations"), list) else []),
        ],
    }


def _termination_with_observer_interrupt(
    *,
    raw_returncode: int | None,
    signal_number: int,
    signal_count: int,
    before: dict[str, Any],
    after: dict[str, Any],
    peak_rss_kib: int | None,
    stderr: str,
    timed_out: bool,
) -> dict[str, Any]:
    child = classify_termination(
        returncode=raw_returncode,
        timed_out=timed_out,
        before=before,
        after=after,
        peak_rss_kib=peak_rss_kib,
        stderr=stderr,
    )
    observer_signal = _signal_name(signal_number)
    return {
        "classification": "observer_interrupted",
        "confidence": "high",
        "returncode": raw_returncode,
        "signal": observer_signal,
        "signal_number": signal_number,
        "signal_count": signal_count,
        "shell_mapped": False,
        "child_classification": child.get("classification"),
        "child_signal": child.get("signal"),
        "child_signal_number": child.get("signal_number"),
        "evidence": [
            f"observer_signal={observer_signal}",
            f"observer_signal_count={signal_count}",
            *(child.get("evidence") if isinstance(child.get("evidence"), list) else []),
        ],
        "recommendations": [
            "treat the run as incomplete and inspect the initiating operator or runner cancellation",
            *(child.get("recommendations") if isinstance(child.get("recommendations"), list) else []),
        ],
    }


def _build_observation_payload(
    *,
    command: Sequence[str],
    label: str,
    cwd_text: str,
    started_at: str,
    started: float,
    raw_returncode: int | None,
    observer_error: dict[str, Any] | None,
    markers: dict[str, int],
    resource_before: dict[str, Any],
    resource_after: dict[str, Any],
    peak_rss_kib: int | None,
    rss_samples: int,
    rss_sample_errors: int,
    timed_out: bool,
    timeout_seconds: float | None,
    interrupt_signal_number: int | None,
    interrupt_signal_count: int,
    interrupt_observation_enabled: bool,
    evidence_token_sha256: str | None,
    total_bytes: int,
    digest: Any,
    tail: bytearray,
) -> dict[str, Any]:
    tail_text = tail.decode("utf-8", errors="replace")
    if raw_returncode is None:
        normalized_exit_code = RUNNER_SPAWN_FAILURE_EXIT_CODE
        signal_name = None
    else:
        normalized_exit_code, signal_name = _normalize_exit_code(raw_returncode)
    interrupted = interrupt_signal_number is not None
    observer_signal = _signal_name(interrupt_signal_number) if interrupted else None
    if observer_error is not None and str(observer_error.get("phase")) != "spawn":
        exit_code = RUNNER_OBSERVER_FAILURE_EXIT_CODE
    elif interrupted:
        exit_code = 128 + int(interrupt_signal_number)
    elif timed_out:
        exit_code = RUNNER_TIMEOUT_EXIT_CODE
    else:
        exit_code = normalized_exit_code
    killed_9 = bool(
        not timed_out
        and not interrupted
        and (
            signal_name == "SIGKILL"
            or normalized_exit_code == 137
            or markers["killed_9_text"]
        )
    )
    transport_restart = bool(markers["run_not_found"] or markers["transport_restart"])
    if observer_error is not None:
        termination = _termination_with_observer_error(
            raw_returncode=raw_returncode,
            observer_error=observer_error,
            before=resource_before,
            after=resource_after,
            peak_rss_kib=peak_rss_kib,
            stderr=tail_text,
            timed_out=timed_out,
        )
    elif interrupted:
        termination = _termination_with_observer_interrupt(
            raw_returncode=raw_returncode,
            signal_number=int(interrupt_signal_number),
            signal_count=interrupt_signal_count,
            before=resource_before,
            after=resource_after,
            peak_rss_kib=peak_rss_kib,
            stderr=tail_text,
            timed_out=timed_out,
        )
    else:
        termination = classify_termination(
            returncode=raw_returncode,
            timed_out=timed_out,
            before=resource_before,
            after=resource_after,
            peak_rss_kib=peak_rss_kib,
            stderr=tail_text,
        )
    return {
        "schema_version": RUNNER_OBSERVATION_SCHEMA_VERSION,
        "ok": (
            raw_returncode == 0
            and observer_error is None
            and not timed_out
            and not interrupted
            and not killed_9
            and not transport_restart
        ),
        "observed": True,
        "spawned": str((observer_error or {}).get("phase")) != "spawn",
        "observer_error": observer_error,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "observer_signal": observer_signal,
        "observer_signal_number": interrupt_signal_number,
        "observer_signal_count": interrupt_signal_count,
        "interrupt_observation_enabled": interrupt_observation_enabled,
        "evidence_token_sha256": evidence_token_sha256,
        "label": label[:200],
        "started_at": started_at,
        "finished_at": _now_iso(),
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "command": _bounded_command(command),
        "cwd": cwd_text,
        "raw_returncode": raw_returncode,
        "exit_code": exit_code,
        "signal": signal_name,
        "killed_9": killed_9,
        "transport_restart": transport_restart,
        "marker_counts": markers,
        "peak_rss_kib": peak_rss_kib,
        "rss_samples": rss_samples,
        "rss_sample_errors": rss_sample_errors,
        "resource_before": resource_before,
        "resource_after": resource_after,
        "termination": termination,
        "output_total_bytes": total_bytes,
        "output_sha256": digest.hexdigest(),
        "output_tail": redact_value(tail_text),
        "policy": {
            "read_chunk_bytes": 64 * 1024,
            "tail_bytes": RUNNER_OUTPUT_TAIL_BYTES,
            "observation_max_bytes": RUNNER_OBSERVATION_MAX_BYTES,
            "command_max_bytes": RUNNER_COMMAND_MAX_BYTES,
            "command_preview_bytes": RUNNER_COMMAND_PREVIEW_BYTES,
            "rss_sample_interval_ms": int(RUNNER_RSS_SAMPLE_INTERVAL_SECONDS * 1000),
            "terminate_grace_ms": int(RUNNER_TERMINATE_GRACE_SECONDS * 1000),
            "watcher_join_ms": int(RUNNER_WATCHER_JOIN_SECONDS * 1000),
            "timeout_seconds": timeout_seconds,
            "observed_parent_signals": [item.name for item in _OBSERVED_PARENT_SIGNALS],
            "evidence_token_max_bytes": RUNNER_EVIDENCE_TOKEN_MAX_BYTES,
        },
    }


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
    timeout_seconds: float | None = None,
    evidence_token: str | None = None,
) -> dict[str, Any]:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("command must contain non-empty string arguments")
    timeout_seconds = _normalize_timeout_seconds(timeout_seconds)
    evidence_token_sha256 = _evidence_token_sha256(evidence_token)
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
    resource_before = system_memory_snapshot()
    cwd_text = effective_cwd.relative_to(root).as_posix() or "."
    try:
        process = subprocess.Popen(
            list(command),
            cwd=effective_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
    except (OSError, ValueError) as exc:
        payload = _build_observation_payload(
            command=command,
            label=label,
            cwd_text=cwd_text,
            started_at=started_at,
            started=started,
            raw_returncode=None,
            observer_error=_observer_error(exc, phase="spawn"),
            markers=markers,
            resource_before=resource_before,
            resource_after=system_memory_snapshot(),
            peak_rss_kib=None,
            rss_samples=0,
            rss_sample_errors=0,
            timed_out=False,
            timeout_seconds=timeout_seconds,
            interrupt_signal_number=None,
            interrupt_signal_count=0,
            interrupt_observation_enabled=False,
            evidence_token_sha256=evidence_token_sha256,
            total_bytes=total_bytes,
            digest=digest,
            tail=tail,
        )
        path = _write_observation(root, payload)
        payload["path"] = path.relative_to(root).as_posix()
        return payload
    initial_sample_errors = 0
    try:
        initial_peak_rss = process_tree_rss_kib(process.pid)
    except Exception:
        initial_peak_rss = None
        initial_sample_errors = 1
    rss_state: dict[str, int | None] = {
        "peak_rss_kib": initial_peak_rss,
        "rss_samples": 1 if initial_peak_rss is not None else 0,
        "rss_sample_errors": initial_sample_errors,
    }
    rss_stop = threading.Event()
    rss_thread = threading.Thread(
        target=_sample_peak_rss,
        args=(process.pid, rss_stop, rss_state),
        name="code-brain-runner-rss",
        daemon=True,
    )
    timeout_stop = threading.Event()
    timeout_state: dict[str, Any] = {
        "timed_out": False,
        "cleanup_completed": None,
        "error": None,
    }
    timeout_thread = (
        threading.Thread(
            target=_watch_observed_timeout,
            args=(process, timeout_seconds, timeout_stop, timeout_state),
            name="code-brain-runner-timeout",
            daemon=True,
        )
        if timeout_seconds is not None
        else None
    )
    interrupt_trigger = threading.Event()
    interrupt_stop = threading.Event()
    interrupt_state: dict[str, Any] = {
        "signal_number": None,
        "count": 0,
        "cleanup_completed": None,
        "error": None,
    }
    interrupt_thread: threading.Thread | None = None
    previous_signal_handlers: dict[int, Any] = {}
    interrupt_observation_enabled = False
    observer_error: dict[str, Any] | None = None
    deferred_error: BaseException | None = None
    raw_returncode: int | None = None
    rss_thread_started = False
    timeout_thread_started = False
    interrupt_thread_started = False
    current_phase = "signal_handler_install"
    try:
        previous_signal_handlers = _install_observer_signal_handlers(
            interrupt_trigger,
            interrupt_state,
        )
        interrupt_observation_enabled = bool(previous_signal_handlers)
        if interrupt_observation_enabled:
            current_phase = "interrupt_watcher_start"
            interrupt_thread = threading.Thread(
                target=_watch_observer_interrupt,
                args=(process, interrupt_trigger, interrupt_stop, interrupt_state),
                name="code-brain-runner-interrupt",
                daemon=True,
            )
            interrupt_thread.start()
            interrupt_thread_started = True
        current_phase = "rss_sampler_start"
        rss_thread.start()
        rss_thread_started = True
        if timeout_thread is not None:
            current_phase = "timeout_watcher_start"
            timeout_thread.start()
            timeout_thread_started = True
        current_phase = "read_output"
        if process.stdout is None:
            raise OSError("observed process stdout pipe is unavailable")
        read_chunk = process.stdout.read1 if hasattr(process.stdout, "read1") else process.stdout.read
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
    except BaseException as exc:
        observer_error = _observer_error(exc, phase=current_phase)
        deferred_error = exc if not isinstance(exc, Exception) else None
        _terminate_observed_process(process)
    finally:
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception as exc:
            if observer_error is None:
                observer_error = _observer_error(exc, phase="close_output")
        try:
            raw_returncode = process.wait(
                timeout=(
                    RUNNER_TERMINATE_GRACE_SECONDS
                    if (
                        observer_error is not None
                        or timeout_state["timed_out"] is True
                        or interrupt_state["signal_number"] is not None
                    )
                    else None
                )
            )
        except subprocess.TimeoutExpired as exc:
            if observer_error is None:
                observer_error = _observer_error(exc, phase="wait")
            _terminate_observed_process(process)
            raw_returncode = process.poll()
        except (OSError, ValueError) as exc:
            if observer_error is None:
                observer_error = _observer_error(exc, phase="wait")
            _terminate_observed_process(process)
            raw_returncode = process.poll()
        finally:
            timeout_stop.set()
            if timeout_thread_started and timeout_thread is not None:
                timeout_thread.join(timeout=RUNNER_WATCHER_JOIN_SECONDS)
                if timeout_thread.is_alive() and observer_error is None:
                    observer_error = _observer_error(
                        RuntimeError("timeout watcher did not stop within the bounded grace period"),
                        phase="timeout_watcher_join",
                    )
                elif isinstance(timeout_state.get("error"), dict) and observer_error is None:
                    observer_error = timeout_state["error"]
                elif (
                    timeout_state.get("timed_out") is True
                    and timeout_state.get("cleanup_completed") is not True
                    and observer_error is None
                ):
                    observer_error = _observer_error(
                        RuntimeError("timed-out process group cleanup did not complete"),
                        phase="timeout_cleanup",
                    )
            interrupt_stop.set()
            interrupt_trigger.set()
            if interrupt_thread_started and interrupt_thread is not None:
                interrupt_thread.join(timeout=RUNNER_WATCHER_JOIN_SECONDS)
                if interrupt_thread.is_alive() and observer_error is None:
                    observer_error = _observer_error(
                        RuntimeError("interrupt watcher did not stop within the bounded grace period"),
                        phase="interrupt_watcher_join",
                    )
                elif isinstance(interrupt_state.get("error"), dict) and observer_error is None:
                    observer_error = interrupt_state["error"]
                elif (
                    interrupt_state.get("signal_number") is not None
                    and interrupt_state.get("cleanup_completed") is not True
                    and observer_error is None
                ):
                    observer_error = _observer_error(
                        RuntimeError("interrupted process group cleanup did not complete"),
                        phase="interrupt_cleanup",
                    )
            rss_stop.set()
            if rss_thread_started:
                rss_thread.join(timeout=RUNNER_TERMINATE_GRACE_SECONDS)
                if rss_thread.is_alive() and observer_error is None:
                    observer_error = _observer_error(
                        RuntimeError("RSS sampler did not stop within the bounded grace period"),
                        phase="rss_sampler_join",
                    )
            try:
                _restore_observer_signal_handlers(previous_signal_handlers)
            except BaseException as exc:
                if observer_error is None:
                    observer_error = _observer_error(exc, phase="signal_handler_restore")
    try:
        final_rss = process_tree_rss_kib(process.pid)
    except Exception:
        final_rss = None
        rss_state["rss_sample_errors"] = int(rss_state.get("rss_sample_errors") or 0) + 1
    if final_rss is not None:
        current_peak = rss_state.get("peak_rss_kib")
        rss_state["peak_rss_kib"] = final_rss if current_peak is None else max(current_peak, final_rss)
        rss_state["rss_samples"] = int(rss_state.get("rss_samples") or 0) + 1
    peak_rss_kib = rss_state.get("peak_rss_kib")
    resource_after = system_memory_snapshot()
    payload = _build_observation_payload(
        command=command,
        label=label,
        cwd_text=cwd_text,
        started_at=started_at,
        started=started,
        raw_returncode=raw_returncode,
        observer_error=observer_error,
        markers=markers,
        resource_before=resource_before,
        resource_after=resource_after,
        peak_rss_kib=peak_rss_kib,
        rss_samples=int(rss_state.get("rss_samples") or 0),
        rss_sample_errors=int(rss_state.get("rss_sample_errors") or 0),
        timed_out=timeout_state["timed_out"] is True,
        timeout_seconds=timeout_seconds,
        interrupt_signal_number=(
            int(interrupt_state["signal_number"])
            if interrupt_state.get("signal_number") is not None
            else None
        ),
        interrupt_signal_count=int(interrupt_state.get("count") or 0),
        interrupt_observation_enabled=interrupt_observation_enabled,
        evidence_token_sha256=evidence_token_sha256,
        total_bytes=total_bytes,
        digest=digest,
        tail=tail,
    )
    path = _write_observation(root, payload)
    payload["path"] = path.relative_to(root).as_posix()
    if deferred_error is not None:
        raise deferred_error
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
            "spawned": False,
            "timed_out": False,
            "interrupted": False,
            "observer_signal": None,
            "observer_signal_number": None,
            "observer_signal_count": 0,
            "interrupt_observation_enabled": False,
            "evidence_token_sha256": None,
            "schema_version": RUNNER_OBSERVATION_SCHEMA_VERSION,
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
        "spawned",
        "timed_out",
        "interrupted",
        "observer_signal",
        "observer_signal_number",
        "observer_signal_count",
        "interrupt_observation_enabled",
        "evidence_token_sha256",
        "observer_error",
        "label",
        "exit_code",
        "killed_9",
        "transport_restart",
        "marker_counts",
        "peak_rss_kib",
        "rss_samples",
        "rss_sample_errors",
        "resource_before",
        "resource_after",
        "termination",
        "policy",
    }
    valid = (
        required.issubset(payload)
        and payload.get("schema_version") == RUNNER_OBSERVATION_SCHEMA_VERSION
        and type(payload.get("ok")) is bool
        and payload.get("observed") is True
        and type(payload.get("spawned")) is bool
        and type(payload.get("timed_out")) is bool
        and type(payload.get("interrupted")) is bool
        and type(payload.get("interrupt_observation_enabled")) is bool
        and type(payload.get("observer_signal_count")) is int
        and int(payload.get("observer_signal_count")) >= 0
        and (
            (
                payload.get("interrupted") is False
                and payload.get("observer_signal") is None
                and payload.get("observer_signal_number") is None
                and payload.get("observer_signal_count") == 0
            )
            or (
                payload.get("interrupted") is True
                and isinstance(payload.get("observer_signal"), str)
                and re.fullmatch(r"SIG[A-Z0-9]+", payload.get("observer_signal")) is not None
                and type(payload.get("observer_signal_number")) is int
                and int(payload.get("observer_signal_number")) > 0
                and payload.get("observer_signal_count", 0) >= 1
                and payload.get("observer_signal")
                == _signal_name(int(payload.get("observer_signal_number")))
            )
        )
        and (
            payload.get("evidence_token_sha256") is None
            or (
                isinstance(payload.get("evidence_token_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", payload.get("evidence_token_sha256"))
                is not None
            )
        )
        and (
            payload.get("observer_error") is None
            or isinstance(payload.get("observer_error"), dict)
        )
        and isinstance(payload.get("label"), str)
        and type(payload.get("exit_code")) is int
        and type(payload.get("killed_9")) is bool
        and type(payload.get("transport_restart")) is bool
        and isinstance(payload.get("marker_counts"), dict)
        and (
            payload.get("peak_rss_kib") is None
            or (
                type(payload.get("peak_rss_kib")) is int
                and int(payload.get("peak_rss_kib")) >= 0
            )
        )
        and isinstance(payload.get("resource_before"), dict)
        and isinstance(payload.get("resource_after"), dict)
        and isinstance(payload.get("termination"), dict)
        and type(payload.get("rss_samples")) is int
        and int(payload.get("rss_samples")) >= 0
        and type(payload.get("rss_sample_errors")) is int
        and int(payload.get("rss_sample_errors")) >= 0
        and isinstance(payload.get("policy"), dict)
        and not (
            payload.get("ok") is True
            and (
                payload.get("spawned") is not True
                or payload.get("observer_error") is not None
                or payload.get("timed_out") is True
                or payload.get("interrupted") is True
                or payload.get("exit_code") != 0
                or payload.get("killed_9") is True
                or payload.get("transport_restart") is True
            )
        )
        and not (
            payload.get("spawned") is False
            and (
                payload.get("timed_out") is True
                or not isinstance(payload.get("observer_error"), dict)
                or payload.get("observer_error", {}).get("phase") != "spawn"
            )
        )
        and not (
            payload.get("timed_out") is True
            and (
                payload.get("spawned") is not True
                or payload.get("ok") is True
                or payload.get("exit_code") not in {
                    RUNNER_TIMEOUT_EXIT_CODE,
                    RUNNER_OBSERVER_FAILURE_EXIT_CODE,
                }
            )
        )
        and not (
            payload.get("interrupted") is True
            and (
                payload.get("spawned") is not True
                or payload.get("ok") is True
                or payload.get("exit_code") not in {
                    128 + int(payload.get("observer_signal_number")),
                    RUNNER_OBSERVER_FAILURE_EXIT_CODE,
                }
            )
        )
    )
    return redact_value(
        {
            "ok": bool(valid and payload.get("ok") is True),
            "bounded": True,
            "observed": True,
            "spawned": payload.get("spawned") is True,
            "timed_out": payload.get("timed_out") is True,
            "interrupted": payload.get("interrupted") is True,
            "observer_signal": payload.get("observer_signal"),
            "observer_signal_number": payload.get("observer_signal_number"),
            "observer_signal_count": payload.get("observer_signal_count"),
            "interrupt_observation_enabled": payload.get("interrupt_observation_enabled") is True,
            "evidence_token_sha256": payload.get("evidence_token_sha256"),
            "schema_version": payload.get("schema_version"),
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
            "peak_rss_kib": payload.get("peak_rss_kib"),
            "rss_samples": payload.get("rss_samples"),
            "rss_sample_errors": payload.get("rss_sample_errors"),
            "observer_error": payload.get("observer_error") if isinstance(payload.get("observer_error"), dict) else None,
            "resources": {
                "before": payload.get("resource_before") if isinstance(payload.get("resource_before"), dict) else {},
                "after": payload.get("resource_after") if isinstance(payload.get("resource_after"), dict) else {},
            },
            "termination": payload.get("termination") if isinstance(payload.get("termination"), dict) else {},
            "output_total_bytes": payload.get("output_total_bytes"),
            "output_sha256": payload.get("output_sha256"),
            "policy": payload.get("policy") if isinstance(payload.get("policy"), dict) else {},
        }
    )


__all__ = [
    "RUNNER_OBSERVATION_SCHEMA_VERSION",
    "_bounded_command",
    "observe_command",
    "observation_path",
    "observation_status",
]
