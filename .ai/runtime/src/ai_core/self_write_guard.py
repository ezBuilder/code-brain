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


# A self-improvement rule may only tune BEHAVIOUR/STYLE (brevity, verification, reporting). It may
# never even MENTION a security-sensitive domain — that is an allow-by-domain defence that closes
# the open-ended "soften the rule" phrasings keyword-verb matching misses. Any rule touching these
# domains is refused outright; legitimate behavioural rules never need these words.
_FORBIDDEN_DOMAIN = re.compile(
    r"(보안|security|safety|인증|\bauth|oauth|권한|permission|승인|approval|허가|consent|"
    r"시크릿|secret|크리덴셜|credential|토큰|token|\.env|환경\s*변수|env\s*var|api[\s._\-]?key|"
    r"private[\s._\-]?key|비밀번호|password|passwd|배포|deploy|prod|커밋|commit|푸시|push|"
    r"머지|merge|리베이스|rebase|삭제|delete|drop|truncate|rm\s|redact|마스킹|결제|billing|payment|"
    r"sandbox|샌드박스|bypass|우회|disable|비활성|kubectl|terraform)",
    re.IGNORECASE,
)


# Common Cyrillic/Greek homoglyphs → Latin, so "аuth"/"ѕecret" cannot smuggle a domain word past
# the ASCII patterns. (unicodedata has no confusables table; this small map covers the usual ones.)
_CONFUSABLE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "с": "c", "р": "p", "х": "x", "у": "y", "ѕ": "s", "і": "i",
    "ј": "j", "ԁ": "d", "ո": "n", "ɡ": "g", "ｅ": "e", "А": "A", "Е": "E", "О": "O", "С": "C",
    "Р": "P", "Х": "X", "ο": "o", "ν": "v", "α": "a",
})


def _fold(text: str) -> str:
    """Canonical form for matching: NFKC folds fullwidth/compatibility forms WITHOUT decomposing
    Hangul (so Korean keywords stay intact), and confusable homoglyphs are mapped to Latin. The
    original, NFKC, and de-confused copies are all searched so KO and Latin patterns both match."""
    import unicodedata

    body = str(text or "")
    nfkc = unicodedata.normalize("NFKC", body)
    deconf = nfkc.translate(_CONFUSABLE)
    return f"{body} {nfkc} {deconf}"


def validate_self_write(text: str) -> dict[str, Any]:
    """Return {ok, violations}. ok=False means the text touches a protected domain / weakens a core invariant."""
    body = _fold(text)
    violations: list[dict[str, str]] = []
    dom = _FORBIDDEN_DOMAIN.search(body)
    if dom:
        # a behavioural self-improvement rule has no business mentioning a security domain at all
        violations.append({"invariant": "out_of_scope_domain", "matched": dom.group(0)[:60]})
    for inv in M_CORE:
        for pat in inv["forbid"]:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                violations.append({"invariant": inv["id"], "matched": m.group(0)[:60]})
                break
    return {"ok": not violations, "violations": violations}
