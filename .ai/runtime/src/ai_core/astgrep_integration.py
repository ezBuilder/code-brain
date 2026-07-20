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


def extract_symbols_js(file_path: str) -> list[dict]:
    """Extract function/class symbols from JS/TS file using ast-grep.

    Returns list of dicts with keys: qualname, kind, lineno, end_lineno.
    Returns [] if ast-grep is unavailable or parse fails.
    """
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    binary = _binary()
    if not binary:
        return []

    p = Path(file_path)
    if not p.exists():
        return []

    # ast-grep pattern for function declarations and arrow functions
    rule_yaml = """\
id: js-functions
language: JavaScript
rule:
  pattern: |
    function $FUNC_NAME($_) {
      $$$BODY
    }
severity: info
message: function
---
id: js-arrow-functions
language: JavaScript
rule:
  pattern: const $VAR = $_
severity: info
message: arrow-function
---
id: js-class
language: JavaScript
rule:
  pattern: |
    class $CLASS_NAME {
      $$$BODY
    }
severity: info
message: class
"""

    findings = scan_path(p, rule_yaml, timeout_seconds=10.0)

    # Transform ast-grep findings into symbol records
    symbols: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        matches = finding.get("matches", [])
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            # Attempt to extract function/class name from matched text
            start = match.get("start", {})
            end = match.get("end", {})
            lineno = start.get("line", 0) if isinstance(start, dict) else 0
            end_lineno = end.get("line", lineno) if isinstance(end, dict) else lineno
            # Increment because ast-grep uses 0-indexed lines, we want 1-indexed
            lineno = max(1, lineno + 1)
            end_lineno = max(lineno, end_lineno + 1)

            # Best-effort: extract identifier from matched region
            text = match.get("text", "")
            kind = finding.get("message", "function").lower()

            # Heuristic: try to extract function name
            import re as _re
            name_match = _re.search(r'(?:function|const|class)\s+(\w+)', text)
            if name_match:
                qualname = name_match.group(1)
            else:
                qualname = f"<anonymous at {lineno}>"

            symbols.append({
                "qualname": qualname,
                "kind": kind,
                "lineno": lineno,
                "end_lineno": end_lineno,
            })

    return symbols


def extract_calls_js(file_path: str) -> list[dict]:
    """Extract function call sites from JS/TS file using ast-grep.

    Returns list of dicts with keys: callee, lineno.
    Returns [] if ast-grep is unavailable or parse fails.
    """
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    binary = _binary()
    if not binary:
        return []

    p = Path(file_path)
    if not p.exists():
        return []

    # Pattern to match function calls
    rule_yaml = """\
id: js-calls
language: JavaScript
rule:
  pattern: $FUNC($_)
severity: info
message: call
"""

    findings = scan_path(p, rule_yaml, timeout_seconds=10.0)

    calls: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        matches = finding.get("matches", [])
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            start = match.get("start", {})
            lineno = start.get("line", 0) if isinstance(start, dict) else 0
            lineno = max(1, lineno + 1)

            # Extract callee name from matched text
            text = match.get("text", "")
            # Heuristic: function call pattern "name(...)" → extract "name"
            import re as _re
            callee_match = _re.search(r'(\w+(?:\.\w+)*)\s*\(', text)
            if callee_match:
                callee = callee_match.group(1)
            else:
                callee = text.split('(')[0].strip()

            if callee:
                calls.append({
                    "callee": callee,
                    "lineno": lineno,
                })

    return calls


def extract_symbols_ts(file_path: str) -> list[dict]:
    """Extract symbols from TypeScript file. Delegates to JS extraction."""
    return extract_symbols_js(file_path)


def extract_calls_ts(file_path: str) -> list[dict]:
    """Extract calls from TypeScript file. Delegates to JS extraction."""
    return extract_calls_js(file_path)


def extract_symbols_go(file_path: str) -> list[dict]:
    """Extract function/method symbols from Go file using ast-grep.

    Returns [] if ast-grep unavailable.
    """
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    binary = _binary()
    if not binary:
        return []

    p = Path(file_path)
    if not p.exists():
        return []

    rule_yaml = """\
id: go-functions
language: Go
rule:
  pattern: |
    func $FUNC_NAME($_) {
      $$$BODY
    }
severity: info
message: function
"""

    findings = scan_path(p, rule_yaml, timeout_seconds=10.0)

    symbols: list[dict] = []
    for finding in findings:
        matches = finding.get("matches", [])
        for match in matches:
            if not isinstance(match, dict):
                continue
            start = match.get("start", {})
            end = match.get("end", {})
            lineno = start.get("line", 0) if isinstance(start, dict) else 0
            end_lineno = end.get("line", lineno) if isinstance(end, dict) else lineno
            lineno = max(1, lineno + 1)
            end_lineno = max(lineno, end_lineno + 1)

            text = match.get("text", "")
            import re as _re
            name_match = _re.search(r'func\s+\(?\s*\w+\s*\)?\s*(\w+)', text)
            if name_match:
                qualname = name_match.group(1)
            else:
                qualname = f"<anonymous at {lineno}>"

            symbols.append({
                "qualname": qualname,
                "kind": "function",
                "lineno": lineno,
                "end_lineno": end_lineno,
            })

    return symbols


def extract_calls_go(file_path: str) -> list[dict]:
    """Extract call sites from Go file using ast-grep."""
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    binary = _binary()
    if not binary:
        return []

    p = Path(file_path)
    if not p.exists():
        return []

    rule_yaml = """\
id: go-calls
language: Go
rule:
  pattern: $FUNC($_)
severity: info
message: call
"""

    findings = scan_path(p, rule_yaml, timeout_seconds=10.0)

    calls: list[dict] = []
    for finding in findings:
        matches = finding.get("matches", [])
        for match in matches:
            if not isinstance(match, dict):
                continue
            start = match.get("start", {})
            lineno = start.get("line", 0) if isinstance(start, dict) else 0
            lineno = max(1, lineno + 1)

            text = match.get("text", "")
            import re as _re
            callee_match = _re.search(r'(\w+(?:\.\w+)*)\s*\(', text)
            if callee_match:
                callee = callee_match.group(1)
            else:
                callee = text.split('(')[0].strip()

            if callee:
                calls.append({"callee": callee, "lineno": lineno})

    return calls


def extract_symbols_rs(file_path: str) -> list[dict]:
    """Extract function/method symbols from Rust file using ast-grep."""
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    binary = _binary()
    if not binary:
        return []

    p = Path(file_path)
    if not p.exists():
        return []

    rule_yaml = """\
id: rust-functions
language: Rust
rule:
  pattern: |
    fn $FUNC_NAME($_) {
      $$$BODY
    }
severity: info
message: function
"""

    findings = scan_path(p, rule_yaml, timeout_seconds=10.0)

    symbols: list[dict] = []
    for finding in findings:
        matches = finding.get("matches", [])
        for match in matches:
            if not isinstance(match, dict):
                continue
            start = match.get("start", {})
            end = match.get("end", {})
            lineno = start.get("line", 0) if isinstance(start, dict) else 0
            end_lineno = end.get("line", lineno) if isinstance(end, dict) else lineno
            lineno = max(1, lineno + 1)
            end_lineno = max(lineno, end_lineno + 1)

            text = match.get("text", "")
            import re as _re
            name_match = _re.search(r'fn\s+(\w+)', text)
            if name_match:
                qualname = name_match.group(1)
            else:
                qualname = f"<anonymous at {lineno}>"

            symbols.append({
                "qualname": qualname,
                "kind": "function",
                "lineno": lineno,
                "end_lineno": end_lineno,
            })

    return symbols


def extract_calls_rs(file_path: str) -> list[dict]:
    """Extract call sites from Rust file using ast-grep."""
    if os.environ.get("AI_ASTGREP_DISABLE") == "1":
        return []
    binary = _binary()
    if not binary:
        return []

    p = Path(file_path)
    if not p.exists():
        return []

    rule_yaml = """\
id: rust-calls
language: Rust
rule:
  pattern: $FUNC($_)
severity: info
message: call
"""

    findings = scan_path(p, rule_yaml, timeout_seconds=10.0)

    calls: list[dict] = []
    for finding in findings:
        matches = finding.get("matches", [])
        for match in matches:
            if not isinstance(match, dict):
                continue
            start = match.get("start", {})
            lineno = start.get("line", 0) if isinstance(start, dict) else 0
            lineno = max(1, lineno + 1)

            text = match.get("text", "")
            import re as _re
            callee_match = _re.search(r'(\w+(?::\w+)*)\s*\(', text)
            if callee_match:
                callee = callee_match.group(1)
            else:
                callee = text.split('(')[0].strip()

            if callee:
                calls.append({"callee": callee, "lineno": lineno})

    return calls


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
]
