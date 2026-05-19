"""Function-call graph extraction for code search.

Phase 1: Python only. Walks a project tree, parses each .py file with the
stdlib `ast` module (zero new deps), and emits two streams:

  - Symbols: top-level + nested function/method/class definitions with their
    (path, lineno, end_lineno, qualname) so chunking can split at function
    boundaries instead of file boundaries.
  - Calls: (caller_qualname, callee_name, call_site_lineno) edges. Callee
    resolution is best-effort lexical — fully-qualified resolution requires
    cross-file import tracking which lands in a later step.

This module is read-only & pure-Python. No write paths, no network. Designed
so the search indexer can opt in via AI_SEARCH_CODEGRAPH=1 once integration
lands in step C.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class Symbol:
    """A function, async function, method, or class definition."""
    path: str            # project-relative
    qualname: str        # e.g. "module.ClassName.method_name"
    kind: str            # "function" | "async_function" | "method" | "async_method" | "class"
    lineno: int          # 1-indexed
    end_lineno: int      # 1-indexed (inclusive)
    parent: str | None = None  # parent qualname or None for module-level

    def loc_count(self) -> int:
        return max(1, self.end_lineno - self.lineno + 1)


@dataclass
class CallEdge:
    """A call site: `caller` invokes `callee` at line `lineno`.

    `callee` is the lexical attribute chain at the call site (best-effort):
      - foo()              → "foo"
      - module.foo()       → "module.foo"
      - self.foo()         → "self.foo"
      - obj.attr.foo()     → "obj.attr.foo"
    Cross-file binding resolution is intentionally deferred.
    """
    path: str
    caller: str          # qualname of enclosing function/method, or "<module>"
    callee: str
    lineno: int


def extract_symbols(source: str, *, path: str) -> list[Symbol]:
    """Parse `source` as Python and emit all Function/Class symbols.

    Returns [] if `source` is not valid Python (best-effort indexer behavior).
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    out: list[Symbol] = []
    _walk_symbols(tree, path=path, parent_qn=None, scope_kind=None, out=out)
    return out


def _walk_symbols(
    node: ast.AST,
    *,
    path: str,
    parent_qn: str | None,
    scope_kind: str | None,
    out: list[Symbol],
) -> None:
    for child in ast.iter_child_nodes(node):
        name = getattr(child, "name", None)
        if isinstance(child, ast.ClassDef):
            qn = f"{parent_qn}.{name}" if parent_qn else name
            out.append(Symbol(
                path=path, qualname=qn, kind="class",
                lineno=child.lineno, end_lineno=child.end_lineno or child.lineno,
                parent=parent_qn,
            ))
            _walk_symbols(child, path=path, parent_qn=qn, scope_kind="class", out=out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qn = f"{parent_qn}.{name}" if parent_qn else name
            if scope_kind == "class":
                kind = "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method"
            else:
                kind = "async_function" if isinstance(child, ast.AsyncFunctionDef) else "function"
            out.append(Symbol(
                path=path, qualname=qn, kind=kind,
                lineno=child.lineno, end_lineno=child.end_lineno or child.lineno,
                parent=parent_qn,
            ))
            _walk_symbols(child, path=path, parent_qn=qn, scope_kind=kind, out=out)
        else:
            # Descend without changing scope (e.g. ast.If, ast.For, ast.With branches).
            _walk_symbols(child, path=path, parent_qn=parent_qn, scope_kind=scope_kind, out=out)


def extract_calls(source: str, *, path: str) -> list[CallEdge]:
    """Emit call edges from `source`. Returns [] on SyntaxError."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    out: list[CallEdge] = []
    _walk_calls(tree, path=path, caller_stack=["<module>"], out=out)
    return out


def _walk_calls(
    node: ast.AST,
    *,
    path: str,
    caller_stack: list[str],
    out: list[CallEdge],
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            qn = f"{caller_stack[-1]}.{child.name}" if caller_stack[-1] != "<module>" else child.name
            _walk_calls(child, path=path, caller_stack=caller_stack + [qn], out=out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qn = f"{caller_stack[-1]}.{child.name}" if caller_stack[-1] != "<module>" else child.name
            _walk_calls(child, path=path, caller_stack=caller_stack + [qn], out=out)
        elif isinstance(child, ast.Call):
            callee = _resolve_call_target(child.func)
            if callee:
                out.append(CallEdge(
                    path=path,
                    caller=caller_stack[-1],
                    callee=callee,
                    lineno=child.lineno,
                ))
            _walk_calls(child, path=path, caller_stack=caller_stack, out=out)
        else:
            _walk_calls(child, path=path, caller_stack=caller_stack, out=out)


def _resolve_call_target(node: ast.AST) -> str | None:
    """Resolve `foo`, `mod.foo`, `obj.attr.foo`, `self.foo` lexically."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _resolve_call_target(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
        return node.attr
    return None


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield project Python files, skipping common cache/venv dirs and hidden files."""
    skip_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".ai"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn
