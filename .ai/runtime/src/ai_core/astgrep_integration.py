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
import subprocess
import tempfile
from pathlib import Path


def astgrep_available() -> bool:
    """True iff an ``ast-grep`` or ``sg`` binary is on PATH."""
    return bool(shutil.which("ast-grep") or shutil.which("sg"))


def _binary() -> str | None:
    return shutil.which("ast-grep") or shutil.which("sg")


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
    if not p.exists():
        return []

    yaml_body = rule_yaml if rule_yaml is not None else _DEFAULT_RULES

    findings: list[dict] = []
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
        try:
            proc = subprocess.run(
                cmd,
                timeout=timeout_seconds,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

        stdout = proc.stdout or ""
        # ast-grep --json=stream emits NDJSON. Older versions emit a single
        # JSON array — handle both.
        stripped = stdout.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                return []
            return []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                findings.append(obj)
    return findings


_SG_LANGS = {
    "python", "py", "javascript", "js", "typescript", "ts", "tsx", "jsx",
    "go", "rust", "rs", "java", "c", "cpp", "ruby", "php", "kotlin", "swift", "scala",
}
_SG_LANG_ALIAS = {"py": "python", "js": "javascript", "ts": "typescript", "rs": "rust"}


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
    # scope target strictly inside the repo (no traversal outside root)
    root = Path(root).resolve()
    target = root
    if path:
        cand = (root / path).resolve()
        if root == cand or root in cand.parents:
            target = cand
        else:
            return {"ok": False, "reason": "path escapes repo", "matches": []}
    if not astgrep_available():
        return {"ok": False, "reason": "ast-grep not installed", "matches": []}
    rule_yaml = "id: cb-search\nlanguage: {lang}\nrule:\n  pattern: |\n    {pat}\n".format(
        lang=lang_norm, pat=pat.replace("\n", "\n    "))
    from .redact import redact_value

    findings = scan_path(target, rule_yaml, timeout_seconds=timeout_seconds)
    matches: list[dict] = []
    for f in findings[: max(1, int(max_results))]:
        rng = f.get("range") if isinstance(f, dict) else None
        start = (rng or {}).get("start") if isinstance(rng, dict) else None
        line = (start or {}).get("line") if isinstance(start, dict) else None
        rel = str(f.get("file", ""))
        text = str(redact_value(str(f.get("text", ""))))[:200]
        matches.append({"file": rel, "line": (line + 1) if isinstance(line, int) else None, "text": text})
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

    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cb-astgrep-") as tmp:
        rule_file = Path(tmp) / "rules.yml"
        try:
            rule_file.write_text(rule_yaml, encoding="utf-8")
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
        try:
            proc = subprocess.run(
                cmd,
                timeout=10.0,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

        # Parse best-effort: extract lineno from matched nodes
        stdout = proc.stdout or ""
        stripped = stdout.strip()
        if not stripped:
            return []

        # Handle both NDJSON and JSON array formats
        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                if isinstance(data, list):
                    findings = [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                return []
        else:
            for line in stripped.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        findings.append(obj)
                except json.JSONDecodeError:
                    continue

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

    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cb-astgrep-") as tmp:
        rule_file = Path(tmp) / "rules.yml"
        try:
            rule_file.write_text(rule_yaml, encoding="utf-8")
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
        try:
            proc = subprocess.run(
                cmd,
                timeout=10.0,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

        stdout = proc.stdout or ""
        stripped = stdout.strip()
        if not stripped:
            return []

        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                if isinstance(data, list):
                    findings = [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                return []
        else:
            for line in stripped.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        findings.append(obj)
                except json.JSONDecodeError:
                    continue

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

    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cb-astgrep-") as tmp:
        rule_file = Path(tmp) / "rules.yml"
        try:
            rule_file.write_text(rule_yaml, encoding="utf-8")
        except OSError:
            return []

        cmd = [binary, "scan", "--rule", str(rule_file), "--json=stream", str(p)]
        try:
            proc = subprocess.run(
                cmd, timeout=10.0, capture_output=True, text=True, check=False, shell=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

        stdout = proc.stdout or ""
        stripped = stdout.strip()
        if not stripped:
            return []

        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                findings = [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                return []
        else:
            for line in stripped.splitlines():
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            findings.append(obj)
                    except json.JSONDecodeError:
                        pass

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

    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cb-astgrep-") as tmp:
        rule_file = Path(tmp) / "rules.yml"
        try:
            rule_file.write_text(rule_yaml, encoding="utf-8")
        except OSError:
            return []

        cmd = [binary, "scan", "--rule", str(rule_file), "--json=stream", str(p)]
        try:
            proc = subprocess.run(
                cmd, timeout=10.0, capture_output=True, text=True, check=False, shell=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

        stdout = proc.stdout or ""
        stripped = stdout.strip()
        if not stripped:
            return []

        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                findings = [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                return []
        else:
            for line in stripped.splitlines():
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            findings.append(obj)
                    except json.JSONDecodeError:
                        pass

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

    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cb-astgrep-") as tmp:
        rule_file = Path(tmp) / "rules.yml"
        try:
            rule_file.write_text(rule_yaml, encoding="utf-8")
        except OSError:
            return []

        cmd = [binary, "scan", "--rule", str(rule_file), "--json=stream", str(p)]
        try:
            proc = subprocess.run(
                cmd, timeout=10.0, capture_output=True, text=True, check=False, shell=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

        stdout = proc.stdout or ""
        stripped = stdout.strip()
        if not stripped:
            return []

        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                findings = [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                return []
        else:
            for line in stripped.splitlines():
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            findings.append(obj)
                    except json.JSONDecodeError:
                        pass

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

    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cb-astgrep-") as tmp:
        rule_file = Path(tmp) / "rules.yml"
        try:
            rule_file.write_text(rule_yaml, encoding="utf-8")
        except OSError:
            return []

        cmd = [binary, "scan", "--rule", str(rule_file), "--json=stream", str(p)]
        try:
            proc = subprocess.run(
                cmd, timeout=10.0, capture_output=True, text=True, check=False, shell=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

        stdout = proc.stdout or ""
        stripped = stdout.strip()
        if not stripped:
            return []

        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                findings = [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                return []
        else:
            for line in stripped.splitlines():
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            findings.append(obj)
                    except json.JSONDecodeError:
                        pass

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
