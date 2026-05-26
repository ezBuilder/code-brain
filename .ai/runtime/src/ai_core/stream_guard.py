from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


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
        pattern=r"(?i)(^|[\s\"'])(?:\.env(?:\.[\w.-]+)?|auth\.json|credentials\.json|id_(?:rsa|dsa|ecdsa|ed25519)|[\w./-]+\.(?:pem|key|p12|pfx))($|[\s\"'])",
        scopes=("tool", "prompt"),
        action="block",
        message="credential-like path detected; do not read or print real secrets",
    ),
    StreamRule(
        id="private_key_literal",
        pattern=r"-----BEGIN (?:OPENSSH|RSA|DSA|EC|PRIVATE) KEY-----",
        scopes=("tool", "output", "prompt"),
        action="block",
        message="private key material detected",
    ),
    StreamRule(
        id="destructive_git",
        pattern=r"(?i)\b(?:git\s+reset\s+--hard|git\s+checkout\s+--\s+\.|rm\s+-rf\s+(?:/|~|\$HOME))",
        scopes=("tool", "prompt"),
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
        text = _payload_text(payload.get("tool_input") or payload)
        return scan_text(text, scope="tool")
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
