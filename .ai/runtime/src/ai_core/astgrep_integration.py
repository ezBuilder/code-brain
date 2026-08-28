"""Optional ast-grep integration for multi-language AST rule checks (T48).

ast-grep (https://ast-grep.github.io) is a tree-sitter based AST matcher
supporting 26+ languages. We invoke it as an external binary (``ast-grep``
or ``sg``) so the runtime stays dependency-free when the tool is absent.

Behaviour summary:
  * ``astgrep_available()`` — quick PATH probe.
  * ``scan_path(path, rule_yaml)`` — write rule YAML to a temp file, call
    ``ast-grep scan --rule <yaml> --json=stream <path>``, parse the result
    line-by-line into dicts. Any failure (missing binary, timeout, bad
    yaml, non-zero exit) is swallowed — return ``[]``.
  * ``AI_ASTGREP_DISABLE=1`` short-circuits to ``[]`` regardless.

The default ruleset (``_DEFAULT_RULES``) covers a few high-signal JS/TS
hazards that the Python-only ``ast_verify`` cannot see.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .private_write import read_root_confined_text, validate_root_confined_directory


def astgrep_available() -> bool:
    """True iff an ``ast-grep`` or ``sg`` binary is on PATH."""
    return bool(shutil.which("ast-grep") or shutil.which("sg"))


def _binary() -> str | None:
    return shutil.which("ast-grep") or shutil.which("sg")


AST_PATTERN_MAX_CHARS = 4096
AST_RULE_MAX_CHARS = 64 * 1024
AST_PATH_MAX_CHARS = 1024
AST_RESULT_MAX = 100
AST_SCAN_MAX_FINDINGS = 2_000
AST_OUTPUT_MAX_BYTES = 2 * 1024 * 1024
AST_OUTPUT_MAX_EVENTS = 2_000
AST_TIMEOUT_MAX_SECONDS = 30.0
AST_MATERIALIZE_MAX_FILES = 5_000
AST_MATERIALIZE_MAX_BYTES = 32 * 1024 * 1024


def _normalise_timeout(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        parsed = default
    return max(0.1, min(AST_TIMEOUT_MAX_SECONDS, parsed))


def _parse_findings(lines: list[str], *, max_findings: int) -> list[dict[str, Any]]:
    cap = max(0, min(AST_SCAN_MAX_FINDINGS, int(max_findings)))
    if cap == 0:
        return []
    stripped = "\n".join(lines).strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)][:cap]
    findings: list[dict[str, Any]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            findings.append(item)
            if len(findings) >= cap:
                break
    return findings


# Minimal cross-language ruleset. Each rule must be a valid ast-grep rule
# (YAML document). We keep this small on purpose — projects can pass their
# own rule_yaml to ``scan_path``.
_DEFAULT_RULES = """\
id: no-eval-call
language: JavaScript
rule:
  pattern: eval($ARG)
severity: error
message: avoid eval()
---
id: no-function-constructor
language: JavaScript
rule:
  pattern: new Function($$$ARGS)
severity: error
message: avoid Function() constructor
---
id: no-child-process-exec
language: JavaScript
rule:
  pattern: child_process.exec($$$ARGS)
severity: error
message: avoid child_process.exec
---
id: no-http-url
language: JavaScript
rule:
  pattern: "'http://$URL'"
severity: warning
message: hardcoded http:// URL
"""


def scan_path(
    path: Path,
    rule_yaml: str | None = None,
    *,
    timeout_seconds: float = 5.0,
) -> list[dict]:
    """Run ast-grep against ``path`` with ``rule_yaml`` (defaults to built-in).

    Returns a list of finding dicts as emitted by ``--json=stream`` (one JSON
    object per line). Returns ``[]`` on any error or when ast-grep is absent.
    """
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    binary = _binary()
    if not binary:
        return []

    p = Path(path)
    try:
        state = p.lstat()
    except OSError:
        return []
    if p.is_symlink() or not (p.is_file() or p.is_dir()):
        return []

    yaml_body = rule_yaml if rule_yaml is not None else _DEFAULT_RULES
    if "\x00" in yaml_body or len(yaml_body) > AST_RULE_MAX_CHARS:
        return []

    with tempfile.TemporaryDirectory(prefix="cb-astgrep-") as tmp:
        rule_file = Path(tmp) / "rules.yml"
        try:
            rule_file.write_text(yaml_body, encoding="utf-8")
        except OSError:
            return []

        cmd = [
            binary,
            "scan",
            "--rule",
            str(rule_file),
            "--json=stream",
            str(p),
        ]
        from .search import _run_process_lines_bounded

        lines = _run_process_lines_bounded(
            cmd,
            timeout_seconds=_normalise_timeout(timeout_seconds, default=5.0),
            max_output_bytes=AST_OUTPUT_MAX_BYTES,
            max_events=AST_OUTPUT_MAX_EVENTS,
            require_complete=True,
        )
        return _parse_findings(lines, max_findings=AST_SCAN_MAX_FINDINGS)


_SG_LANGS = {
    "python", "py", "javascript", "js", "typescript", "ts", "tsx", "jsx",
    "go", "rust", "rs", "java", "c", "cpp", "ruby", "php", "kotlin", "swift", "scala",
}
_SG_LANG_ALIAS = {"py": "python", "js": "javascript", "ts": "typescript", "rs": "rust"}
_SG_LANG_SUFFIXES = {
    "python": {".py"},
    "javascript": {".js", ".jsx"},
    "jsx": {".jsx"},
    "typescript": {".ts", ".tsx"},
    "tsx": {".tsx"},
    "go": {".go"},
    "rust": {".rs"},
    "java": {".java"},
    "c": {".c", ".h"},
    "cpp": {".cc", ".cpp", ".cxx", ".h", ".hpp"},
    "ruby": {".rb"},
    "php": {".php"},
    "kotlin": {".kt", ".kts"},
    "swift": {".swift"},
    "scala": {".scala"},
}


def _normalise_result_limit(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(1, min(AST_RESULT_MAX, parsed))


def _normalise_scope_path(value: object) -> tuple[Path | None, str | None]:
    if value is None or str(value).strip() == "":
        return None, None
    raw = str(value).strip()
    if raw in {".", "./"}:
        return None, None
    if "\x00" in raw:
        return None, "invalid path control character"
    if len(raw) > AST_PATH_MAX_CHARS:
        return None, "path too long"
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None, "path escapes repo"
    return path, None


def _finding_repo_path(
    raw: object,
    *,
    mirror_root: Path,
    exact_scope: Path | None,
) -> str | None:
    value = str(raw or "").strip()
    if not value or "\x00" in value:
        return None
    path = Path(value)
    if path.is_absolute():
        try:
            return path.relative_to(mirror_root).as_posix()
        except ValueError:
            return None
    if exact_scope is not None and (len(path.parts) == 1 or path == exact_scope.name):
        return exact_scope.as_posix()
    if path.parts and path.parts[0] == mirror_root.name:
        path = Path(*path.parts[1:])
    if ".." in path.parts or not path.parts:
        return None
    return path.as_posix()


def ast_grep_search(
    root: Path,
    *,
    pattern: str,
    lang: str,
    path: str | None = None,
    max_results: int = 40,
    timeout_seconds: float = 8.0,
) -> dict:
    """Agent-facing structural (AST) search: find code matching ``pattern`` in ``lang``.

    Read-only, repo-scoped, no shell. Returns compact {file,line,text} hits — precise
    structural matching BM25 cannot do (refactor/audit queries). Fails soft.
    """
    # validate inputs first (always enforced, even when ast-grep is absent)
    lang_norm = _SG_LANG_ALIAS.get(str(lang or "").strip().lower(), str(lang or "").strip().lower())
    if lang_norm not in _SG_LANGS:
        return {"ok": False, "reason": f"unsupported lang: {lang}", "matches": []}
    pat = str(pattern or "").strip()
    if not pat:
        return {"ok": False, "reason": "empty pattern", "matches": []}
    if "\x00" in pat:
        return {"ok": False, "reason": "invalid pattern control character", "matches": []}
    if len(pat) > AST_PATTERN_MAX_CHARS:
        return {"ok": False, "reason": "pattern too long", "matches": []}
    scope_rel, path_reason = _normalise_scope_path(path)
    if path_reason:
        return {"ok": False, "reason": path_reason, "matches": []}
    result_limit = _normalise_result_limit(max_results, default=40)
    timeout = _normalise_timeout(timeout_seconds, default=8.0)
    root = Path(os.path.abspath(root))
    if not astgrep_available():
        return {"ok": False, "reason": "ast-grep not installed", "matches": []}
    rule_yaml = "id: cb-search\nlanguage: {lang}\nrule:\n  pattern: |\n    {pat}\n".format(
        lang=lang_norm, pat=pat.replace("\n", "\n    "))
    from .redact import redact_value
    from .search import MAX_TEXT_BYTES, _is_indexable_text_file, iter_text_files

    exact_scope: Path | None = None
    if scope_rel is not None:
        scoped_source = root / scope_rel
        if _is_indexable_text_file(root, scoped_source):
            exact_scope = scope_rel
        else:
            try:
                validate_root_confined_directory(
                    scoped_source,
                    root=root,
                    require_safe_permissions=True,
                )
            except OSError:
                return {"ok": False, "reason": "path unavailable", "matches": []}

    suffixes = _SG_LANG_SUFFIXES.get(lang_norm, set())
    with tempfile.TemporaryDirectory(prefix="cb-astgrep-search-") as tmp:
        mirror_root = Path(tmp) / "workspace"
        mirror_root.mkdir(mode=0o700)
        copied_files = 0
        copied_bytes = 0
        overflow = False
        for source in iter_text_files(root):
            try:
                rel = source.relative_to(root)
            except ValueError:
                continue
            if exact_scope is not None:
                if rel != exact_scope:
                    continue
            elif scope_rel is not None and scope_rel not in rel.parents:
                continue
            if suffixes and source.suffix.casefold() not in suffixes:
                continue
            try:
                content, state = read_root_confined_text(
                    source,
                    root=root,
                    max_bytes=MAX_TEXT_BYTES,
                    require_private=False,
                    require_owner=True,
                    reject_group_other_writable=True,
                )
            except (OSError, UnicodeDecodeError):
                continue
            encoded_size = len(content.encode("utf-8"))
            if (
                copied_files >= AST_MATERIALIZE_MAX_FILES
                or copied_bytes + encoded_size > AST_MATERIALIZE_MAX_BYTES
            ):
                overflow = True
                break
            destination = mirror_root / rel
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_text(content, encoding="utf-8")
            if os.name != "nt":
                destination.chmod(0o600)
            copied_files += 1
            copied_bytes += int(state.st_size)
        if overflow:
            return {"ok": False, "reason": "search scope too large", "matches": []}
        if copied_files == 0:
            return {"ok": True, "count": 0, "lang": lang_norm, "matches": []}
        if exact_scope is not None:
            target = mirror_root / exact_scope
        elif scope_rel is not None:
            target = mirror_root / scope_rel
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            target = mirror_root
        findings = scan_path(target, rule_yaml, timeout_seconds=timeout)
        matches: list[dict] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            rel = _finding_repo_path(
                finding.get("file"),
                mirror_root=mirror_root,
                exact_scope=exact_scope,
            )
            if rel is None:
                continue
            rng = finding.get("range") if isinstance(finding.get("range"), dict) else None
            start = (rng or {}).get("start") if isinstance(rng, dict) else None
            line = (start or {}).get("line") if isinstance(start, dict) else None
            text = str(redact_value(str(finding.get("text", ""))))[:200]
            matches.append(
                {
                    "file": rel,
                    "line": (line + 1) if isinstance(line, int) and line >= 0 else None,
                    "text": text,
                }
            )
            if len(matches) >= result_limit:
                break
        return {"ok": True, "count": len(matches), "lang": lang_norm, "matches": matches}


# ---------------------------------------------------------------------------
# Multi-language symbol / call extraction
#
# HISTORY (why this is kind-based and table-driven):
# The first implementation used `pattern:` rules like `fn $FUNC_NAME($_) { $$$BODY }`
# and then read `finding["matches"][*]["start"]`. Both halves were wrong:
#
#   1. `$_` matches exactly ONE node, so an arity-1 pattern silently skipped
#      zero-arg and multi-arg functions. Measured against a 5-function Rust file
#      the old rule matched 0.
#   2. ast-grep `--json=stream` emits ONE object per match with the location under
#      `range.start` / `range.end`. There is no `matches` key at all, so every
#      extractor returned [] even with the binary installed.
#
# The combination meant the graph layer was a no-op for JS/TS/Go/Rust whether or
# not ast-grep was present, and nothing failed loudly. Node `kind` (tree-sitter
# node type) is arity-independent and covers methods, so we match on kind and
# recover the identifier from the matched text.
# ---------------------------------------------------------------------------

# `const f = (x) => ...` / `const f = function (x) {}` declare a callable but are
# `variable_declarator` nodes. `has:` restricts the match to declarators whose
# value is a function, so plain `const x = 1` is not mistaken for a symbol.
_JS_FUNCTION_VALUED_VARIABLE_RULE = """\
---
id: cb-{language}-function-valued-variable
language: {language}
rule:
  kind: variable_declarator
  has:
    any:
      - kind: arrow_function
      - kind: function_expression
severity: info
message: node
"""

# Alternatives, in priority order: `function foo`/`class Foo`; then `foo = (x) =>`
# for function-valued variables; then a bare `foo(` for method definitions.
_JS_NAME_RE = (
    r"(?:function|class)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*([A-Za-z_$][\w$]*)\s*(?::[^=]+)?="
    r"|^\s*(?:static\s+|async\s+|public\s+|private\s+|protected\s+|readonly\s+|get\s+|set\s+)*"
    r"([A-Za-z_$][\w$]*)\s*[(<]"
)

# tree-sitter node kinds per language, verified empirically against ast-grep 0.45.2.
# symbol_kinds: node kinds that denote a declared function/method.
# name_re:      first non-empty capturing group yields the declared identifier.
# extra_rules:  optional additional rule documents appended to the generated rule
#               file. Needed for JS/TS, where `const f = (x) => ...` is an
#               extremely common declaration style that no function/method node
#               kind covers; a bare `variable_declarator` kind would also match
#               plain data like `const x = 1`, so it is constrained with `has:`
#               to declarators that actually contain a function body.
_SYMBOL_SPECS: dict[str, dict[str, Any]] = {
    "JavaScript": {
        "symbol_kinds": ("function_declaration", "method_definition", "class_declaration"),
        "name_re": _JS_NAME_RE,
        "extra_rules": _JS_FUNCTION_VALUED_VARIABLE_RULE.format(language="JavaScript"),
    },
    "TypeScript": {
        "symbol_kinds": ("function_declaration", "method_definition", "class_declaration"),
        "name_re": _JS_NAME_RE,
        "extra_rules": _JS_FUNCTION_VALUED_VARIABLE_RULE.format(language="TypeScript"),
    },
    "Go": {
        "symbol_kinds": ("function_declaration", "method_declaration"),
        "name_re": r"func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)",
    },
    "Rust": {
        "symbol_kinds": ("function_item",),
        "name_re": r"fn\s+([A-Za-z_]\w*)",
    },
    "Kotlin": {
        "symbol_kinds": ("function_declaration",),
        "name_re": r"fun\s+(?:<[^>]*>\s*)?(?:[A-Za-z_][\w.]*\.)?([A-Za-z_]\w*)",
    },
    # Dart is a first-class ast-grep built-in language. `function_signature` is the
    # declaration header for BOTH top-level functions and class methods, including
    # `=>` arrow bodies, which `function_declaration`/`method_declaration` miss.
    #
    # A signature spans only the header line, so on its own it cannot enclose the
    # body and caller attribution collapses to "<module>". An `inside:` constraint
    # was tried and rejected: it only inspects the direct parent, so class methods
    # (whose parent differs from a top-level function's) were dropped entirely.
    # `body_kind` instead collects body nodes in a second pass and widens each
    # signature to the body that starts on or just after it — see `_extract_symbols`.
    "Dart": {
        "symbol_kinds": ("function_signature",),
        "name_re": r"(?:[\w<>,?\[\]. ]+?\s+)?([A-Za-z_]\w*)\s*\(",
        "body_kind": "function_body",
    },
}

# `call_expression` is the call node kind in every language above (verified).
_CALL_KIND = "call_expression"

# Bound per-file extraction so a pathological file cannot dominate an index run.
AST_EXTRACT_MAX_SYMBOLS = 2_000
AST_EXTRACT_MAX_CALLS = 5_000
AST_EXTRACT_TIMEOUT_SECONDS = 10.0

_CALLEE_RE = re.compile(r"([A-Za-z_$][\w$]*(?:\s*(?:\.|::)\s*[A-Za-z_$][\w$]*)*)\s*[(<]")


def _kind_rule(language: str, kinds: tuple[str, ...]) -> str:
    """Build an ast-grep rule matching any of ``kinds`` in ``language``."""
    if len(kinds) == 1:
        body = f"  kind: {kinds[0]}\n"
    else:
        body = "  any:\n" + "".join(f"    - kind: {kind}\n" for kind in kinds)
    return f"id: cb-{language.lower()}-nodes\nlanguage: {language}\nrule:\n{body}severity: info\nmessage: node\n"


def _finding_span(finding: dict[str, Any]) -> tuple[int, int] | None:
    """1-indexed (start_line, end_line) from a `--json=stream` finding.

    ast-grep reports 0-indexed lines under ``range``. Older code looked for a
    non-existent ``matches`` list; keep reading ``range`` only.
    """
    rng = finding.get("range")
    if not isinstance(rng, dict):
        return None
    start = rng.get("start")
    end = rng.get("end")
    if not isinstance(start, dict):
        return None
    try:
        start_line = int(start.get("line", 0)) + 1
    except (TypeError, ValueError):
        return None
    end_line = start_line
    if isinstance(end, dict):
        try:
            end_line = int(end.get("line", start_line - 1)) + 1
        except (TypeError, ValueError):
            end_line = start_line
    start_line = max(1, start_line)
    return start_line, max(start_line, end_line)


def _meta_var_text(finding: dict[str, Any], name: str) -> str | None:
    meta = finding.get("metaVariables")
    if not isinstance(meta, dict):
        return None
    single = meta.get("single")
    if not isinstance(single, dict):
        return None
    entry = single.get(name)
    if not isinstance(entry, dict):
        return None
    text = entry.get("text")
    return text if isinstance(text, str) and text else None


def _extract_symbols(file_path: str, language: str) -> list[dict]:
    """Kind-based symbol extraction for one ast-grep language."""
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    spec = _SYMBOL_SPECS.get(language)
    if spec is None or not _binary():
        return []

    p = Path(file_path)
    if not p.is_file():
        return []

    rule_yaml = _kind_rule(language, spec["symbol_kinds"]) + spec.get("extra_rules", "")
    findings = scan_path(p, rule_yaml, timeout_seconds=AST_EXTRACT_TIMEOUT_SECONDS)
    name_re = re.compile(spec["name_re"], re.MULTILINE)

    symbols: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        span = _finding_span(finding)
        if span is None:
            continue
        lineno, end_lineno = span
        text = finding.get("text")
        text = text if isinstance(text, str) else ""

        qualname = None
        match = name_re.search(text)
        if match:
            qualname = next((g for g in match.groups() if g), None)
        if not qualname:
            qualname = f"<anonymous at {lineno}>"

        key = (qualname, lineno)
        if key in seen:
            continue
        seen.add(key)

        kind = "class" if text.lstrip().startswith("class") else "function"
        symbols.append(
            {"qualname": qualname, "kind": kind, "lineno": lineno, "end_lineno": end_lineno}
        )
        if len(symbols) >= AST_EXTRACT_MAX_SYMBOLS:
            break

    body_kind = spec.get("body_kind")
    if body_kind and symbols:
        symbols = _widen_to_bodies(p, language, body_kind, symbols)
    return symbols


def _widen_to_bodies(
    path: Path, language: str, body_kind: str, symbols: list[dict]
) -> list[dict]:
    """Extend header-only declarations to cover their body.

    Dart's ``function_signature`` ends at the header, so a call inside the body
    falls outside the symbol span and caller attribution degrades to "<module>".
    Bodies are matched in a second scan and each signature adopts the first body
    that starts on or after its own start line.
    """
    bodies = []
    for finding in scan_path(
        path, _kind_rule(language, (body_kind,)), timeout_seconds=AST_EXTRACT_TIMEOUT_SECONDS
    ):
        if not isinstance(finding, dict):
            continue
        span = _finding_span(finding)
        if span is not None:
            bodies.append(span)
    if not bodies:
        return symbols
    bodies.sort()

    for record in symbols:
        start = record["lineno"]
        for body_start, body_end in bodies:
            if body_start >= start:
                if body_end > record["end_lineno"]:
                    record["end_lineno"] = body_end
                break
    return symbols


def _extract_calls(file_path: str, language: str) -> list[dict]:
    """Kind-based call-site extraction for one ast-grep language."""
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    if language not in _SYMBOL_SPECS or not _binary():
        return []

    p = Path(file_path)
    if not p.is_file():
        return []

    findings = scan_path(
        p,
        _kind_rule(language, (_CALL_KIND,)),
        timeout_seconds=AST_EXTRACT_TIMEOUT_SECONDS,
    )

    calls: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        span = _finding_span(finding)
        if span is None:
            continue
        lineno = span[0]
        text = finding.get("text")
        text = text if isinstance(text, str) else ""

        callee = _meta_var_text(finding, "FUNC")
        if not callee:
            match = _CALLEE_RE.match(text.lstrip())
            if match:
                callee = match.group(1)
            else:
                callee = text.split("(", 1)[0].strip()
        callee = re.sub(r"\s+", "", callee or "")
        # Keep only the final segment so `self.foo()` / `mod::foo()` join on `foo`,
        # matching how the Python extractor records callees.
        if callee:
            callee = re.split(r"\.|::", callee)[-1]
        if not callee or not callee.isidentifier():
            continue

        calls.append({"callee": callee, "lineno": lineno})
        if len(calls) >= AST_EXTRACT_MAX_CALLS:
            break
    return calls


def extract_symbols_js(file_path: str) -> list[dict]:
    """Extract function/class symbols from a JavaScript file."""
    return _extract_symbols(file_path, "JavaScript")


def extract_calls_js(file_path: str) -> list[dict]:
    """Extract call sites from a JavaScript file."""
    return _extract_calls(file_path, "JavaScript")


def extract_symbols_ts(file_path: str) -> list[dict]:
    """Extract function/class symbols from a TypeScript file."""
    return _extract_symbols(file_path, "TypeScript")


def extract_calls_ts(file_path: str) -> list[dict]:
    """Extract call sites from a TypeScript file."""
    return _extract_calls(file_path, "TypeScript")


def extract_symbols_go(file_path: str) -> list[dict]:
    """Extract function/method symbols from a Go file."""
    return _extract_symbols(file_path, "Go")


def extract_calls_go(file_path: str) -> list[dict]:
    """Extract call sites from a Go file."""
    return _extract_calls(file_path, "Go")


def extract_symbols_rs(file_path: str) -> list[dict]:
    """Extract function/method symbols from a Rust file."""
    return _extract_symbols(file_path, "Rust")


def extract_calls_rs(file_path: str) -> list[dict]:
    """Extract call sites from a Rust file."""
    return _extract_calls(file_path, "Rust")


def extract_symbols_kt(file_path: str) -> list[dict]:
    """Extract function symbols from a Kotlin file."""
    return _extract_symbols(file_path, "Kotlin")


def extract_calls_kt(file_path: str) -> list[dict]:
    """Extract call sites from a Kotlin file."""
    return _extract_calls(file_path, "Kotlin")


def extract_symbols_dart(file_path: str) -> list[dict]:
    """Extract function/method symbols from a Dart file."""
    return _extract_symbols(file_path, "Dart")


def extract_calls_dart(file_path: str) -> list[dict]:
    """Extract call sites from a Dart file."""
    return _extract_calls(file_path, "Dart")


__all__ = [
    "astgrep_available",
    "scan_path",
    "extract_symbols_js",
    "extract_calls_js",
    "extract_symbols_ts",
    "extract_calls_ts",
    "extract_symbols_go",
    "extract_calls_go",
    "extract_symbols_rs",
    "extract_calls_rs",
    "extract_symbols_kt",
    "extract_calls_kt",
    "extract_symbols_dart",
    "extract_calls_dart",
]
