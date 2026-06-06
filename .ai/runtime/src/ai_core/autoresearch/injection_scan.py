"""Heuristic prompt-injection signal scan for untrusted ingest content (PRD §12.2.6).

NOT a guarantee — a defense *signal*. Content matching known manipulation patterns is
flagged so ingest marks the source `quarantined`, which propagates `taint` to derived
pages (laundering guard). Reuses stream_guard's rule engine; stdlib only. The real
boundary remains structural (nonce + verify-det + agent-side review); this only raises
a low-trust flag so tainted material is surfaced rather than silently trusted.
"""
from __future__ import annotations

from ..stream_guard import StreamRule, scan_text

INJECTION_RULES: tuple[StreamRule, ...] = (
    StreamRule(
        id="ignore_previous",
        pattern=r"(?i)\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all)\b",
        scopes=("data",), action="flag", message="instruction-override phrasing",
    ),
    StreamRule(
        id="role_override",
        pattern=r"(?i)(?:you are now|act as|pretend to be|new instructions?:|system prompt:)",
        scopes=("data",), action="flag", message="role/instruction override",
    ),
    StreamRule(
        id="tag_injection",
        pattern=r"(?i)</?(?:system|instruction|admin)[\s>]",
        scopes=("data",), action="flag", message="control-tag injection",
    ),
    StreamRule(
        id="exfil_phrase",
        pattern=r"(?i)\b(?:send|exfiltrate|post|leak|email)\b[^.\n]{0,30}\b(?:secret|api[_ ]?key|password|token|credential)",
        scopes=("data",), action="flag", message="exfiltration phrasing",
    ),
)


MAX_SCAN_LEN = 50_000  # cap untrusted input fed to the regex engine (ReDoS guard)


def scan_injection(content: str) -> dict:
    """Return {"flagged": bool, "signals": [rule_id, ...]}. Heuristic quarantine signal.

    Only the first MAX_SCAN_LEN chars are scanned, bounding regex backtracking on hostile
    long inputs (web-crawled content is untrusted); manipulation phrasing sits up front.
    """
    res = scan_text(content[:MAX_SCAN_LEN], scope="data", rules=INJECTION_RULES)
    signals = [m["id"] for m in res.get("matches", []) if isinstance(m, dict)]
    return {"flagged": bool(signals), "signals": signals}
