from __future__ import annotations

import re
from typing import Any

PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]+=*\b"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9./+=-]{20,}['\"]?"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (RSA |EC |OPENSSH )?PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"),
    re.compile(r"/Users/[^/\s]+/"),
    re.compile(r"/home/[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
]

SECRET_PATTERNS = PATTERNS[:8]
SECRET_MATCHER_VERSION = 2
_ASSIGNMENT_TERMS = ("apikey", "api_key", "api-key", "secret", "token", "password")
_ASSIGNMENT_VALUE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789./+=-"
)
_UNICODE_IGNORECASE_EXTRAS = frozenset("İıſK")


def _contains_assignment_secret(value: str, lowered: str) -> bool:
    length = len(value)
    for term in _ASSIGNMENT_TERMS:
        offset = 0
        while True:
            found = lowered.find(term, offset)
            if found < 0:
                break
            cursor = found + len(term)
            while cursor < length and value[cursor].isspace():
                cursor += 1
            if cursor >= length or value[cursor] not in {":", "="}:
                offset = found + 1
                continue
            cursor += 1
            while cursor < length and value[cursor].isspace():
                cursor += 1
            if cursor < length and value[cursor] in {"'", '"'}:
                cursor += 1
            start = cursor
            while cursor < length and value[cursor] in _ASSIGNMENT_VALUE_CHARS:
                cursor += 1
            if cursor - start >= 20:
                return True
            offset = found + 1
    return False


def contains_secret(value: str) -> bool:
    """Existence-only secret scan with necessary-prefix prefilters.

    Each branch still delegates the final decision to the original compiled
    regex. The cheap literal checks are necessary conditions, so this is
    semantically identical to ``any(pattern.search(value) ...)`` while avoiding
    eight full-text regex passes for ordinary source files.
    """
    if "AKIA" in value and SECRET_PATTERNS[0].search(value):
        return True
    lowered = value.lower()
    is_ascii = value.isascii()
    needs_unicode_fallback = not is_ascii and any(
        character in value for character in _UNICODE_IGNORECASE_EXTRAS
    )
    github_candidate = (
        "ghp_" in lowered
        or "gho_" in lowered
        or "ghu_" in lowered
        or "ghs_" in lowered
        or "ghr_" in lowered
    )
    if github_candidate or needs_unicode_fallback:
        if SECRET_PATTERNS[1].search(value):
            return True
    if "github_pat_" in value and SECRET_PATTERNS[2].search(value):
        return True
    if "sk-" in value and SECRET_PATTERNS[3].search(value):
        return True
    if "xox" in value and SECRET_PATTERNS[4].search(value):
        return True
    if ("authorization" in lowered and "bearer" in lowered) or needs_unicode_fallback:
        if SECRET_PATTERNS[5].search(value):
            return True
    assignment_candidate = (
        "apikey" in lowered
        or "api_key" in lowered
        or "api-key" in lowered
        or "secret" in lowered
        or "token" in lowered
        or "password" in lowered
    )
    if assignment_candidate or needs_unicode_fallback:
        if (
            _contains_assignment_secret(value, lowered)
            if not needs_unicode_fallback
            else SECRET_PATTERNS[6].search(value) is not None
        ):
            return True
    if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
        if SECRET_PATTERNS[7].search(value):
            return True
    return False


def redact_text(value: str) -> str:
    redacted = value
    for pattern in PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value
