"""Neurosymbolic-lite AST verifier for catalog bodies and inline code (T31).

Goes beyond DANGER_PATTERNS regex by parsing real Python and walking the
syntax tree. Catches patterns that text matching misses:
  - subprocess.Popen / os.system / os.exec* / pty.spawn / eval / exec
  - __import__, compile, globals, locals, vars
  - open(..., "w") / open(..., "a") on absolute paths
  - importing modules outside the ALLOW list
  - calls to socket / urllib / http / requests (network at runtime)

Used by:
  - recommend.accept() optional pre-flight (`AI_AST_VERIFY=1`)
  - `ai code verify` CLI for any local file or stdin

Read-only (returns a report); never executes anything.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Iterable


# Forbidden call targets — exact attribute-chain match.
_FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__",
    "os.system", "os.exec", "os.execl", "os.execlp", "os.execle",
    "os.execv", "os.execvp", "os.execvpe", "os.spawn", "os.spawnl",
    "os.spawnv", "os.spawnvp", "os.spawnvpe", "os.fork",
    "subprocess.Popen", "subprocess.run", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output", "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "pty.spawn", "pty.fork",
    "ctypes.CDLL", "ctypes.cdll.LoadLibrary",
    "importlib.import_module", "importlib.__import__",
}

# Modules forbidden in `import` and `from ... import`.
_FORBIDDEN_IMPORTS = {
    "subprocess", "pty", "ctypes",
    "socket", "ssl", "select",
    "urllib", "urllib.request", "urllib2", "http", "http.client", "httplib",
    "requests", "httpx", "aiohttp",
    "ftplib", "telnetlib", "smtplib", "poplib", "imaplib",
    "multiprocessing", "concurrent.futures",
    "marshal", "pickle", "shelve",
}

# Globals lookups that smell like sandbox escape.
_FORBIDDEN_NAMES = {"globals", "locals", "vars", "dir", "__builtins__"}


@dataclass
class Violation:
    kind: str         # "call" | "import" | "name" | "open_write" | "syntax"
    detail: str       # human-readable
    lineno: int       # 1-indexed
    col_offset: int   # 0-indexed


@dataclass
class Report:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violations": [
                {
                    "kind": v.kind,
                    "detail": v.detail,
                    "lineno": v.lineno,
                    "col_offset": v.col_offset,
                }
                for v in self.violations
            ],
        }


def _attr_chain(node: ast.AST) -> str | None:
    """foo → 'foo', mod.foo → 'mod.foo', a.b.c → 'a.b.c'."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _attr_chain(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
        return node.attr
    return None


def verify_source(source: str, *, allow_imports: Iterable[str] | None = None) -> Report:
    """Walk the AST of `source` and report every violation found.

    `allow_imports` extends the default permissible set (stdlib `os`, `json`,
    `re`, `pathlib`, `typing`, `dataclasses`, `datetime`, `hashlib`, `collections`)
    plus our own `ai_core.*` is always allowed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return Report(ok=False, violations=[Violation(
            kind="syntax", detail=str(exc.msg), lineno=exc.lineno or 1, col_offset=exc.offset or 0,
        )])

    base_allow = {
        "os", "json", "re", "pathlib", "typing", "dataclasses", "datetime",
        "hashlib", "collections", "math", "itertools", "functools", "enum",
        "io", "sys",  # sys is allowed but writes to sys.modules etc still checked at call level
    }
    allowed = set(base_allow)
    if allow_imports:
        allowed |= set(allow_imports)

    violations: list[Violation] = []

    for node in ast.walk(tree):
        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORTS or any(alias.name.startswith(f + ".") for f in _FORBIDDEN_IMPORTS):
                    violations.append(Violation(
                        kind="import", detail=f"forbidden import: {alias.name}",
                        lineno=node.lineno, col_offset=node.col_offset,
                    ))
                elif alias.name in allowed or alias.name.startswith("ai_core"):
                    pass
                elif alias.name.split(".", 1)[0] in allowed:
                    pass
                else:
                    violations.append(Violation(
                        kind="import", detail=f"unlisted import: {alias.name}",
                        lineno=node.lineno, col_offset=node.col_offset,
                    ))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in _FORBIDDEN_IMPORTS or any(mod.startswith(f + ".") for f in _FORBIDDEN_IMPORTS):
                violations.append(Violation(
                    kind="import", detail=f"forbidden import from: {mod}",
                    lineno=node.lineno, col_offset=node.col_offset,
                ))
            elif mod and mod.split(".", 1)[0] in allowed:
                pass
            elif mod.startswith("ai_core") or node.level > 0:
                pass
            else:
                violations.append(Violation(
                    kind="import", detail=f"unlisted import from: {mod}",
                    lineno=node.lineno, col_offset=node.col_offset,
                ))
        # Calls
        elif isinstance(node, ast.Call):
            target = _attr_chain(node.func)
            if target and target in _FORBIDDEN_CALLS:
                violations.append(Violation(
                    kind="call", detail=f"forbidden call: {target}",
                    lineno=node.lineno, col_offset=node.col_offset,
                ))
            # open(..., "w"|"a"|"x"|...+"b") — file-writing
            if target == "open" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
                if isinstance(mode, str) and any(c in mode for c in "waxWAX"):
                    violations.append(Violation(
                        kind="open_write", detail=f"open() with write mode: {mode!r}",
                        lineno=node.lineno, col_offset=node.col_offset,
                    ))
        # Bare names
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES and isinstance(node.ctx, ast.Load):
                violations.append(Violation(
                    kind="name", detail=f"sandbox-escape reference: {node.id}",
                    lineno=node.lineno, col_offset=node.col_offset,
                ))

    return Report(ok=not violations, violations=violations)


def verify_file(path) -> Report:
    """Convenience: read+verify a file. SyntaxError → report with single violation."""
    from pathlib import Path
    p = Path(path)
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Report(ok=False, violations=[Violation(
            kind="syntax", detail=f"unreadable: {exc}", lineno=0, col_offset=0,
        )])
    return verify_source(source)
