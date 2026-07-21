from __future__ import annotations

import json
import os
import hashlib
import heapq
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

CODEX_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


TRANSCRIPT_MAX_FILE_BYTES = _bounded_env_int(
    "AI_TRANSCRIPT_MAX_FILE_BYTES",
    64_000_000,
    minimum=1_000_000,
    maximum=2_000_000_000,
)
TRANSCRIPT_MAX_LINE_BYTES = _bounded_env_int(
    "AI_TRANSCRIPT_MAX_LINE_BYTES",
    2_000_000,
    minimum=64_000,
    maximum=64_000_000,
)
TRANSCRIPT_MAX_SCAN_BYTES = _bounded_env_int(
    "AI_TRANSCRIPT_MAX_SCAN_BYTES",
    256_000_000,
    minimum=4_000_000,
    maximum=8_000_000_000,
)
TRANSCRIPT_MAX_SESSIONS = _bounded_env_int(
    "AI_TRANSCRIPT_MAX_SESSIONS",
    1000,
    minimum=1,
    maximum=100_000,
)
TRANSCRIPT_MAX_CANDIDATES = _bounded_env_int(
    "AI_TRANSCRIPT_MAX_CANDIDATES",
    4000,
    minimum=1,
    maximum=200_000,
)
TRANSCRIPT_MAX_SCAN_SECONDS = _bounded_env_float(
    "AI_TRANSCRIPT_MAX_SCAN_SECONDS",
    8.0,
    minimum=0.1,
    maximum=300.0,
)
TRANSCRIPT_MAX_DEDUPE_KEYS = _bounded_env_int(
    "AI_TRANSCRIPT_MAX_DEDUPE_KEYS",
    100_000,
    minimum=100,
    maximum=2_000_000,
)
TRANSCRIPT_MAX_DIAGNOSTICS = 25


@dataclass(frozen=True)
class TranscriptCandidate:
    path: Path
    size: int
    mtime_ns: int
    device: int
    inode: int


@dataclass
class TranscriptFileScan:
    bytes_read: int = 0
    lines_read: int = 0
    invalid_lines: int = 0
    oversized_lines: int = 0
    omitted_bytes: int = 0
    complete: bool = True
    fatal: bool = False
    reason: str | None = None
    warning_counts: dict[str, int] = field(default_factory=dict)
    warning_bytes: dict[str, int] = field(default_factory=dict)

    def stop(self, reason: str) -> None:
        self.complete = False
        self.fatal = True
        if self.reason is None:
            self.reason = reason

    def warn(self, reason: str, *, omitted_bytes: int = 0) -> None:
        self.complete = False
        self.warning_counts[reason] = self.warning_counts.get(reason, 0) + 1
        bounded = max(0, int(omitted_bytes))
        self.warning_bytes[reason] = self.warning_bytes.get(reason, 0) + bounded
        self.omitted_bytes += bounded


@dataclass
class TranscriptScan:
    started: float
    deadline: float
    discovered: int = 0
    scanned: int = 0
    bytes_scanned: int = 0
    bytes_skipped: int = 0
    invalid_lines: int = 0
    partial_sessions: int = 0
    skip_counts: dict[str, int] = field(default_factory=dict)
    warning_counts: dict[str, int] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def skip(self, reason: str, *, path: Path | None = None, size: int = 0, count: int = 1) -> None:
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + max(1, int(count))
        self.bytes_skipped += max(0, int(size))
        if path is not None and len(self.skipped) < TRANSCRIPT_MAX_DIAGNOSTICS:
            self.skipped.append(
                {
                    "path": _display_path(path),
                    "reason": reason,
                    "bytes": max(0, int(size)),
                }
            )

    def warn(self, reason: str, *, path: Path, size: int = 0, count: int = 1) -> None:
        self.warning_counts[reason] = self.warning_counts.get(reason, 0) + max(1, int(count))
        self.bytes_skipped += max(0, int(size))
        if len(self.warnings) < TRANSCRIPT_MAX_DIAGNOSTICS:
            self.warnings.append(
                {
                    "path": _display_path(path),
                    "reason": reason,
                    "bytes": max(0, int(size)),
                }
            )

    def payload(self) -> dict[str, Any]:
        elapsed_ms = max(0, int((time.monotonic() - self.started) * 1000))
        complete = not self.skip_counts and not self.warning_counts
        return {
            "complete": complete,
            "partial": not complete,
            "sessions_discovered": self.discovered,
            "sessions_scanned": self.scanned,
            "sessions_skipped": sum(self.skip_counts.values()),
            "sessions_partial": self.partial_sessions,
            "bytes_scanned": self.bytes_scanned,
            "bytes_skipped": self.bytes_skipped,
            "invalid_lines": self.invalid_lines,
            "elapsed_ms": elapsed_ms,
            "skip_counts": dict(sorted(self.skip_counts.items())),
            "warning_counts": dict(sorted(self.warning_counts.items())),
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
            "policy": {
                "max_file_bytes": int(TRANSCRIPT_MAX_FILE_BYTES),
                "max_line_bytes": int(TRANSCRIPT_MAX_LINE_BYTES),
                "max_scan_bytes": int(TRANSCRIPT_MAX_SCAN_BYTES),
                "max_sessions": int(TRANSCRIPT_MAX_SESSIONS),
                "max_candidates": int(TRANSCRIPT_MAX_CANDIDATES),
                "max_scan_seconds": float(TRANSCRIPT_MAX_SCAN_SECONDS),
                "max_dedupe_keys": int(TRANSCRIPT_MAX_DEDUPE_KEYS),
            },
        }


def _display_path(path: Path) -> str:
    value = path.as_posix()
    home = str(Path.home())
    return value.replace(home, "~", 1) if value.startswith(home) else value


def _collect_latest_candidates(paths: Iterator[Path], scan: TranscriptScan) -> list[TranscriptCandidate]:
    limit = max(1, int(TRANSCRIPT_MAX_CANDIDATES))
    heap: list[tuple[int, str, TranscriptCandidate]] = []
    for path in paths:
        if time.monotonic() >= scan.deadline:
            scan.skip("discovery_time_limit")
            break
        scan.discovered += 1
        try:
            state = path.lstat()
        except OSError:
            scan.skip("stat_error", path=path)
            continue
        if stat.S_ISLNK(state.st_mode):
            scan.skip("unsafe_symlink", path=path, size=int(state.st_size))
            continue
        if not stat.S_ISREG(state.st_mode):
            scan.skip("not_regular", path=path, size=int(state.st_size))
            continue
        candidate = TranscriptCandidate(
            path=path,
            size=max(0, int(state.st_size)),
            mtime_ns=int(state.st_mtime_ns),
            device=int(state.st_dev),
            inode=int(state.st_ino),
        )
        item = (candidate.mtime_ns, candidate.path.as_posix(), candidate)
        if len(heap) < limit:
            heapq.heappush(heap, item)
            continue
        if item[:2] > heap[0][:2]:
            _mtime, _name, dropped = heapq.heapreplace(heap, item)
            scan.skip("candidate_limit", path=dropped.path, size=dropped.size)
        else:
            scan.skip("candidate_limit", path=candidate.path, size=candidate.size)
    return [item[2] for item in sorted(heap, reverse=True)]


@dataclass
class SessionUsage:
    agent: str
    session_id: str
    source_path: str
    source: str = "claude_transcript"
    cwd: str | None = None
    model: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    messages: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {name: 0 for name in TOKEN_FIELDS})

    @property
    def total_observed_tokens(self) -> int:
        return sum(self.tokens.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "source": self.source,
            "source_path": self.source_path,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "model": self.model,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "messages": self.messages,
            "tokens": dict(self.tokens),
            "total_observed_tokens": self.total_observed_tokens,
        }


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser()


def enumerate_claude_sessions(home: Path | None = None) -> Iterator[Path]:
    projects = (home or claude_home()) / "projects"
    if not projects.exists():
        return
    # Candidate ordering is handled by a fixed-size newest-first heap. Avoid
    # sorted(...) here because it materializes every transcript path in RAM.
    yield from projects.glob("*/*.jsonl")


def _iter_jsonl_records(
    path: Path,
    *,
    scan: TranscriptFileScan | None = None,
    deadline: float | None = None,
    max_bytes: int | None = None,
    expected: TranscriptCandidate | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a .jsonl transcript one line at a time.

    A whole-file ``read_text().splitlines()`` materialises the entire transcript
    (plus a list of every line) in RAM. For multi-GB agent logs that spikes RSS to
    several times the file size, and because CPython/malloc retain freed arenas the
    high-water mark accumulates across files — a status scan over many large Codex
    rollouts drove a single process past 20 GB. Streaming bounds memory to one line.
    """
    state = scan or TranscriptFileScan()
    byte_limit = max(1, int(max_bytes if max_bytes is not None else TRANSCRIPT_MAX_FILE_BYTES))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        state.stop("open_error")
        return
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            state.stop("not_regular")
            return
        if expected is not None and (
            int(opened.st_dev) != expected.device or int(opened.st_ino) != expected.inode
        ):
            state.stop("changed_before_read")
            return
        with os.fdopen(descriptor, "rb") as fh:
            descriptor = -1
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    state.stop("scan_time_limit")
                    return
                remaining = byte_limit - state.bytes_read
                if remaining <= 0:
                    if fh.peek(1) if hasattr(fh, "peek") else True:
                        state.stop("file_byte_limit")
                    return
                read_limit = min(max(1, int(TRANSCRIPT_MAX_LINE_BYTES)) + 1, remaining + 1)
                line = fh.readline(read_limit)
                if not line:
                    break
                state.bytes_read += len(line)
                state.lines_read += 1
                if state.bytes_read > byte_limit:
                    state.stop("file_byte_limit")
                    return
                if len(line) > TRANSCRIPT_MAX_LINE_BYTES and not line.endswith(b"\n"):
                    state.oversized_lines += 1
                    omitted = len(line)
                    while line and not line.endswith(b"\n"):
                        if deadline is not None and time.monotonic() >= deadline:
                            state.stop("scan_time_limit")
                            return
                        remaining = byte_limit - state.bytes_read
                        if remaining <= 0:
                            state.stop("file_byte_limit")
                            return
                        line = fh.readline(min(64_000, remaining))
                        state.bytes_read += len(line)
                        omitted += len(line)
                    state.warn("line_byte_limit", omitted_bytes=omitted)
                    continue
                if not line.strip():
                    continue
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    state.invalid_lines += 1
                    state.warn("invalid_json_line", omitted_bytes=len(line))
                    continue
                if isinstance(record, dict):
                    yield record
            try:
                final_state = os.fstat(fh.fileno())
            except OSError:
                state.stop("final_stat_error")
                return
            if (
                int(final_state.st_size) != int(opened.st_size)
                or int(final_state.st_mtime_ns) != int(opened.st_mtime_ns)
            ):
                state.stop("changed_during_read")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def parse_claude_session(
    path: Path,
    *,
    display_path: str | None = None,
    scan: TranscriptFileScan | None = None,
    deadline: float | None = None,
    max_bytes: int | None = None,
    expected: TranscriptCandidate | None = None,
) -> SessionUsage | None:
    file_scan = scan or TranscriptFileScan()
    usage: SessionUsage | None = None
    seen_requests: set[bytes] = set()
    for record in _iter_jsonl_records(
        path,
        scan=file_scan,
        deadline=deadline,
        max_bytes=max_bytes,
        expected=expected,
    ):
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        message_usage = message.get("usage")
        if not isinstance(message_usage, dict):
            continue
        request_key = str(record.get("requestId") or message.get("id") or record.get("uuid") or "")
        if request_key:
            digest = hashlib.blake2b(request_key.encode("utf-8", errors="replace"), digest_size=16).digest()
            if digest in seen_requests:
                continue
            if len(seen_requests) >= TRANSCRIPT_MAX_DEDUPE_KEYS:
                file_scan.stop("dedupe_key_limit")
                break
            seen_requests.add(digest)
        session_id = str(record.get("sessionId") or path.stem)
        if usage is None:
            usage = SessionUsage(
                agent="claude",
                session_id=session_id,
                source_path=display_path or str(path),
                cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                model=message.get("model") if isinstance(message.get("model"), str) else None,
                first_timestamp=record.get("timestamp") if isinstance(record.get("timestamp"), str) else None,
            )
        usage.messages += 1
        if isinstance(record.get("timestamp"), str):
            usage.last_timestamp = record["timestamp"]
        if isinstance(message.get("model"), str):
            usage.model = message["model"]
        for field_name in TOKEN_FIELDS:
            value = message_usage.get(field_name, 0)
            if isinstance(value, int):
                usage.tokens[field_name] += value
    return usage if not file_scan.fatal else None


def _summary_scan_base(paths: Iterator[Path]) -> tuple[TranscriptScan, list[TranscriptCandidate]]:
    started = time.monotonic()
    scan = TranscriptScan(started=started, deadline=started + float(TRANSCRIPT_MAX_SCAN_SECONDS))
    candidates = _collect_latest_candidates(paths, scan)
    return scan, candidates


def _scan_candidate(
    candidate: TranscriptCandidate,
    scan: TranscriptScan,
    parser: Any,
) -> Any | None:
    if time.monotonic() >= scan.deadline:
        scan.skip("scan_time_limit", path=candidate.path, size=candidate.size)
        return None
    if candidate.size > TRANSCRIPT_MAX_FILE_BYTES:
        scan.skip("file_too_large", path=candidate.path, size=candidate.size)
        return None
    remaining = int(TRANSCRIPT_MAX_SCAN_BYTES) - scan.bytes_scanned
    if remaining <= 0 or candidate.size > remaining:
        scan.skip("aggregate_byte_limit", path=candidate.path, size=candidate.size)
        return None
    file_scan = TranscriptFileScan()
    result = parser(
        candidate.path,
        display_path=_display_path(candidate.path),
        scan=file_scan,
        deadline=scan.deadline,
        max_bytes=min(int(TRANSCRIPT_MAX_FILE_BYTES), remaining),
        expected=candidate,
    )
    scan.bytes_scanned += file_scan.bytes_read
    scan.invalid_lines += file_scan.invalid_lines
    if file_scan.fatal:
        scan.skip(file_scan.reason or "incomplete_file", path=candidate.path, size=candidate.size)
        return None
    scan.scanned += 1
    if file_scan.warning_counts:
        scan.partial_sessions += 1
        for reason, count in file_scan.warning_counts.items():
            scan.warn(
                reason,
                path=candidate.path,
                size=file_scan.warning_bytes.get(reason, 0),
                count=count,
            )
    return result


def claude_usage_summary(root: Path, *, home: Path | None = None) -> dict[str, Any]:
    sessions: list[SessionUsage] = []
    scan, candidates = _summary_scan_base(enumerate_claude_sessions(home))
    for index, candidate in enumerate(candidates):
        if index >= TRANSCRIPT_MAX_SESSIONS:
            remaining = candidates[index:]
            scan.skip(
                "session_limit",
                size=sum(item.size for item in remaining),
                count=len(remaining),
            )
            break
        session = _scan_candidate(candidate, scan, parse_claude_session)
        if session is None:
            continue
        if session.cwd and not _cwd_matches_root(session.cwd, root):
            continue
        sessions.append(session)
    totals = {name: sum(session.tokens[name] for session in sessions) for name in TOKEN_FIELDS}
    scan_payload = scan.payload()
    return {
        "ok": True,
        "source": "claude_transcript",
        "home": str(home or claude_home()).replace(str(Path.home()), "~", 1),
        "project_root": str(root),
        "sessions_scanned": scan.scanned,
        "sessions_matched": len(sessions),
        "messages": sum(session.messages for session in sessions),
        "tokens": totals,
        "total_observed_tokens": sum(totals.values()),
        "sessions": [session.as_dict() for session in sorted(sessions, key=lambda item: item.last_timestamp or "")],
        "complete": scan_payload["complete"],
        "partial": scan_payload["partial"],
        "scan": scan_payload,
    }


@dataclass
class CodexSessionUsage:
    agent: str
    session_id: str
    source_path: str
    source: str = "codex_transcript"
    cwd: str | None = None
    model_provider: str | None = None
    cli_version: str | None = None
    originator: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    user_messages: int = 0
    agent_messages: int = 0
    turns: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {name: 0 for name in CODEX_TOKEN_FIELDS})

    @property
    def total_observed_tokens(self) -> int:
        return int(self.tokens.get("total_tokens", 0)) or sum(
            int(self.tokens.get(name, 0)) for name in ("input_tokens", "output_tokens")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "source": self.source,
            "source_path": self.source_path,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "model_provider": self.model_provider,
            "cli_version": self.cli_version,
            "originator": self.originator,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "user_messages": self.user_messages,
            "agent_messages": self.agent_messages,
            "turns": self.turns,
            "tokens": dict(self.tokens),
            "total_observed_tokens": self.total_observed_tokens,
        }


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def enumerate_codex_sessions(home: Path | None = None) -> Iterator[Path]:
    base = home or codex_home()
    for sub in ("sessions", "archived_sessions"):
        root = base / sub
        if not root.exists():
            continue
        yield from root.rglob("rollout-*.jsonl")


def parse_codex_session(
    path: Path,
    *,
    display_path: str | None = None,
    scan: TranscriptFileScan | None = None,
    deadline: float | None = None,
    max_bytes: int | None = None,
    expected: TranscriptCandidate | None = None,
) -> CodexSessionUsage | None:
    file_scan = scan or TranscriptFileScan()
    usage = CodexSessionUsage(
        agent="codex",
        session_id=path.stem.removeprefix("rollout-"),
        source_path=display_path or str(path),
    )
    seen_meta = False
    last_total: dict[str, int] | None = None
    last_total_ts: str | None = None
    for record in _iter_jsonl_records(
        path,
        scan=file_scan,
        deadline=deadline,
        max_bytes=max_bytes,
        expected=expected,
    ):
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        if ts:
            if usage.first_timestamp is None:
                usage.first_timestamp = ts
            usage.last_timestamp = ts
        kind = record.get("type")
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if kind == "session_meta" and not seen_meta:
            seen_meta = True
            cwd = payload.get("cwd")
            usage.cwd = cwd if isinstance(cwd, str) else None
            usage.model_provider = payload.get("model_provider") if isinstance(payload.get("model_provider"), str) else None
            usage.cli_version = payload.get("cli_version") if isinstance(payload.get("cli_version"), str) else None
            usage.originator = payload.get("originator") if isinstance(payload.get("originator"), str) else None
            inner_id = payload.get("id")
            if isinstance(inner_id, str) and inner_id:
                usage.session_id = inner_id
        if kind == "event_msg":
            event_type = payload.get("type")
            if event_type == "user_message":
                usage.user_messages += 1
            elif event_type == "agent_message":
                usage.agent_messages += 1
            elif event_type == "task_started":
                usage.turns += 1
            elif event_type == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    total = info.get("total_token_usage")
                    if isinstance(total, dict):
                        last_total = {
                            name: int(total.get(name, 0) or 0) for name in CODEX_TOKEN_FIELDS
                        }
                        last_total_ts = ts or last_total_ts
    if last_total is not None:
        usage.tokens = last_total
        if last_total_ts:
            usage.last_timestamp = last_total_ts
    if not file_scan.complete:
        return None
    if usage.user_messages == 0 and usage.agent_messages == 0 and last_total is None:
        return None
    return usage


def codex_usage_summary(root: Path, *, home: Path | None = None) -> dict[str, Any]:
    base_home = home or codex_home()
    if not base_home.exists():
        return {
            "ok": True,
            "source": "codex_transcript_unavailable",
            "home": str(base_home).replace(str(Path.home()), "~", 1),
            "project_root": str(root),
            "sessions_scanned": 0,
            "sessions_matched": 0,
            "user_messages": 0,
            "agent_messages": 0,
            "turns": 0,
            "tokens": {name: 0 for name in CODEX_TOKEN_FIELDS},
            "total_observed_tokens": 0,
            "sessions": [],
            "complete": True,
            "partial": False,
            "scan": {
                "complete": True,
                "partial": False,
                "sessions_discovered": 0,
                "sessions_scanned": 0,
                "sessions_skipped": 0,
                "sessions_partial": 0,
                "bytes_scanned": 0,
                "bytes_skipped": 0,
                "invalid_lines": 0,
                "elapsed_ms": 0,
                "skip_counts": {},
                "warning_counts": {},
                "skipped": [],
                "warnings": [],
                "policy": {
                    "max_file_bytes": int(TRANSCRIPT_MAX_FILE_BYTES),
                    "max_line_bytes": int(TRANSCRIPT_MAX_LINE_BYTES),
                    "max_scan_bytes": int(TRANSCRIPT_MAX_SCAN_BYTES),
                    "max_sessions": int(TRANSCRIPT_MAX_SESSIONS),
                    "max_candidates": int(TRANSCRIPT_MAX_CANDIDATES),
                    "max_scan_seconds": float(TRANSCRIPT_MAX_SCAN_SECONDS),
                    "max_dedupe_keys": int(TRANSCRIPT_MAX_DEDUPE_KEYS),
                },
            },
        }
    sessions: list[CodexSessionUsage] = []
    scan, candidates = _summary_scan_base(enumerate_codex_sessions(base_home))
    for index, candidate in enumerate(candidates):
        if index >= TRANSCRIPT_MAX_SESSIONS:
            remaining = candidates[index:]
            scan.skip(
                "session_limit",
                size=sum(item.size for item in remaining),
                count=len(remaining),
            )
            break
        session = _scan_candidate(candidate, scan, parse_codex_session)
        if session is None:
            continue
        if session.cwd and not _cwd_matches_root(session.cwd, root):
            continue
        sessions.append(session)
    totals = {name: sum(int(session.tokens.get(name, 0)) for session in sessions) for name in CODEX_TOKEN_FIELDS}
    scan_payload = scan.payload()
    return {
        "ok": True,
        "source": "codex_transcript",
        "home": str(base_home).replace(str(Path.home()), "~", 1),
        "project_root": str(root),
        "sessions_scanned": scan.scanned,
        "sessions_matched": len(sessions),
        "user_messages": sum(session.user_messages for session in sessions),
        "agent_messages": sum(session.agent_messages for session in sessions),
        "turns": sum(session.turns for session in sessions),
        "tokens": totals,
        "total_observed_tokens": sum(totals.values()),
        "sessions": [session.as_dict() for session in sorted(sessions, key=lambda item: item.last_timestamp or "")],
        "complete": scan_payload["complete"],
        "partial": scan_payload["partial"],
        "scan": scan_payload,
    }


def _cwd_matches_root(cwd: str, root: Path) -> bool:
    try:
        cwd_path = Path(cwd).resolve()
        root_path = root.resolve()
    except OSError:
        return False
    return cwd_path == root_path or root_path in cwd_path.parents
