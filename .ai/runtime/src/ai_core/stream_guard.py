from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_CREDENTIAL_PATH_PATTERN = (
    r"(?ix)(?<![\w.-])"
    r"(?:(?:~|\.{1,2}|[A-Z]:)?/)?"
    r"(?:[\w@+.-]+/)*"
    r"(?:\.env(?:\.[\w.-]+)?|auth\.json|credentials\.json|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)|[\w@+.-]+\.(?:pem|key|p8|p12|pfx))"
    r"(?![\w.-])"
)
_COMMAND_KEYS = ("command", "CommandLine", "commandLine")
_PATH_KEY_NAMES = frozenset(
    {
        "absolutepath", "file", "filename", "filenames", "filepath", "filepaths",
        "files", "notebookpath", "path", "paths", "relativepath", "targetfile",
        "targetpath", "uri", "uris",
    }
)
_STRUCTURED_FILE_TOOLS = frozenset(
    {
        "edit", "multiedit", "notebookedit", "read", "view_file", "write",
        "write_to_file", "replace_file_content", "multi_replace_file_content",
    }
)
_PATCH_TOOLS = frozenset({"apply_patch"})


@dataclass(frozen=True)
class StreamRule:
    id: str
    pattern: str
    scopes: tuple[str, ...]
    action: str
    message: str


DEFAULT_RULES: tuple[StreamRule, ...] = (
    StreamRule(
        id="credential_path",
        pattern=_CREDENTIAL_PATH_PATTERN,
        # A user may safely discuss a credential filename. Blocking belongs at the
        # tool boundary where a read/edit/print could actually expose it.
        scopes=("tool",),
        action="block",
        message="credential-like path detected; do not read or print real secrets",
    ),
    StreamRule(
        id="private_key_literal",
        pattern=r"-----BEGIN (?:(?:OPENSSH|RSA|DSA|EC|ENCRYPTED) )?PRIVATE KEY-----",
        scopes=("tool", "output", "prompt"),
        action="block",
        message="private key material detected",
    ),
    StreamRule(
        id="destructive_git",
        pattern=r"(?i)\b(?:git\s+reset\s+--hard|git\s+checkout\s+--\s+\.|rm\s+-rf\s+(?:/|~|\$HOME))",
        scopes=("tool",),
        action="block",
        message="destructive command requires explicit user approval",
    ),
)


def _payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _normalized_tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool_name") or payload.get("tool") or "").rsplit(".", 1)[-1].lower()


def _command_text(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _COMMAND_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def _patch_target_text(tool_input: Any) -> str:
    text = ""
    if isinstance(tool_input, dict):
        for key in ("patch", "input", "text"):
            value = tool_input.get(key)
            if isinstance(value, str):
                text = value
                break
    elif isinstance(tool_input, str):
        text = tool_input
    targets = re.findall(r"(?m)^\*\*\*\s+(?:Add|Update|Delete) File:\s*(.+?)\s*$", text)
    return "\n".join(targets)


def _structured_path_text(tool_input: Any) -> str:
    if isinstance(tool_input, str):
        return tool_input
    values: list[str] = []
    stack: list[tuple[Any, int]] = [(tool_input, 0)]
    seen: set[int] = set()
    visited = 0
    while stack and visited < 256 and len(values) < 64:
        current, depth = stack.pop()
        if depth > 8 or not isinstance(current, (dict, list, tuple)):
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        visited += 1
        if isinstance(current, dict):
            for key, value in current.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in _PATH_KEY_NAMES or normalized.endswith(("path", "paths")):
                    if isinstance(value, str) and value.strip():
                        values.append(value)
                    elif isinstance(value, (list, tuple)):
                        values.extend(
                            item for item in value if isinstance(item, str) and item.strip()
                        )
                if isinstance(value, (dict, list, tuple)):
                    stack.append((value, depth + 1))
        else:
            stack.extend((value, depth + 1) for value in current if isinstance(value, (dict, list, tuple)))
    return "\n".join(values[:64])


def _merge_tool_scans(*scans: dict[str, Any]) -> dict[str, Any]:
    matches = [match for scan in scans for match in scan.get("matches", []) if isinstance(match, dict)]
    return {
        "ok": not any(match.get("action") == "block" for match in matches),
        "scope": "tool",
        "matches": matches,
    }


def _scan_pretool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply each rule to the part of a tool call where it is executable.

    Patch/write bodies may legitimately contain synthetic credential filenames or
    dangerous-command fixtures. Their target path is security-relevant; the body is
    still scanned for literal private-key material. Shell commands remain conservative.
    """
    tool_input = payload.get("tool_input") or payload
    raw_text = _payload_text(tool_input)
    tool_name = _normalized_tool_name(payload)
    command = _command_text(tool_input)

    if command:
        credential_source = command
        destructive_source = command
    elif tool_name in _PATCH_TOOLS:
        credential_source = _patch_target_text(tool_input) or raw_text
        destructive_source = ""
    elif tool_name in _STRUCTURED_FILE_TOOLS:
        credential_source = _structured_path_text(tool_input) or raw_text
        destructive_source = ""
    else:
        credential_source = raw_text
        destructive_source = raw_text

    credential_rules = tuple(rule for rule in DEFAULT_RULES if rule.id == "credential_path")
    destructive_rules = tuple(rule for rule in DEFAULT_RULES if rule.id == "destructive_git")
    literal_rules = tuple(rule for rule in DEFAULT_RULES if rule.id == "private_key_literal")
    return _merge_tool_scans(
        scan_text(credential_source, scope="tool", rules=credential_rules),
        scan_text(destructive_source, scope="tool", rules=destructive_rules),
        scan_text(raw_text, scope="tool", rules=literal_rules),
    )


def scan_text(text: str, *, scope: str, rules: tuple[StreamRule, ...] = DEFAULT_RULES) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for rule in rules:
        if scope not in rule.scopes:
            continue
        found = re.search(rule.pattern, text)
        if not found:
            continue
        matches.append(
            {
                "id": rule.id,
                "action": rule.action,
                "message": rule.message,
                "span": [found.start(), found.end()],
            }
        )
    block = any(match.get("action") == "block" for match in matches)
    return {"ok": not block, "scope": scope, "matches": matches}


def evaluate_hook_payload(hook_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if hook_name == "PreToolUse":
        return _scan_pretool_payload(payload)
    if hook_name == "UserPromptSubmit":
        prompt = ""
        for key in ("prompt", "message", "user_prompt"):
            value = payload.get(key)
            if isinstance(value, str):
                prompt = value
                break
        return scan_text(prompt, scope="prompt")
    if hook_name == "PostToolUse":
        raw = payload.get("tool_response", payload.get("tool_output", ""))
        return scan_text(_payload_text(raw), scope="output")
    return {"ok": True, "scope": "none", "matches": []}


def decision_reason(scan: dict[str, Any]) -> str:
    matches = scan.get("matches")
    if not isinstance(matches, list) or not matches:
        return "stream guard matched"
    first = matches[0]
    if not isinstance(first, dict):
        return "stream guard matched"
    return f"Code Brain stream guard: {first.get('id')}: {first.get('message')}"
