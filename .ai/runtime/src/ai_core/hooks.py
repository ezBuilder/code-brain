from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .memory import (
    append_event,
    read_jsonl_open_todos as _read_jsonl_open_todos,
    read_jsonl_tail as _read_jsonl_tail,
    read_text_tail as _read_text_tail,
)
from .policy import is_ci
from .redact import redact_value

import os as _os

HOT_PATH_TARGET_MS = 200
INJECTION_HOOKS = {"SessionStart", "UserPromptSubmit"}
AUTO_REBUILD_HOOKS = {"Stop", "SubagentStop", "PostToolUse"}
CONTEXT_INJECTION_HOOKS = {"UserPromptSubmit", "SessionStart", "PreToolUse", "PostToolUse"}
SKILL_RECOMMENDATION_HOOKS = {"SessionStart"}
try:
    MAX_INJECTION_BYTES = max(256, min(8192, int(_os.environ.get("AI_INJECTION_MAX_BYTES", "4096"))))
except (ValueError, TypeError):
    MAX_INJECTION_BYTES = 4096
DECISIONS_TAIL = 5
TODOS_LIMIT = 5
SESSION_TAIL_LINES = 8
DELTA_NOTICE = "Code Brain context unchanged since last injection (delta-skipped)."
SKILL_RECOMMENDATION_DISABLE_VALUES = {"0", "false", "no", "off"}


def _injection_marker_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "last_injection.sha"


def _maybe_apply_delta(root: Path, hook_name: str, full_context: str) -> tuple[str, bool, int]:
    """For UserPromptSubmit only, replace identical repeat injections with a tiny notice.

    Returns (effective_context, delta_skipped, original_bytes).
    SessionStart always sends full context (start of session is the high-value moment).
    """
    if hook_name != "UserPromptSubmit":
        return full_context, False, len(full_context.encode("utf-8"))
    import hashlib
    sha = hashlib.sha256(full_context.encode("utf-8")).hexdigest()
    marker = _injection_marker_path(root)
    prev = ""
    if marker.exists():
        try:
            prev = marker.read_text(encoding="utf-8").strip()
        except OSError:
            prev = ""
    original_bytes = len(full_context.encode("utf-8"))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(sha, encoding="utf-8")
    except OSError:
        pass
    if prev == sha and prev:
        return DELTA_NOTICE, True, original_bytes
    return full_context, False, original_bytes


def _spawn_background_rebuild(root: Path) -> None:
    import os
    import subprocess

    from .portable import IS_WINDOWS, detached_popen_kwargs

    ai_bin_unix = root / ".ai" / "bin" / "ai"
    ai_bin_ps = root / ".ai" / "bin" / "ai.ps1"
    if IS_WINDOWS and ai_bin_ps.exists():
        cmd = ["powershell", "-NoProfile", "-File", str(ai_bin_ps), "index", "rebuild", "--single-flight", "--json"]
    elif ai_bin_unix.exists():
        cmd = [str(ai_bin_unix), "index", "rebuild", "--single-flight", "--json"]
    else:
        return
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                cmd,
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(root),
                **detached_popen_kwargs(),
            )
    except Exception:
        pass


def _skill_recommendation_context(root: Path, hook_name: str, payload: dict[str, Any]) -> str:
    if hook_name not in SKILL_RECOMMENDATION_HOOKS:
        return ""
    if _os.environ.get("AI_SKILL_RECOMMENDATIONS", "1").lower() in SKILL_RECOMMENDATION_DISABLE_VALUES:
        return ""
    try:
        min_signal = int(_os.environ.get("AI_SKILL_RECOMMEND_MIN_SIGNAL", "3"))
    except (TypeError, ValueError):
        min_signal = 3
    persist = not (is_ci() or payload.get("dry") is True)
    try:
        from .recommend import recommend

        result = recommend(
            root,
            limit=3,
            include_global=False,
            min_signal=min_signal,
            persist=persist,
        )
    except Exception:
        return ""
    candidates = result.get("candidates") if isinstance(result, dict) else []
    if not isinstance(candidates, list) or not candidates:
        return ""
    lines = [
        "Skill recommendations available. Surface these to the user; install only after explicit approval:",
    ]
    for cand in candidates[:3]:
        if not isinstance(cand, dict):
            continue
        cid = str(cand.get("id") or "")
        slug = str(cand.get("slug") or "")
        desc = str(cand.get("description") or "")[:120]
        if cid and slug:
            lines.append(f"  - {cid} | {slug}: {desc}")
    lines.append("Approval: `ai recommend skills accept <id>`; reject noise with `ai recommend skills reject <id>`.")
    return "\n".join(lines) if len(lines) > 2 else ""


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

            extra_rules: list[dict[str, Any]] = []
            try:
                from .precall_recommend import load_active_rules

                extra_rules = load_active_rules(root)
            except Exception:
                extra_rules = []
            precall_decision = precall_evaluate(tool_name, tool_input, extra_rules=extra_rules)
            if precall_decision and precall_decision.get("action") == "observe":
                rid = precall_decision.get("rule_id")
                if rid:
                    try:
                        from .precall_recommend import record_dry_run_observation

                        record_dry_run_observation(root, str(rid))
                    except Exception:
                        pass
            elif (
                precall_decision
                and precall_decision.get("action") == "block"
                and precall_decision.get("rule_id")
            ):
                rid = str(precall_decision.get("rule_id"))
                try:
                    from .precall_recommend import record_user_override

                    record_user_override(root, rid, str(tool_input.get("command") or ""))
                except Exception:
                    pass
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
    additional_context, delta_skipped, original_context_bytes = _maybe_apply_delta(
        root, effective_hook, additional_context
    )
    additional_context_bytes = len(additional_context.encode("utf-8"))
    event = {
        "hook": effective_hook,
        "additional_context_bytes": additional_context_bytes,
        "original_context_bytes": original_context_bytes,
        "delta_skipped": delta_skipped,
        **payload,
    }
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
        if effective_hook in AUTO_REBUILD_HOOKS:
            _spawn_background_rebuild(root)
        try:
            _handle_lifecycle_event(root, effective_hook, payload)
        except Exception:
            pass
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response = {
        "ok": True,
        "hook": effective_hook,
        "mode": mode,
        "persisted": persisted,
        "elapsed_ms": elapsed_ms,
        "target_ms": HOT_PATH_TARGET_MS,
        "additional_context_bytes": additional_context_bytes,
    }
    if effective_hook in CONTEXT_INJECTION_HOOKS:
        response["additionalContext"] = additional_context
        response["hookSpecificOutput"] = {
            "hookEventName": effective_hook,
            "additionalContext": additional_context,
        }
    if precall_decision:
        response["precall"] = precall_decision
        if precall_decision.get("action") == "block":
            import os
            rewrite_mode = os.environ.get("AI_PRECALL_REWRITE", "").lower() in ("1", "true", "yes")
            suggestion = str(precall_decision.get("suggestion") or "")
            if rewrite_mode and suggestion.startswith(".ai/bin/ai exec run --"):
                response["hookSpecificOutput"] = {
                    "hookEventName": effective_hook,
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        f"Code Brain auto-rewrite: {precall_decision.get('reason')} → routed to sandbox."
                    ),
                    "updatedInput": {"command": suggestion},
                    "additionalContext": additional_context,
                }
                response["rewritten"] = True
            else:
                response["decision"] = "block"
                response["hookSpecificOutput"] = {
                    "hookEventName": effective_hook,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Code Brain auto-routing: {precall_decision.get('reason')}. "
                        f"Use this instead: {suggestion}."
                    ),
                    "additionalContext": additional_context,
                }
                response["reason"] = (
                    f"Code Brain auto-routing: {precall_decision.get('reason')}. "
                    f"Use this instead: {suggestion}. "
                    "Or call MCP `mcp__code-brain__sandbox_execute` directly. "
                    "Code Brain stores full output in .ai/cache/sandbox/<exec_id>.txt and returns a short summary "
                    "(first 30 + last 5 lines, total under 4 KB) to keep your context window small."
                )
    return redact_value(response)


def codex_wire_output(response: dict[str, Any]) -> dict[str, Any]:
    """Project the verbose diagnostic hook response to Codex's strict wire schema.

    `ai hook --json` intentionally returns diagnostic fields used by tests and
    observability. Actual Codex hook commands must emit only fields accepted by
    the current hook runtime; otherwise Codex marks the hook as failed and opens.
    """
    hook = str(response.get("hook") or "")
    hook_specific = response.get("hookSpecificOutput")
    hook_specific = hook_specific if isinstance(hook_specific, dict) else {}

    if response.get("decision") == "block":
        reason = str(response.get("reason") or hook_specific.get("permissionDecisionReason") or "Blocked by Code Brain hook.")
        if hook == "PreToolUse":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        if hook in {"UserPromptSubmit", "PostToolUse", "Stop"}:
            return {"decision": "block", "reason": reason}

    additional_context = hook_specific.get("additionalContext")
    if hook in {"SessionStart", "UserPromptSubmit"} and additional_context:
        return {
            "hookSpecificOutput": {
                "hookEventName": hook,
                "additionalContext": str(additional_context),
            }
        }
    if hook == "PostToolUse" and additional_context:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": str(additional_context),
            }
        }
    if hook == "Stop":
        return {"continue": True}
    return {}


LIFECYCLE_SNAPSHOT_HOOKS = {"PreCompact", "SessionEnd"}


def _handle_lifecycle_event(root: Path, hook_name: str, payload: dict[str, Any]) -> None:
    """Side-effect handler for PreCompact / SessionEnd / Notification / PermissionRequest.

    Runs after append_event so audit ordering matches the original event timestamp.
    Errors are swallowed by the caller — never break the hook hot path.
    """
    from .memory import append_audit

    if hook_name in LIFECYCLE_SNAPSHOT_HOOKS:
        session_id = str(payload.get("session_id") or payload.get("sid") or "")
        agent = str(payload.get("agent") or "unknown")
        if session_id:
            try:
                from .session_resume import write_snapshot

                if hook_name == "PreCompact":
                    trigger = str(payload.get("trigger") or "unknown")
                    write_snapshot(
                        root,
                        session_id=session_id,
                        agent=agent,
                        force=True,
                        reason=f"precompact_{trigger}",
                    )
                    append_audit(
                        root,
                        action="compact.snapshot_forced",
                        category="memory",
                        payload={"trigger": trigger, "session_id": session_id},
                    )
                else:
                    reason = str(payload.get("reason") or "unknown")
                    write_snapshot(
                        root,
                        session_id=session_id,
                        agent=agent,
                        force=True,
                        reason=f"session_end_{reason}",
                    )
                    append_audit(
                        root,
                        action="session.end",
                        category="memory",
                        payload={"reason": reason, "session_id": session_id},
                    )
            except Exception:
                pass
        return

    if hook_name == "Notification":
        ntype = str(payload.get("type") or payload.get("notification_type") or "unknown")
        append_audit(
            root,
            action="notification.received",
            category="memory",
            payload={"type": ntype[:64]},
        )
        return

    if hook_name == "PermissionRequest":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "unknown")
        raw_input = payload.get("tool_input")
        description = ""
        if isinstance(raw_input, dict):
            description = str(raw_input.get("description") or "")[:200]
        append_audit(
            root,
            action="permission.requested",
            category="approval",
            payload={"tool_name": tool_name[:64], "description": description},
        )
        return

    if hook_name == "PermissionDenied":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "unknown")
        reason = str(payload.get("reason") or "")[:200]
        append_audit(
            root,
            action="permission.denied",
            category="approval",
            payload={"tool_name": tool_name[:64], "reason": reason},
        )
        return

    if hook_name == "PostCompact":
        trigger = str(payload.get("trigger") or "unknown")
        append_audit(
            root,
            action="compact.completed",
            category="memory",
            payload={"trigger": trigger},
        )
        return

    if hook_name == "CwdChanged":
        prev = str(payload.get("previous_cwd") or "")
        new = str(payload.get("new_cwd") or "")
        cross_project = False
        if prev and new:
            try:
                prev_root = Path(prev).resolve()
                new_root = Path(new).resolve()
                cross_project = (
                    prev_root != new_root
                    and not str(new_root).startswith(str(prev_root))
                    and not str(prev_root).startswith(str(new_root))
                )
            except Exception:
                cross_project = False
        append_audit(
            root,
            action="cwd.changed",
            category="memory",
            payload={
                "previous_cwd": prev[:200],
                "new_cwd": new[:200],
                "cross_project": cross_project,
            },
        )
        return

    if hook_name == "ConfigChange":
        source = str(payload.get("source") or "")
        append_audit(
            root,
            action="config.changed",
            category="memory",
            payload={"source": source[:64]},
        )
        return

    if hook_name == "InstructionsLoaded":
        file_path = str(payload.get("file_path") or "")
        memory_type = str(payload.get("memory_type") or "")
        load_reason = str(payload.get("load_reason") or "")
        append_audit(
            root,
            action="instructions.loaded",
            category="memory",
            payload={
                "file_path": file_path[:200],
                "memory_type": memory_type[:32],
                "load_reason": load_reason[:32],
            },
        )


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
    skill_recommendations = _skill_recommendation_context(root, hook_name, payload)
    if skill_recommendations:
        sections.append(skill_recommendations)
    session_tail = _read_text_tail(root / ".ai" / "memory" / "session-current.md", SESSION_TAIL_LINES)
    if session_tail:
        sections.append("Session-current tail:\n" + session_tail)
    try:
        from .config import load_config
        from .remote_memory import cache_path

        config = load_config(root)
        remote = config.get("remote_memory", {}) if isinstance(config.get("remote_memory"), dict) else {}
        if hook_name == "SessionStart" and bool(remote.get("inject_on_session_start", False)):
            cached = _read_text_tail(cache_path(root), 12)
            if cached:
                sections.append("Remote memory cached summary (no network in hook):\n" + cached)
    except Exception:
        pass
    composed = "\n\n".join(sections)
    if len(composed.encode("utf-8")) > MAX_INJECTION_BYTES:
        truncated = composed.encode("utf-8")[: MAX_INJECTION_BYTES - 3].decode("utf-8", errors="ignore") + "..."
        composed = truncated
    return composed
