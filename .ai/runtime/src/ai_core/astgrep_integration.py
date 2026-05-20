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


__all__ = ["astgrep_available", "scan_path"]
