from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .doctor import as_payload, run_checks
from .memory import (
    append_decision,
    append_event,
    append_session_note,
    append_todo,
    close_todo,
)
from .obs import health_summary, search_report, usage_report
from .policy import is_ci
from .redact import redact_value
from .sandbox import execute as sandbox_execute, fetch as sandbox_fetch, list_executions as sandbox_list
from .search import context_pack, query, rebuild
from .worker.ipc import health

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_NAME = "code-brain"

# Tool catalog. Each entry is exposed via tools/list and dispatched via tools/call.
# Description text is short; the inputSchema follows JSON Schema (draft 2020-12 compatible).
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "memory_query",
        "description": "BM25 search over indexed source. Returns top-K snippets with provenance.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "code_query",
        "description": "Alias of memory_query — BM25 code search.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "context_pack",
        "description": "BM25 query plus an additionalContext string suitable for hook injection.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "ai_status",
        "description": "Worker health envelope.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ai_request_rebuild",
        "description": "Force-rebuild the SQLite FTS5 code index. Write-class.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "obs_usage",
        "description": "Actual token usage from Claude/Codex transcripts plus measured Code Brain effect bytes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "obs_health_summary",
        "description": "Read-only roll-up: doctor checks, queue counts, worker lock, release artifacts, index state.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "obs_search",
        "description": "BM25 query with stale-detection report.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "doctor_strict",
        "description": "Run all doctor checks and return the full payload. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sandbox_execute",
        "description": "Run a shell command in Code Brain's sandbox. Returns short summary plus exec_id; full output stored on disk.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["command"],
        },
    },
    {
        "name": "sandbox_fetch",
        "description": "Fetch a line range or grep filter from a stored sandbox execution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "exec_id": {"type": "string"},
                "line_start": {"type": "integer", "default": 1},
                "line_end": {"type": "integer"},
                "grep_pattern": {"type": "string"},
            },
            "required": ["exec_id"],
        },
    },
    {
        "name": "sandbox_list",
        "description": "List recent sandbox executions (newest first).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "record_decision",
        "description": (
            "Persist a project decision to .ai/memory/decisions.jsonl. Call this whenever the "
            "user makes or confirms a meaningful project decision — architectural choice, scope "
            "change, dropped option, locked policy. Decisions auto-inject into next session's "
            "additionalContext via SessionStart hook. Keep `text` short (<200 chars), prefer one "
            "decision per call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "record_todo",
        "description": (
            "Persist an open todo to .ai/memory/todos.jsonl. Call this when the user mentions a "
            "future task that should be remembered across sessions, or you yourself defer work. "
            "Open todos auto-inject into next session's additionalContext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "owner": {"type": "string", "default": ""},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "close_todo",
        "description": "Mark an open todo as done/closed. `match` accepts the todo id or a unique substring of the title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "match": {"type": "string"},
                "status": {"type": "string", "enum": ["done", "closed", "cancelled", "canceled"], "default": "done"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["match"],
        },
    },
    {
        "name": "append_session_note",
        "description": (
            "Append a short, human-readable note to .ai/memory/session-current.md. Use for "
            "milestones the operator should see when resuming the session — completed tasks, "
            "discoveries, reminders. The hook injects the last lines on SessionStart."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
)

MCP_METHODS = tuple(tool["name"] for tool in TOOLS)
TOOL_NAMES = frozenset(MCP_METHODS)


def _dispatch_tool(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the underlying handler for a tool by name. Raises KeyError if unknown."""
    args = arguments or {}
    if name in ("memory_query", "code_query"):
        return query(root, str(args.get("query", "")), limit=int(args.get("limit", 5) or 5))
    if name == "context_pack":
        return context_pack(root, str(args.get("query", "")), limit=int(args.get("limit", 5) or 5))
    if name == "ai_status":
        return health(root)
    if name == "ai_request_rebuild":
        return rebuild(root)
    if name == "obs_usage":
        return usage_report(root)
    if name == "obs_health_summary":
        return health_summary(root)
    if name == "obs_search":
        return search_report(root, query_text=args.get("query"), limit=int(args.get("limit", 5) or 5))
    if name == "doctor_strict":
        return as_payload(run_checks(root))
    if name == "sandbox_execute":
        command = args.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("sandbox_execute requires non-empty command list")
        return sandbox_execute(
            root,
            command=[str(part) for part in command],
            cwd=str(args["cwd"]) if isinstance(args.get("cwd"), str) else None,
            timeout=int(args.get("timeout", 30) or 30),
        )
    if name == "sandbox_fetch":
        exec_id = args.get("exec_id")
        if not isinstance(exec_id, str) or not exec_id:
            raise ValueError("sandbox_fetch requires exec_id string")
        return sandbox_fetch(
            root,
            exec_id=exec_id,
            line_start=int(args.get("line_start", 1) or 1),
            line_end=(int(args["line_end"]) if isinstance(args.get("line_end"), int) else None),
            grep_pattern=(str(args["grep_pattern"]) if isinstance(args.get("grep_pattern"), str) else None),
        )
    if name == "sandbox_list":
        return sandbox_list(root, limit=int(args.get("limit", 20) or 20))
    if name == "record_decision":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("record_decision requires non-empty text")
        return append_decision(
            root,
            text=text,
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            source=str(args.get("source", "agent")),
        )
    if name == "record_todo":
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("record_todo requires non-empty title")
        return append_todo(
            root,
            title=title,
            owner=str(args.get("owner", "")),
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            source=str(args.get("source", "agent")),
        )
    if name == "close_todo":
        match = args.get("match")
        if not isinstance(match, str) or not match.strip():
            raise ValueError("close_todo requires match string")
        return close_todo(
            root,
            match=match,
            status=str(args.get("status", "done")),
            reason=str(args.get("reason", "")),
        )
    if name == "append_session_note":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("append_session_note requires non-empty text")
        return append_session_note(root, text=text)
    raise KeyError(name)


def _parse_prompt_md(text: str) -> tuple[str, str | None, str]:
    """Parse `.claude/commands/*.md` frontmatter -> (description, argument_hint, body)."""
    lines = text.splitlines()
    desc = ""
    arg_hint: str | None = None
    in_fm = False
    body_start = 0
    seen_fm_open = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---":
            if not seen_fm_open:
                seen_fm_open = True
                in_fm = True
                continue
            body_start = i + 1
            in_fm = False
            break
        if in_fm:
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip("\"").strip("'")
            elif line.startswith("argument-hint:"):
                arg_hint = line.split(":", 1)[1].strip().strip("\"").strip("'")
    body = "\n".join(lines[body_start:]).strip() if seen_fm_open else text.strip()
    return desc, arg_hint, body


def _list_prompts(root: Path) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    cmd_dir = root / ".claude" / "commands"
    if not cmd_dir.exists():
        return prompts
    for md in sorted(cmd_dir.glob("cb-*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        desc, arg_hint, _ = _parse_prompt_md(text)
        entry: dict[str, Any] = {"name": md.stem, "description": desc or md.stem}
        if arg_hint:
            entry["arguments"] = [{"name": "input", "description": arg_hint, "required": False}]
        prompts.append(entry)
    return prompts


def _get_prompt(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    md = root / ".claude" / "commands" / f"{name}.md"
    if not md.is_file():
        raise KeyError(name)
    text = md.read_text(encoding="utf-8")
    desc, _, body = _parse_prompt_md(text)
    args_value = ""
    if isinstance(arguments, dict):
        for key in ("input", "ARGUMENTS", "args"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                args_value = value
                break
    body = body.replace("$ARGUMENTS", args_value)
    return {
        "description": desc,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": body}},
        ],
    }


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(root: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    """Route a single JSON-RPC message. Returns None for notifications (no response).

    Supports both standard MCP protocol (initialize, tools/list, tools/call, etc.)
    and direct tool-name dispatch (legacy/internal callers like ai-mcp --once-json).
    """
    start = time.perf_counter()
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    is_notification = "id" not in request

    try:
        # Notifications — no response per JSON-RPC 2.0.
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": MCP_SERVER_NAME, "version": __version__},
            }
            response = _ok(request_id, result)
        elif method == "ping":
            response = _ok(request_id, {})
        elif method == "tools/list":
            response = _ok(request_id, {"tools": [dict(tool) for tool in TOOLS]})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if not isinstance(name, str) or name not in TOOL_NAMES:
                response = _err(request_id, -32602, f"unknown tool: {name!r}")
            else:
                try:
                    tool_result = _dispatch_tool(root, name, arguments or {})
                    response = _ok(
                        request_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(tool_result, ensure_ascii=False, sort_keys=True),
                                }
                            ],
                            "isError": not bool(tool_result.get("ok", True)) if isinstance(tool_result, dict) else False,
                            "structuredContent": tool_result if isinstance(tool_result, dict) else None,
                        },
                    )
                except Exception as exc:
                    response = _ok(
                        request_id,
                        {
                            "content": [{"type": "text", "text": f"error: {exc}"}],
                            "isError": True,
                        },
                    )
        elif method == "prompts/list":
            response = _ok(request_id, {"prompts": _list_prompts(root)})
        elif method == "prompts/get":
            prompt_name = params.get("name")
            prompt_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if not isinstance(prompt_name, str) or not prompt_name:
                response = _err(request_id, -32602, "prompts/get requires name")
            else:
                try:
                    response = _ok(request_id, _get_prompt(root, prompt_name, prompt_args or {}))
                except KeyError:
                    response = _err(request_id, -32602, f"unknown prompt: {prompt_name}")
                except Exception as exc:
                    response = _err(request_id, -32000, str(exc))
        elif method == "resources/list":
            response = _ok(request_id, {"resources": []})
        elif method == "resources/templates/list":
            response = _ok(request_id, {"resourceTemplates": []})
        elif isinstance(method, str) and method in TOOL_NAMES:
            # Legacy direct dispatch: e.g. {"method": "obs_usage", ...}
            result = _dispatch_tool(root, method, params if isinstance(params, dict) else {})
            response = _ok(request_id, result)
        else:
            response = _err(request_id, -32601, f"method not found: {method}")
    except Exception as exc:
        response = _err(request_id, -32000, str(exc))

    record_mcp_request(root, method, request, response, start, response.get("result") if isinstance(response, dict) else None)
    return None if is_notification else redact_value(response)


def record_mcp_request(
    root: Path,
    method: Any,
    request: dict[str, Any],
    response: dict[str, Any],
    start: float,
    result: Any,
) -> None:
    if is_ci():
        return
    try:
        append_event(
            root,
            {
                "hook": "mcp.request",
                "method": method,
                "elapsed_ms": int((time.perf_counter() - start) * 1000),
                "request_bytes": len(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")),
                "response_bytes": len(json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")),
                "results_count": len(result.get("results", [])) if isinstance(result, dict) else None,
            },
        )
    except Exception:
        # mcp.request audit is best-effort; never fail the JSON-RPC response on it.
        pass


def serve_stdio(root: Path) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps(_err(None, -32700, f"parse error: {exc}"), ensure_ascii=False, sort_keys=True), flush=True)
            continue
        response = handle_request(root, request)
        if response is None:
            continue
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
    return 0
