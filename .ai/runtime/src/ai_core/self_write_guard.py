"""Write-validation gate for self-modifying memory (prompt growth / auto-learned rules).

Any text the system writes to itself — an auto-grown prompt rule, a learned precall/recommend
rule — passes through ``validate_self_write`` first. It deterministically refuses text that
tries to weaken a protected core invariant (M_core): security/permission boundaries, the
human-approval gates, redaction, and the no-auto-destructive rule. This is the ASI06
memory-poisoning gate: a bad lesson can never rewrite the rules that keep the agent safe.

No LLM, no network. stdlib only. The check is conservative (substring/keyword), erring toward
refusing a suspicious self-write rather than letting it through.
"""
from __future__ import annotations

import re
from typing import Any

# Protected invariants. Each entry: a short id, and regexes that signal an ATTEMPT to weaken it.
# Patterns match the *proposed self-write*, in both Korean and English, case-insensitive.
M_CORE: tuple[dict[str, Any], ...] = (
    {
        "id": "security_boundary",
        "forbid": [
            r"보안.{0,10}(무시|우회|약화|해제|꺼|끈다|끄고)",
            r"(ignore|bypass|disable|weaken|skip|turn\s*off)\s+(the\s+)?(security|safety)",
            r"시크릿|토큰|키.{0,6}(출력|노출|읽)",
            r"(print|expose|read|leak)\s+(secrets?|tokens?|credentials?|\.env)",
            r"(disable|skip|turn\s*off)\s+redact",
            r"redact\w*\s*(끄|해제|비활성|off)",
        ],
    },
    {
        "id": "approval_gate",
        "forbid": [
            r"(승인|허가)\s*(없이|생략|건너|무시).{0,12}(실행|적용|배포|커밋|삭제|결제)",
            r"(auto[-\s]?approve|skip\s+approval|without\s+approval|no\s+approval)",
            r"(자동\s*승인|승인\s*자동)",
        ],
    },
    {
        "id": "no_auto_destructive",
        "forbid": [
            r"(자동|무조건|항상).{0,8}(커밋|푸시|배포|삭제|머지|리베이스|drop\s+table)",
            r"(always|auto(?:matically)?)\s+(commit|push|deploy|delete|drop|merge|rebase|force)",
            r"(destructive|prod(uction)?\s+deploy).{0,12}(자동|auto|without)",
        ],
    },
    {
        "id": "no_self_unbounded",
        "forbid": [
            r"(프롬프트|규칙).{0,8}(자동\s*적용|사람\s*승인\s*없이|무인\s*반영)",
            r"(auto[-\s]?apply|without\s+human).{0,16}(prompt|rule|patch)",
        ],
    },
)


def validate_self_write(text: str) -> dict[str, Any]:
    """Return {ok, violations}. ok=False means the text tries to weaken a core invariant."""
    body = str(text or "")
    violations: list[dict[str, str]] = []
    for inv in M_CORE:
        for pat in inv["forbid"]:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                violations.append({"invariant": inv["id"], "matched": m.group(0)[:60]})
                break
    return {"ok": not violations, "violations": violations}
