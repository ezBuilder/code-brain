from __future__ import annotations

import re
from pathlib import Path
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
SECRET_MATCHER_VERSION = 4
_ASSIGNMENT_TERMS = ("apikey", "api_key", "api-key", "secret", "token", "password")
_ASSIGNMENT_VALUE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789./+=-"
)
_UNICODE_IGNORECASE_EXTRAS = frozenset("İıſK")


# Words that may appear in a documentation placeholder. Membership here is not
# sufficient on its own: a placeholder must also contain a marker below, so
# descriptive nouns like "password" or "prod" cannot form an exemption by
# themselves.
_PLACEHOLDER_WORDS = frozenset(
    {
        "a",
        "account",
        "actual",
        "add",
        "an",
        "api",
        "app",
        "change",
        "changeme",
        "client",
        "credential",
        "credentials",
        "demo",
        "dev",
        "development",
        "dummy",
        "email",
        "empty",
        "enter",
        "example",
        "fake",
        "goes",
        "here",
        "hidden",
        "id",
        "insert",
        "key",
        "keys",
        "local",
        "masked",
        "my",
        "name",
        "none",
        "null",
        "omitted",
        "optional",
        "org",
        "own",
        "passphrase",
        "password",
        "passwords",
        "placeholder",
        "prod",
        "production",
        "project",
        "put",
        "real",
        "redacted",
        "replace",
        "required",
        "sample",
        "secret",
        "secrets",
        "server",
        "set",
        "some",
        "specific",
        "staging",
        "team",
        "test",
        "the",
        "todo",
        "token",
        "tokens",
        "unset",
        "user",
        "username",
        "value",
        "with",
        "xxx",
        "xxxx",
        "your",
        "yours",
    }
)

# A placeholder must declare itself. These segments mean "substitute your own"
# or "this is not a real value", which no operator writes inside an actual
# credential. Without one of them the value stays classified as a possible
# secret, so a weak-but-real credential built from ordinary words is still
# reported instead of being silently exempted.
_PLACEHOLDER_MARKERS = frozenset(
    {
        "change",
        "changeme",
        "demo",
        "dummy",
        "empty",
        "enter",
        "example",
        "fake",
        "goes",
        "here",
        "hidden",
        "insert",
        "masked",
        "none",
        "null",
        "omitted",
        "optional",
        "placeholder",
        "put",
        "redacted",
        "replace",
        "sample",
        "specific",
        "test",
        "todo",
        "unset",
        "xxx",
        "xxxx",
        "your",
        "yours",
    }
)


def _is_word_placeholder(candidate: str) -> bool:
    """True when a value declares itself a documentation placeholder.

    Three conditions must all hold, and each removes a different way a real
    credential could slip through:

    1. No entropy. The value is single-case with no digits and no symbols
       besides `-`/`_`. Real credentials mix case, carry digits, or use other
       symbols.
    2. Known vocabulary. Every `-`/`_` segment is an English word from
       `_PLACEHOLDER_WORDS`, and there are at least two segments. One
       unrecognized segment keeps the value classified as a possible secret.
    3. Declared intent. At least one segment is a `_PLACEHOLDER_MARKERS` word.
       That is what separates a substitute-me placeholder from a weak real
       password assembled out of the same descriptive nouns.
    """
    if not candidate:
        return False
    if not (candidate.islower() or candidate.isupper()):
        return False
    lowered_candidate = candidate.lower()
    if any(character.isdigit() for character in lowered_candidate):
        return False
    normalized = lowered_candidate.replace("_", "-").strip("-")
    if not normalized:
        return False
    segments = [segment for segment in normalized.split("-") if segment]
    if len(segments) < 2:
        return False
    if not all(segment.isalpha() for segment in segments):
        return False
    if not all(segment in _PLACEHOLDER_WORDS for segment in segments):
        return False
    return any(segment in _PLACEHOLDER_MARKERS for segment in segments)


def _looks_like_code_type_annotation(
    value: str,
    *,
    term: str,
    term_start: int,
    separator: str,
    candidate: str,
    candidate_end: int,
    quoted: bool,
    source_path: str | Path | None,
) -> bool:
    """Recognize typed declarations/selectors without exempting config values."""
    if quoted or separator != ":" or not candidate.isascii() or not candidate.isalpha():
        return False
    if candidate.isupper() or candidate.islower():
        return False

    previous = value[term_start - 1] if term_start else ""
    following = value[candidate_end] if candidate_end < len(value) else ""
    prefix = value[max(0, term_start - 256) : term_start]
    line_prefix = prefix.rsplit("\n", 1)[-1].rsplit("\r", 1)[-1]
    line_suffix = value[candidate_end : candidate_end + 256].split("\n", 1)[0].split("\r", 1)[0]
    objc_method = any(marker in line_prefix for marker in ("+[", "-[")) and "]" in line_suffix
    objc_selector = "@selector(" in line_prefix and ")" in line_suffix
    selector_context = (
        previous.isalnum()
        and following == ":"
        and (objc_method or objc_selector)
    )
    if selector_context:
        return True
    if not candidate[0].isupper():
        return False

    swift_source = source_path is not None and Path(source_path).suffix.casefold() == ".swift"
    parameter_context = swift_source and (
        re.search(r"\b(?:func|init|subscript)\b[^()]*\([^()]*$", line_prefix) is not None
    )
    embedded_binding = previous.isalnum() and (
        re.search(r"\b(?:let|var)\s+[A-Za-z_][A-Za-z0-9_]*$", line_prefix) is not None
    )
    bare_binding = (
        candidate.casefold().endswith(term.casefold())
        and re.search(r"\b(?:let|var)\s*$", line_prefix) is not None
        and re.fullmatch(r"[?!]?\s*(?://.*)?", line_suffix) is not None
    )
    binding_context = swift_source and (embedded_binding or bare_binding)
    declaration_context = parameter_context or binding_context
    return declaration_context and "=" not in line_suffix


def _assignment_secret_spans(
    value: str,
    lowered: str,
    *,
    preserve_identifiers: bool = True,
    source_path: str | Path | None = None,
) -> list[tuple[int, int]]:
    length = len(value)
    spans: list[tuple[int, int]] = []
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
            separator = value[cursor]
            cursor += 1
            while cursor < length and value[cursor].isspace():
                cursor += 1
            quoted = cursor < length and value[cursor] in {"'", '"'}
            quote = value[cursor] if quoted else ""
            if quoted:
                cursor += 1
            start = cursor
            while cursor < length and value[cursor] in _ASSIGNMENT_VALUE_CHARS:
                cursor += 1
            candidate = value[start:cursor]
            # Avoid treating ordinary program expressions such as
            # ``token = cancellationToken`` or ``password = props.getValue``
            # as credentials. Unquoted all-letter/dotted values are identifiers,
            # and a quoted one-character repetition is a deterministic test
            # placeholder. Real generic credentials remain detected when quoted
            # or when an unquoted value contains digits/symbols.
            identifier_expression = (
                not quoted
                and all(character.isalpha() or character == "." for character in candidate)
                and (
                    "." in candidate
                    or (cursor < length and value[cursor] in {"(", ";", ",", "@", "["})
                )
            )
            repeated_placeholder = quoted and len(set(candidate)) <= 1
            # Documentation placeholders describe the *shape* of a credential
            # instead of carrying one: a build guide that documents an Apple
            # app-specific password env var, or an API key placeholder telling
            # the reader to substitute their own, is built from self-describing
            # English hyphen/underscore words. A strict doctor must not fail a repo
            # for documenting its own environment variables. Real credentials
            # of the same length carry entropy: digits, mixed case, or symbols
            # outside `-`/`_`, none of which a placeholder word has. Apple's
            # actual format (`abcd-efgh-ijkl-mnop`) is unaffected because this
            # generic assignment rule only fires at >=20 characters.
            word_placeholder = _is_word_placeholder(candidate)
            code_type_annotation = _looks_like_code_type_annotation(
                value,
                term=term,
                term_start=found,
                separator=separator,
                candidate=candidate,
                candidate_end=cursor,
                quoted=quoted,
                source_path=source_path,
            )
            if (
                len(candidate) >= 20
                and not (preserve_identifiers and identifier_expression)
                and not repeated_placeholder
                and not word_placeholder
                and not code_type_annotation
            ):
                end = cursor + (1 if quoted and cursor < length and value[cursor] == quote else 0)
                spans.append((found, end))
            offset = found + 1
    return spans


def _contains_assignment_secret(
    value: str,
    lowered: str,
    *,
    source_path: str | Path | None = None,
) -> bool:
    return bool(_assignment_secret_spans(value, lowered, source_path=source_path))


def contains_secret(value: str, *, source_path: str | Path | None = None) -> bool:
    """Existence-only secret scan with necessary-prefix prefilters.

    Prefix-specific branches delegate to compiled patterns. The generic
    assignment branch additionally excludes bounded placeholders and code
    identifiers so tracked fixtures and type annotations do not become findings.
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
            _contains_assignment_secret(value, lowered, source_path=source_path)
            if not needs_unicode_fallback
            else SECRET_PATTERNS[6].search(value) is not None
        ):
            return True
    if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
        if SECRET_PATTERNS[7].search(value):
            return True
    return False


def redact_text(value: str, *, source_path: str | Path | None = None) -> str:
    redacted = value
    for pattern in PATTERNS[:6]:
        redacted = pattern.sub("[REDACTED]", redacted)
    needs_unicode_fallback = not redacted.isascii() and any(
        character in redacted for character in _UNICODE_IGNORECASE_EXTRAS
    )
    if needs_unicode_fallback:
        redacted = SECRET_PATTERNS[6].sub("[REDACTED]", redacted)
    else:
        spans = sorted(
            _assignment_secret_spans(
                redacted,
                redacted.lower(),
                preserve_identifiers=False,
                source_path=source_path,
            )
        )
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in reversed(merged):
            redacted = redacted[:start] + "[REDACTED]" + redacted[end:]
    for pattern in PATTERNS[7:]:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_value(value: Any, *, source_path: str | Path | None = None) -> Any:
    if isinstance(value, str):
        return redact_text(value, source_path=source_path)
    if isinstance(value, list):
        return [redact_value(item, source_path=source_path) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, source_path=source_path) for key, item in value.items()}
    return value
