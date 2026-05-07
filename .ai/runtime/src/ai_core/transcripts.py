from __future__ import annotations

import json
import os
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
    yield from sorted(projects.glob("*/*.jsonl"))


def parse_claude_session(path: Path, *, display_path: str | None = None) -> SessionUsage | None:
    usage: SessionUsage | None = None
    seen_requests: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        message_usage = message.get("usage")
        if not isinstance(message_usage, dict):
            continue
        request_key = str(record.get("requestId") or message.get("id") or record.get("uuid") or "")
        if request_key and request_key in seen_requests:
            continue
        if request_key:
            seen_requests.add(request_key)
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
    return usage


def claude_usage_summary(root: Path, *, home: Path | None = None) -> dict[str, Any]:
    sessions: list[SessionUsage] = []
    scanned = 0
    for path in enumerate_claude_sessions(home):
        scanned += 1
        display_path = path.as_posix().replace(str(Path.home()), "~", 1)
        session = parse_claude_session(path, display_path=display_path)
        if session is None:
            continue
        if session.cwd and not _cwd_matches_root(session.cwd, root):
            continue
        sessions.append(session)
    totals = {name: sum(session.tokens[name] for session in sessions) for name in TOKEN_FIELDS}
    return {
        "ok": True,
        "source": "claude_transcript",
        "home": str(home or claude_home()).replace(str(Path.home()), "~", 1),
        "project_root": str(root),
        "sessions_scanned": scanned,
        "sessions_matched": len(sessions),
        "messages": sum(session.messages for session in sessions),
        "tokens": totals,
        "total_observed_tokens": sum(totals.values()),
        "sessions": [session.as_dict() for session in sorted(sessions, key=lambda item: item.last_timestamp or "")],
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
        yield from sorted(p for p in root.rglob("rollout-*.jsonl") if p.is_file())


def parse_codex_session(path: Path, *, display_path: str | None = None) -> CodexSessionUsage | None:
    usage = CodexSessionUsage(
        agent="codex",
        session_id=path.stem.removeprefix("rollout-"),
        source_path=display_path or str(path),
    )
    seen_meta = False
    last_total: dict[str, int] | None = None
    last_total_ts: str | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
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
        }
    sessions: list[CodexSessionUsage] = []
    scanned = 0
    for path in enumerate_codex_sessions(base_home):
        scanned += 1
        display_path = path.as_posix().replace(str(Path.home()), "~", 1)
        session = parse_codex_session(path, display_path=display_path)
        if session is None:
            continue
        if session.cwd and not _cwd_matches_root(session.cwd, root):
            continue
        sessions.append(session)
    totals = {name: sum(int(session.tokens.get(name, 0)) for session in sessions) for name in CODEX_TOKEN_FIELDS}
    return {
        "ok": True,
        "source": "codex_transcript",
        "home": str(base_home).replace(str(Path.home()), "~", 1),
        "project_root": str(root),
        "sessions_scanned": scanned,
        "sessions_matched": len(sessions),
        "user_messages": sum(session.user_messages for session in sessions),
        "agent_messages": sum(session.agent_messages for session in sessions),
        "turns": sum(session.turns for session in sessions),
        "tokens": totals,
        "total_observed_tokens": sum(totals.values()),
        "sessions": [session.as_dict() for session in sorted(sessions, key=lambda item: item.last_timestamp or "")],
    }


def _cwd_matches_root(cwd: str, root: Path) -> bool:
    try:
        cwd_path = Path(cwd).resolve()
        root_path = root.resolve()
    except OSError:
        return False
    return cwd_path == root_path or root_path in cwd_path.parents
