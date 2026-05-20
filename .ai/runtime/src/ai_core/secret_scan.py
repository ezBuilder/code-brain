"""Regex-based inline secret scanner (T47).

LLM-generated code frequently inlines real credentials. This module performs
a fast, dependency-free regex scan over a source string and returns masked
findings. Used by ``ast_verify.verify_source`` as a final pass after AST
checks (toggle with ``AI_AST_VERIFY_SECRETS=0``).

Design constraints:
  * No external dependencies, no network.
  * All patterns precompiled at module load.
  * Raw secret values are never echoed back — findings expose at most the
    last 4 characters, prefixed with ``***``.
  * Patterns biased toward low false positives (length floors, anchors).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SecretFinding:
    kind: str          # e.g. "aws_access_key", "openai_api_key"
    detail: str        # masked sample, e.g. "***WXYZ"
    lineno: int        # 1-indexed
    col_offset: int    # 0-indexed


# (kind, compiled_regex, group_index_for_value)
#
# group_index_for_value:
#   - 0  → use the entire match (group(0)) as the secret value to mask.
#   - >0 → use that capture group as the secret value.
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    # 1. AWS access key (anchored prefix + 16 upper/digit chars)
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}"), 0),
    # 2. AWS secret key — `aws_secret...=...<40 base64ish>` (lookahead-ish via .{0,N})
    (
        "aws_secret_key",
        re.compile(r"aws_secret.{0,20}=.{0,5}([A-Za-z0-9/+=]{40})", re.IGNORECASE),
        1,
    ),
    # 4. Anthropic API key — checked BEFORE openai to avoid sk- prefix collision
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    # 3. OpenAI API key — generic sk- prefix
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{20,}"), 0),
    # 5. GitHub personal access token / OAuth / app / server-to-server / user-to-server
    ("github_pat", re.compile(r"gh[opsu]_[A-Za-z0-9]{30,}"), 0),
    # 6. Slack bot/app/user token
    ("slack_token", re.compile(r"xox[abp]-[A-Za-z0-9\-]{10,}"), 0),
    # 7. JWT (three base64url segments separated by dots; eyJ prefix on header & payload)
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_=\-]+\.eyJ[A-Za-z0-9_=\-]+\.[A-Za-z0-9_=\-]+"),
        0,
    ),
    # 8. Generic high-entropy assignment: password/secret/api_key/token = "..."
    (
        "generic_secret",
        re.compile(
            r"""(?ix)
            \b(?:password|passwd|secret|api[_\-]?key|token)\s*[:=]\s*
            ["']([A-Za-z0-9!@\#\$%\^&\*\(\)_\+\-=]{12,})["']
            """,
        ),
        1,
    ),
    # 9. Private key PEM header
    (
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        0,
    ),
]


def _mask(value: str) -> str:
    """Return a redacted sample exposing only the last 4 chars."""
    if not value:
        return "***"
    tail = value[-4:] if len(value) >= 4 else value
    return f"***{tail}"


def _lineno_col(source: str, offset: int) -> tuple[int, int]:
    """Translate an absolute byte offset within ``source`` to (lineno, col)."""
    if offset <= 0:
        return 1, 0
    prefix = source[:offset]
    lineno = prefix.count("\n") + 1
    last_nl = prefix.rfind("\n")
    col = offset - (last_nl + 1) if last_nl >= 0 else offset
    return lineno, col


def scan_source(source: str) -> list[SecretFinding]:
    """Scan ``source`` and return all masked secret findings.

    Order is deterministic (pattern order, then position within source).
    Anthropic keys are checked before generic OpenAI ``sk-`` to give them
    a more specific kind label; both may still match in unusual inputs.
    """
    if not source:
        return []

    findings: list[SecretFinding] = []
    seen_spans: set[tuple[int, int]] = set()

    for kind, pattern, group_idx in _PATTERNS:
        for match in pattern.finditer(source):
            span = match.span(group_idx) if group_idx else match.span(0)
            if span in seen_spans:
                continue
            seen_spans.add(span)
            value = match.group(group_idx) if group_idx else match.group(0)
            lineno, col = _lineno_col(source, span[0])
            findings.append(
                SecretFinding(
                    kind=kind,
                    detail=_mask(value),
                    lineno=lineno,
                    col_offset=col,
                )
            )
    return findings


__all__ = ["SecretFinding", "scan_source"]
