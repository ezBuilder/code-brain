from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .memory import append_event
from .policy import is_ci
from .redact import redact_value

HOT_PATH_TARGET_MS = 200
INJECTION_HOOKS = {"SessionStart", "UserPromptSubmit"}
MAX_INJECTION_BYTES = 4096
DECISIONS_TAIL = 5
TODOS_LIMIT = 5
SESSION_TAIL_LINES = 8


def read_payload(stdin: str | None = None) -> dict[str, Any]:
    raw = stdin if stdin is not None else sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def handle_hook(root: Path, hook_name: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    effective_hook = hook_name or payload.get("hook") or payload.get("event") or "unknown"

    precall_decision: dict[str, Any] | None = None
    if effective_hook == "PreToolUse":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
        raw_input = payload.get("tool_input")
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        try:
            from .precall import evaluate as precall_evaluate

            precall_decision = precall_evaluate(tool_name, tool_input)
        except Exception:
            precall_decision = None

    additional_context = build_context(effective_hook, payload, root=root)
    if (
        effective_hook == "PreToolUse"
        and precall_decision
        and precall_decision.get("action") == "block"
    ):
        deny_reason = (
            f"Code Brain auto-routing: {precall_decision.get('reason')}. "
            f"Use this instead: {precall_decision.get('suggestion')}."
        )
        additional_context = f"{deny_reason}\n\n{additional_context}" if additional_context else deny_reason
    additional_context_bytes = len(additional_context.encode("utf-8"))
    event = {"hook": effective_hook, "additional_context_bytes": additional_context_bytes, **payload}
    if precall_decision:
        event["precall"] = {
            "action": precall_decision.get("action"),
            "reason": precall_decision.get("reason"),
            "binary": precall_decision.get("binary"),
        }
        if precall_decision.get("action") == "block":
            event["decision"] = "block"
    if is_ci() or payload.get("dry") is True:
        mode = "ci-fast-path" if is_ci() else "local-dry-fast-path"
        persisted = False
    else:
        append_event(root, event)
        mode = "local-append"
        persisted = True
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response = {
        "ok": True,
        "hook": effective_hook,
        "mode": mode,
        "persisted": persisted,
        "elapsed_ms": elapsed_ms,
        "target_ms": HOT_PATH_TARGET_MS,
        "additional_context_bytes": additional_context_bytes,
        "additionalContext": additional_context,
    }
    if precall_decision:
        response["precall"] = precall_decision
        if precall_decision.get("action") == "block":
            response["decision"] = "block"
            response["reason"] = (
                f"Code Brain auto-routing: {precall_decision.get('reason')}. "
                f"Use this instead: {precall_decision.get('suggestion')}. "
                "Or call MCP `mcp__code-brain__sandbox_execute` directly. "
                "Code Brain stores full output in .ai/cache/sandbox/<exec_id>.txt and returns a short summary "
                "(first 30 + last 5 lines, total under 4 KB) to keep your context window small."
            )
    return redact_value(response)


def build_context(hook_name: str, payload: dict[str, Any], *, root: Path | None = None) -> str:
    agent = payload.get("agent", "unknown")
    writes = "off" if is_ci() or payload.get("dry") is True else "worker-local"
    header = f"Code Brain fast_path: hook={hook_name}, agent={agent}, network=off, writes={writes}."
    if hook_name not in INJECTION_HOOKS or root is None:
        return header
    sections = [header]
    routing = (
        "Search routing: prefer MCP `code_query` / `context_pack` over Bash grep/find. "
        "Each MCP query returns ranked snippets (default 5) instead of full grep dumps — "
        "use grep only as fallback when MCP fails."
    )
    sections.append(routing)
    if hook_name == "SessionStart":
        try:
            from .session_resume import read_latest_snapshot
            current_sid = str(payload.get("session_id") or payload.get("sid") or "")
            prior = read_latest_snapshot(root, exclude_session_id=current_sid or None)
        except Exception:
            prior = None
        if prior:
            lines = [f"Prior session resume (session_id={prior.get('session_id')}, written_at={prior.get('written_at')}):"]
            for entry in (prior.get("decisions_tail") or [])[-3:]:
                text = str(entry.get("decision") or entry.get("summary") or entry.get("text") or "")[:160]
                if text:
                    lines.append(f"  decision: {text}")
            for entry in (prior.get("todos_open") or [])[-3:]:
                text = str(entry.get("title") or entry.get("text") or entry.get("summary") or "")[:160]
                if text:
                    lines.append(f"  open todo: {text}")
            actions = prior.get("audit_tail_actions") or []
            if actions:
                lines.append(f"  recent actions: {', '.join(str(a) for a in actions[-5:])}")
            sections.append("\n".join(lines))
    decisions = _read_jsonl_tail(root / ".ai" / "memory" / "decisions.jsonl", DECISIONS_TAIL)
    if decisions:
        lines = ["Recent decisions:"]
        for entry in decisions:
            ts = str(entry.get("decided_at") or entry.get("timestamp") or "")[:19]
            text = str(entry.get("decision") or entry.get("summary") or entry.get("text") or "")[:160]
            lines.append(f"  - [{ts}] {text}" if ts else f"  - {text}")
        sections.append("\n".join(lines))
    todos = _read_jsonl_open_todos(root / ".ai" / "memory" / "todos.jsonl", TODOS_LIMIT)
    if todos:
        lines = ["Open todos:"]
        for entry in todos:
            text = str(entry.get("title") or entry.get("text") or entry.get("summary") or "")[:160]
            owner = str(entry.get("owner") or "")
            lines.append(f"  - {text} [{owner}]" if owner else f"  - {text}")
        sections.append("\n".join(lines))
    session_tail = _read_text_tail(root / ".ai" / "memory" / "session-current.md", SESSION_TAIL_LINES)
    if session_tail:
        sections.append("Session-current tail:\n" + session_tail)
    composed = "\n\n".join(sections)
    if len(composed.encode("utf-8")) > MAX_INJECTION_BYTES:
        truncated = composed.encode("utf-8")[: MAX_INJECTION_BYTES - 3].decode("utf-8", errors="ignore") + "..."
        composed = truncated
    return composed


def _read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-(limit * 4):]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out[-limit:]


def _read_jsonl_open_todos(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    open_items: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or entry.get("state") or "open").lower()
        if status in {"done", "closed", "completed", "cancelled", "canceled"}:
            continue
        open_items.append(entry)
        if len(open_items) >= limit:
            break
    return open_items


def _read_text_tail(path: Path, lines: int) -> str:
    if not path.exists() or lines <= 0:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    tail = text.rstrip().splitlines()[-lines:]
    return "\n".join(tail)
