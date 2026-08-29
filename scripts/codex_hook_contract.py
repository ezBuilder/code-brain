"""Canonical Code Brain Codex hook definitions and validation.

This module is intentionally stdlib-only because both the Bash installer's
embedded Python and the standalone trust helper import it before the managed
runtime exists.  Keep every Code Brain-owned hooks.json field here so install
and trust cannot drift across Codex version gates.
"""

from __future__ import annotations

from typing import Any


PROJECT_EVENTS = {
    "preToolUse": "PreToolUse",
    "permissionRequest": "PermissionRequest",
    "postToolUse": "PostToolUse",
    "preCompact": "PreCompact",
    "postCompact": "PostCompact",
    "sessionStart": "SessionStart",
    "userPromptSubmit": "UserPromptSubmit",
    "subagentStart": "SubagentStart",
    "subagentStop": "SubagentStop",
    "stop": "Stop",
    "sessionEnd": "SessionEnd",
    "interrupt": "Interrupt",
}

BASE_MANAGED_EVENTS = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PostCompact",
        "PermissionRequest",
    }
)
OPTIONAL_MANAGED_EVENTS = frozenset({"SessionEnd", "Interrupt"})
ALL_MANAGED_EVENTS = BASE_MANAGED_EVENTS | OPTIONAL_MANAGED_EVENTS
_CONTEXT_PRODUCING_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"}
)


class HookContractError(ValueError):
    """Raised when Code Brain-owned hooks do not match the canonical contract."""


def project_command(event: str) -> str:
    return 'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; "$ROOT/.ai/bin/ai-hook" ' + event


def project_windows_command(event: str) -> str:
    return (
        'powershell -NoProfile -Command "$ROOT = (git rev-parse --show-toplevel 2>$null); '
        'if (-not $ROOT) { $ROOT = (Get-Location).Path }; '
        '& \\"$ROOT/.ai/bin/ai-hook.ps1\\" ' + event + '"'
    )


def _entry(
    event: str,
    *,
    matcher: str | None = None,
    status_message: str,
    timeout: int,
    context_limit: int | None = None,
) -> list[dict[str, Any]]:
    handler: dict[str, Any] = {
        "type": "command",
        "command": project_command(event),
        "commandWindows": project_windows_command(event),
        "statusMessage": status_message,
        "timeout": timeout,
    }
    if context_limit is not None:
        if event not in _CONTEXT_PRODUCING_EVENTS:
            raise AssertionError(f"additionalContextLimit is invalid for {event}")
        handler["additionalContextLimit"] = context_limit
    group: dict[str, Any] = {"hooks": [handler]}
    if matcher is not None:
        group["matcher"] = matcher
    return [group]


def managed_codex_hooks(
    *, session_end_enabled: bool, interrupt_enabled: bool
) -> dict[str, list[dict[str, Any]]]:
    """Render the complete canonical Code Brain-owned hooks mapping."""

    hooks = {
        "PreToolUse": _entry(
            "PreToolUse",
            matcher="Bash|Shell|exec_command|functions.exec_command|apply_patch|Edit|Write|run_command",
            status_message="Checking Code Brain command routing",
            timeout=5,
        ),
        "PostToolUse": _entry(
            "PostToolUse",
            matcher=(
                "Bash|Shell|exec_command|functions.exec_command|apply_patch|Edit|Write|MultiEdit|"
                "NotebookEdit|Read|Glob|Grep|run_command|replace_file_content|"
                "multi_replace_file_content|write_to_file|view_file|grep_search|list_dir"
            ),
            status_message="Recording Code Brain tool result",
            timeout=2,
        ),
        "SessionStart": _entry(
            "SessionStart",
            matcher="startup|resume|clear|compact",
            status_message="Loading Code Brain session context",
            timeout=2,
            context_limit=5000,
        ),
        "UserPromptSubmit": _entry(
            "UserPromptSubmit",
            status_message="Loading Code Brain prompt context",
            timeout=5,
            context_limit=2500,
        ),
        "Stop": _entry(
            "Stop",
            status_message="Recording Code Brain stop event",
            timeout=5,
        ),
        "SubagentStart": _entry(
            "SubagentStart",
            status_message="Loading Code Brain subagent context",
            timeout=2,
            context_limit=5000,
        ),
        "SubagentStop": _entry(
            "SubagentStop",
            status_message="Recording Code Brain subagent stop",
            timeout=2,
        ),
        "PreCompact": _entry(
            "PreCompact",
            status_message="Saving Code Brain compact snapshot",
            timeout=2,
        ),
        "PostCompact": _entry(
            "PostCompact",
            status_message="Recording Code Brain compact completion",
            timeout=2,
        ),
        "PermissionRequest": _entry(
            "PermissionRequest",
            matcher="Bash|Shell|exec_command|functions.exec_command|run_command|ask_permission",
            status_message="Checking Code Brain approval policy",
            timeout=5,
        ),
    }
    if session_end_enabled:
        hooks["SessionEnd"] = _entry(
            "SessionEnd",
            matcher="other",
            status_message="Recording Code Brain session end",
            timeout=2,
        )
    if interrupt_enabled:
        hooks["Interrupt"] = _entry(
            "Interrupt",
            status_message="Recording Code Brain interrupt",
            timeout=2,
        )
    return hooks


def contains_code_brain_command(value: object) -> bool:
    """Return whether an entry contains either managed platform command."""

    if not isinstance(value, dict):
        return False
    for field in ("command", "commandWindows"):
        command = value.get(field)
        if isinstance(command, str) and (
            ".ai/bin/ai-hook" in command or ".ai/bin/ai-hook.ps1" in command
        ):
            return True
    children = value.get("hooks")
    return isinstance(children, list) and any(contains_code_brain_command(item) for item in children)


def validate_managed_hook_payload(payload: object) -> frozenset[str]:
    """Validate only managed entries while allowing unrelated foreign entries.

    The top-level Codex schema and every event list must still be structurally
    valid.  Each base Code Brain event must contain exactly one canonical
    managed group. SessionEnd and Interrupt may be absent for older CLIs, but
    are canonical when present. Foreign groups are ignored and remain subject
    to Codex's own independent hash trust.
    """

    if not isinstance(payload, dict) or set(payload) != {"hooks"}:
        raise HookContractError("hooks.json must contain only a top-level hooks object")
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        raise HookContractError("hooks.json hooks must be an object")

    present: set[str] = set()
    for event, entries in hooks.items():
        if not isinstance(event, str) or not isinstance(entries, list):
            raise HookContractError("every hook event must map to a list")
        managed_entries = [entry for entry in entries if contains_code_brain_command(entry)]
        if not managed_entries:
            continue
        if event not in ALL_MANAGED_EVENTS:
            raise HookContractError(f"managed command appears under unsupported event {event!r}")
        expected = managed_codex_hooks(
            session_end_enabled=event == "SessionEnd",
            interrupt_enabled=event == "Interrupt",
        )[event]
        if managed_entries != expected:
            raise HookContractError(f"managed {event} entry is missing, duplicated, or modified")
        present.add(event)

    missing = sorted(BASE_MANAGED_EVENTS - present)
    if missing:
        raise HookContractError(f"managed base events are missing: {', '.join(missing)}")
    return frozenset(present)
