"""Function-call graph extraction for code search.

Phase 1: Python only. Walks a project tree, parses each .py file with the
stdlib `ast` module (zero new deps), and emits two streams:

  - Symbols: top-level + nested function/method/class definitions with their
    (path, lineno, end_lineno, qualname) so chunking can split at function
    boundaries instead of file boundaries.
  - Calls: (caller_qualname, callee_name, call_site_lineno) edges. Python calls
    carry the original lexical target plus deterministic syntactic resolution
    provenance for import aliases, same-file symbols, and self/cls members.

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
    `target` retains the best fully-qualified syntactic target when available;
    `callee` remains graph-compatible with local symbol qualnames.
    """
    path: str
    caller: str          # qualname of enclosing function/method, or "<module>"
    callee: str
    lineno: int
    lexical_callee: str | None = None
    target: str | None = None
    resolution: str = "lexical"
    confidence: float = 0.45


@dataclass(frozen=True)
class Reference:
    """A bounded syntactic identifier use with resolution provenance."""

    path: str
    scope: str
    name: str
    lexical_name: str
    kind: str
    lineno: int
    column: int
    end_lineno: int
    end_column: int
    target: str | None = None
    resolution: str = "lexical"
    confidence: float = 0.45


@dataclass(frozen=True)
class _ImportBinding:
    target: str
    graph_prefix: str
    resolution: str
    confidence: float


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
    symbols = extract_symbols(source, path=path)
    symbol_qualnames = {symbol.qualname for symbol in symbols}
    class_qualnames = {symbol.qualname for symbol in symbols if symbol.kind == "class"}
    out: list[CallEdge] = []
    _walk_calls(
        tree,
        path=path,
        caller_stack=["<module>"],
        aliases=_scope_import_bindings(tree, path=path),
        symbol_qualnames=symbol_qualnames,
        class_qualnames=class_qualnames,
        out=out,
    )
    return out


def extract_references(source: str, *, path: str) -> list[Reference]:
    """Extract call and non-call identifier uses from one Python source file.

    Calls are indexed once as ``kind=call``. Attribute and name loads outside
    call targets are indexed separately, avoiding the duplicate ``obj`` and
    ``obj.attr`` rows produced by a naive ``ast.walk`` implementation.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    symbols = extract_symbols(source, path=path)
    symbol_qualnames = {symbol.qualname for symbol in symbols}
    class_qualnames = {symbol.qualname for symbol in symbols if symbol.kind == "class"}
    out: list[Reference] = []
    _walk_references(
        tree,
        path=path,
        scope_stack=["<module>"],
        aliases=_scope_import_bindings(tree, path=path),
        symbol_qualnames=symbol_qualnames,
        class_qualnames=class_qualnames,
        out=out,
    )
    return out


def _reference_location(node: ast.AST) -> tuple[int, int, int, int]:
    lineno = max(1, int(getattr(node, "lineno", 1) or 1))
    column = max(0, int(getattr(node, "col_offset", 0) or 0))
    end_lineno = max(lineno, int(getattr(node, "end_lineno", lineno) or lineno))
    end_column = max(column, int(getattr(node, "end_col_offset", column) or column))
    return lineno, column, end_lineno, end_column


def _append_reference(
    out: list[Reference],
    *,
    path: str,
    scope: str,
    lexical: str,
    kind: str,
    node: ast.AST,
    aliases: dict[str, _ImportBinding],
    symbol_qualnames: set[str],
    class_qualnames: set[str],
) -> None:
    name, target, resolution, confidence = _resolve_syntactic_target(
        lexical,
        caller=scope,
        aliases=aliases,
        symbol_qualnames=symbol_qualnames,
        class_qualnames=class_qualnames,
    )
    lineno, column, end_lineno, end_column = _reference_location(node)
    out.append(
        Reference(
            path=path,
            scope=scope,
            name=name,
            lexical_name=lexical,
            kind=kind,
            lineno=lineno,
            column=column,
            end_lineno=end_lineno,
            end_column=end_column,
            target=target,
            resolution=resolution,
            confidence=confidence,
        )
    )


def _append_import_binding(
    out: list[Reference],
    *,
    path: str,
    scope: str,
    local: str,
    target: str,
    resolution: str,
    confidence: float,
    node: ast.AST,
) -> None:
    lineno, column, end_lineno, end_column = _reference_location(node)
    out.append(
        Reference(
            path=path,
            scope=scope,
            name=local,
            lexical_name=local,
            kind="import_binding",
            lineno=lineno,
            column=column,
            end_lineno=end_lineno,
            end_column=end_column,
            target=target,
            resolution=resolution,
            confidence=confidence,
        )
    )


def _walk_references(
    node: ast.AST,
    *,
    path: str,
    scope_stack: list[str],
    aliases: dict[str, _ImportBinding],
    symbol_qualnames: set[str],
    class_qualnames: set[str],
    out: list[Reference],
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            qualname = (
                f"{scope_stack[-1]}.{child.name}"
                if scope_stack[-1] != "<module>"
                else child.name
            )
            _walk_references(
                child,
                path=path,
                scope_stack=[*scope_stack, qualname],
                aliases={**aliases, **_scope_import_bindings(child, path=path)},
                symbol_qualnames=symbol_qualnames,
                class_qualnames=class_qualnames,
                out=out,
            )
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = (
                f"{scope_stack[-1]}.{child.name}"
                if scope_stack[-1] != "<module>"
                else child.name
            )
            _walk_references(
                child,
                path=path,
                scope_stack=[*scope_stack, qualname],
                aliases={**aliases, **_scope_import_bindings(child, path=path)},
                symbol_qualnames=symbol_qualnames,
                class_qualnames=class_qualnames,
                out=out,
            )
            continue
        if isinstance(child, ast.Import):
            for alias in child.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                _append_import_binding(
                    out,
                    path=path,
                    scope=scope_stack[-1],
                    local=local,
                    target=alias.name,
                    resolution="import_alias" if alias.asname else "import",
                    confidence=0.9,
                    node=alias,
                )
            continue
        if isinstance(child, ast.ImportFrom):
            base = _resolve_import_module(path, child.module, int(child.level or 0))
            for alias in child.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                target = ".".join(part for part in (base, alias.name) if part)
                _append_import_binding(
                    out,
                    path=path,
                    scope=scope_stack[-1],
                    local=local,
                    target=target,
                    resolution="relative_import_alias" if child.level else "from_import_alias",
                    confidence=0.95 if not child.level else 0.92,
                    node=alias,
                )
            continue
        if isinstance(child, ast.Call):
            lexical = _resolve_call_target(child.func)
            if lexical:
                _append_reference(
                    out,
                    path=path,
                    scope=scope_stack[-1],
                    lexical=lexical,
                    kind="call",
                    node=child.func,
                    aliases=aliases,
                    symbol_qualnames=symbol_qualnames,
                    class_qualnames=class_qualnames,
                )
            else:
                _walk_references(
                    child.func,
                    path=path,
                    scope_stack=scope_stack,
                    aliases=aliases,
                    symbol_qualnames=symbol_qualnames,
                    class_qualnames=class_qualnames,
                    out=out,
                )
            for argument in child.args:
                _walk_references(
                    argument,
                    path=path,
                    scope_stack=scope_stack,
                    aliases=aliases,
                    symbol_qualnames=symbol_qualnames,
                    class_qualnames=class_qualnames,
                    out=out,
                )
            for keyword in child.keywords:
                _walk_references(
                    keyword.value,
                    path=path,
                    scope_stack=scope_stack,
                    aliases=aliases,
                    symbol_qualnames=symbol_qualnames,
                    class_qualnames=class_qualnames,
                    out=out,
                )
            continue
        if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            lexical = _resolve_call_target(child)
            if lexical:
                _append_reference(
                    out,
                    path=path,
                    scope=scope_stack[-1],
                    lexical=lexical,
                    kind="attribute_read",
                    node=child,
                    aliases=aliases,
                    symbol_qualnames=symbol_qualnames,
                    class_qualnames=class_qualnames,
                )
            if isinstance(child.value, ast.Call):
                _walk_references(
                    child.value,
                    path=path,
                    scope_stack=scope_stack,
                    aliases=aliases,
                    symbol_qualnames=symbol_qualnames,
                    class_qualnames=class_qualnames,
                    out=out,
                )
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            _append_reference(
                out,
                path=path,
                scope=scope_stack[-1],
                lexical=child.id,
                kind="name_read",
                node=child,
                aliases=aliases,
                symbol_qualnames=symbol_qualnames,
                class_qualnames=class_qualnames,
            )
            continue
        _walk_references(
            child,
            path=path,
            scope_stack=scope_stack,
            aliases=aliases,
            symbol_qualnames=symbol_qualnames,
            class_qualnames=class_qualnames,
            out=out,
        )


def _walk_calls(
    node: ast.AST,
    *,
    path: str,
    caller_stack: list[str],
    aliases: dict[str, _ImportBinding],
    symbol_qualnames: set[str],
    class_qualnames: set[str],
    out: list[CallEdge],
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            qn = f"{caller_stack[-1]}.{child.name}" if caller_stack[-1] != "<module>" else child.name
            child_aliases = {**aliases, **_scope_import_bindings(child, path=path)}
            _walk_calls(
                child,
                path=path,
                caller_stack=caller_stack + [qn],
                aliases=child_aliases,
                symbol_qualnames=symbol_qualnames,
                class_qualnames=class_qualnames,
                out=out,
            )
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qn = f"{caller_stack[-1]}.{child.name}" if caller_stack[-1] != "<module>" else child.name
            child_aliases = {**aliases, **_scope_import_bindings(child, path=path)}
            _walk_calls(
                child,
                path=path,
                caller_stack=caller_stack + [qn],
                aliases=child_aliases,
                symbol_qualnames=symbol_qualnames,
                class_qualnames=class_qualnames,
                out=out,
            )
        elif isinstance(child, ast.Call):
            lexical_callee = _resolve_call_target(child.func)
            if lexical_callee:
                callee, target, resolution, confidence = _resolve_syntactic_target(
                    lexical_callee,
                    caller=caller_stack[-1],
                    aliases=aliases,
                    symbol_qualnames=symbol_qualnames,
                    class_qualnames=class_qualnames,
                )
                out.append(CallEdge(
                    path=path,
                    caller=caller_stack[-1],
                    callee=callee,
                    lineno=child.lineno,
                    lexical_callee=lexical_callee,
                    target=target,
                    resolution=resolution,
                    confidence=confidence,
                ))
            _walk_calls(
                child,
                path=path,
                caller_stack=caller_stack,
                aliases=aliases,
                symbol_qualnames=symbol_qualnames,
                class_qualnames=class_qualnames,
                out=out,
            )
        else:
            _walk_calls(
                child,
                path=path,
                caller_stack=caller_stack,
                aliases=aliases,
                symbol_qualnames=symbol_qualnames,
                class_qualnames=class_qualnames,
                out=out,
            )


def _resolve_call_target(node: ast.AST) -> str | None:
    """Resolve `foo`, `mod.foo`, `obj.attr.foo`, `self.foo` lexically."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "super"
        ):
            return f"super.{node.attr}"
        prefix = _resolve_call_target(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
        return node.attr
    return None


def _module_parts(path: str) -> list[str]:
    parts = list(Path(path).with_suffix("").parts)
    while parts and parts[0] in {".", "src", "lib", "python"}:
        parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return [part for part in parts if part and part not in {".."}]


def _resolve_import_module(path: str, module: str | None, level: int) -> str:
    module_parts = [part for part in str(module or "").split(".") if part]
    if level <= 0:
        return ".".join(module_parts)
    package = _module_parts(path)[:-1]
    drop = max(0, int(level) - 1)
    if drop:
        package = package[:-drop] if drop < len(package) else []
    return ".".join([*package, *module_parts])


def _scope_import_bindings(node: ast.AST, *, path: str) -> dict[str, _ImportBinding]:
    """Collect imports in one lexical scope without descending into child scopes."""
    bindings: dict[str, _ImportBinding] = {}

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:  # noqa: N802
            return

        def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:  # noqa: N802
            return

        def visit_ClassDef(self, child: ast.ClassDef) -> None:  # noqa: N802
            return

        def visit_Lambda(self, child: ast.Lambda) -> None:  # noqa: N802
            return

        def visit_Import(self, child: ast.Import) -> None:  # noqa: N802
            for alias in child.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                target = alias.name if alias.asname else local
                bindings[local] = _ImportBinding(
                    target=target,
                    graph_prefix=local,
                    resolution="import_alias" if alias.asname else "import",
                    confidence=0.9,
                )

        def visit_ImportFrom(self, child: ast.ImportFrom) -> None:  # noqa: N802
            base = _resolve_import_module(path, child.module, int(child.level or 0))
            for alias in child.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                target = ".".join(part for part in (base, alias.name) if part)
                bindings[local] = _ImportBinding(
                    target=target,
                    graph_prefix=alias.name,
                    resolution=(
                        "relative_import_alias"
                        if child.level
                        else "from_import_alias"
                    ),
                    confidence=0.95 if not child.level else 0.92,
                )

    collector = Collector()
    for statement in getattr(node, "body", []):
        collector.visit(statement)
    return bindings


def _same_file_symbol(
    lexical: str,
    caller: str,
    symbol_qualnames: set[str],
    class_qualnames: set[str],
) -> str | None:
    if "." in lexical:
        return None
    if caller != "<module>":
        scope = caller
        while scope:
            # A plain name inside a method does not resolve through the class
            # namespace; sibling methods require self/cls qualification.
            if scope not in class_qualnames:
                nested = f"{scope}.{lexical}"
                if nested in symbol_qualnames:
                    return nested
            scope = scope.rsplit(".", 1)[0] if "." in scope else ""
    return lexical if lexical in symbol_qualnames else None


def _enclosing_class(caller: str, class_qualnames: set[str]) -> str | None:
    matches = [name for name in class_qualnames if caller == name or caller.startswith(f"{name}.")]
    return max(matches, key=len) if matches else None


def _resolve_syntactic_target(
    lexical: str,
    *,
    caller: str,
    aliases: dict[str, _ImportBinding],
    symbol_qualnames: set[str],
    class_qualnames: set[str],
) -> tuple[str, str | None, str, float]:
    parts = lexical.split(".")
    binding = aliases.get(parts[0])
    if binding is not None:
        suffix = parts[1:]
        target = ".".join([binding.target, *suffix])
        graph_parts = [part for part in ([binding.graph_prefix] if binding.graph_prefix else []) + suffix if part]
        callee = ".".join(graph_parts) if graph_parts else target.rsplit(".", 1)[-1]
        return callee, target, binding.resolution, binding.confidence

    if parts[0] in {"self", "cls"} and len(parts) > 1:
        owner = _enclosing_class(caller, class_qualnames)
        if owner:
            target = ".".join([owner, *parts[1:]])
            return lexical, target, "class_member", 0.95

    same_file = _same_file_symbol(lexical, caller, symbol_qualnames, class_qualnames)
    if same_file:
        return same_file, same_file, "same_file_symbol", 0.9

    return lexical, None, "lexical", 0.45


def query_callers(root: Path, qualname: str, *, limit: int = 20) -> dict:
    """Return lexical or syntactically-resolved callers for ``qualname``."""
    from .search import connect, init_schema

    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            "select path, caller, callee, lineno, lang, lexical_callee, target, resolution, confidence "
            "from code_calls "
            "where callee = ? or lexical_callee = ? or target = ? or target like ? escape '\\' "
            "order by confidence desc, path, lineno limit ?",
            (qualname, qualname, qualname, f"%.{_escape_like(qualname)}", limit),
        ).fetchall()
    callers = [dict(row) for row in rows]
    for item in callers:
        item["matched_on"] = _matched_on(item, qualname)
    return {
        "ok": True,
        "callee": qualname,
        "count": len(rows),
        "backend": "syntactic_codegraph",
        "callers": callers,
    }


def query_references(root: Path, symbol: str, *, limit: int = 200) -> dict:
    """Return bounded call and non-call references with ambiguity diagnostics."""
    from .search import connect, init_schema

    cap = max(1, min(500, int(limit)))
    needle = str(symbol or "").strip()
    if not needle:
        return {
            "ok": True,
            "symbol": needle,
            "count": 0,
            "references": [],
            "backend": "syntactic_codegraph",
            "precision": "syntactic",
            "complete": True,
            "ambiguous": False,
            "definition_candidates": [],
        }
    suffix = f"%.{_escape_like(needle)}"
    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            "select path, scope, name, lexical_name, kind, lineno, column, end_lineno, end_column, "
            "lang, target, resolution, confidence from code_references "
            "where name = ? or lexical_name = ? or target = ? or target like ? escape '\\' "
            "order by confidence desc, case when kind = 'import_binding' then 1 else 0 end, "
            "path, lineno, column, kind limit ?",
            (needle, needle, needle, suffix, cap + 1),
        ).fetchall()
        definitions = _definition_candidates(conn, needle, limit=32)

    partial = len(rows) > cap
    references = [dict(row) for row in rows[:cap]]
    for item in references:
        item["matched_on"] = _reference_matched_on(item, needle)
    best_tier = min((int(item["match_tier"]) for item in definitions), default=None)
    best_count = sum(1 for item in definitions if int(item["match_tier"]) == best_tier)
    return {
        "ok": True,
        "symbol": needle,
        "count": len(references),
        "references": references,
        "backend": "syntactic_codegraph",
        "precision": "syntactic",
        "complete": not partial,
        "partial": partial,
        "limit": cap,
        "ambiguous": best_count > 1,
        "definition_candidates": definitions,
        "definition_candidate_count": len(definitions),
        "best_definition_count": best_count,
    }


def query_callees(root: Path, qualname: str, *, limit: int = 20) -> dict:
    """Return rows where caller == qualname (exact match)."""
    from .search import connect, init_schema

    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            "select path, caller, callee, lineno, lang, lexical_callee, target, resolution, confidence "
            "from code_calls "
            "where caller = ? order by lineno limit ?",
            (qualname, limit),
        ).fetchall()
    return {
        "ok": True,
        "caller": qualname,
        "count": len(rows),
        "backend": "syntactic_codegraph",
        "callees": [dict(r) for r in rows],
    }


def find_symbol(root: Path, name: str, *, limit: int = 20) -> dict:
    """LIKE-match qualname; matches both exact and fragment (e.g. 'recommend')."""
    from .search import connect, init_schema

    pat = f"%{name}%"
    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            "select path, qualname, kind, lineno, end_lineno, parent, lang from code_symbols "
            "where qualname like ? order by length(qualname), path, lineno limit ?",
            (pat, limit),
        ).fetchall()
    return {
        "ok": True,
        "needle": name,
        "count": len(rows),
        "backend": "syntactic_codegraph",
        "symbols": [dict(r) for r in rows],
    }


def hotspot_callees(root: Path, *, limit: int = 20) -> dict:
    """Most-frequently-called callees across the indexed codebase."""
    from .search import connect, init_schema

    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            "select callee, count(*) as n from code_calls "
            "group by callee order by n desc, callee asc limit ?",
            (limit,),
        ).fetchall()
    return {
        "ok": True,
        "count": len(rows),
        "hotspots": [{"callee": r["callee"], "calls": r["n"]} for r in rows],
    }


def trace_call_path(root: Path, *, src: str, dst: str, max_depth: int = 6) -> dict:
    """Shortest caller→callee chain from `src` to `dst` (multi-hop BFS). Orientation aid only."""
    from collections import deque

    from .search import connect, init_schema

    with connect(root) as conn:
        init_schema(conn)
        seen = {src}
        q: deque[list[str]] = deque([[src]])
        while q:
            chain = q.popleft()
            if len(chain) > max(1, int(max_depth)):
                continue
            node = chain[-1]
            rows = conn.execute(
                "select distinct case "
                "when resolution = 'class_member' and target is not null then target "
                "else callee end as graph_callee "
                "from code_calls where caller = ? limit 200",
                (node,),
            ).fetchall()
            for r in rows:
                callee = r["graph_callee"]
                if callee == dst:
                    return {"ok": True, "found": True, "path": chain + [callee], "hops": len(chain)}
                if callee not in seen:
                    seen.add(callee)
                    q.append(chain + [callee])
    return {"ok": True, "found": False, "path": [], "scanned": len(seen)}


def blast_radius(root: Path, *, symbols: list[str], max_depth: int = 4, limit: int = 200) -> dict:
    """Transitive callers of `symbols` (reverse BFS) = the impact set of changing them."""
    from collections import deque

    from .search import connect, init_schema

    seeds = [s for s in (symbols or []) if isinstance(s, str) and s]
    impacted: dict[str, int] = {}
    with connect(root) as conn:
        init_schema(conn)
        q: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
        seen = set(seeds)
        while q and len(impacted) < limit:
            node, depth = q.popleft()
            if depth >= max(1, int(max_depth)):
                continue
            rows = conn.execute(
                "select distinct caller from code_calls "
                "where callee = ? or target = ? limit 200",
                (node, node),
            ).fetchall()
            for r in rows:
                caller = r["caller"]
                if not caller or caller in seen:
                    continue
                seen.add(caller)
                impacted[caller] = depth + 1
                q.append((caller, depth + 1))
    ranked = sorted(impacted.items(), key=lambda kv: (kv[1], kv[0]))
    return {"ok": True, "seeds": seeds, "count": len(ranked),
            "impacted": [{"symbol": s, "distance": d} for s, d in ranked[:limit]]}


def impacted_by_paths(root: Path, *, paths: list[str], max_depth: int = 4) -> dict:
    """Map changed file paths → the symbols they define → transitive callers (git-diff blast radius)."""
    from .search import connect, init_schema

    norm = [str(p).split("::", 1)[0] for p in (paths or []) if isinstance(p, str) and p]
    symbols: list[str] = []
    with connect(root) as conn:
        init_schema(conn)
        for p in norm[:100]:
            rows = conn.execute(
                "select qualname from code_symbols where path = ? limit 500", (p,)
            ).fetchall()
            symbols.extend(r["qualname"] for r in rows)
    out = blast_radius(root, symbols=symbols, max_depth=max_depth)
    out["changed_paths"] = norm
    out["changed_symbols"] = len(symbols)
    return out


def architecture_summary(root: Path, *, limit: int = 8) -> dict:
    """Cheap whole-repo orientation: top modules by symbol count and incoming-call centrality."""
    from .search import connect, init_schema

    with connect(root) as conn:
        init_schema(conn)
        sym_rows = conn.execute(
            "select path, count(*) as n from code_symbols group by path order by n desc limit 500"
        ).fetchall()
        call_rows = conn.execute(
            "select c.path as path, count(*) as n from code_calls c group by c.path "
            "order by n desc limit 500"
        ).fetchall()
    calls_by_path = {r["path"]: r["n"] for r in call_rows}

    def _module(p: str) -> str:
        return str(p).split("::", 1)[0]

    agg: dict[str, dict[str, int]] = {}
    for r in sym_rows:
        m = _module(r["path"])
        a = agg.setdefault(m, {"symbols": 0, "calls": 0})
        a["symbols"] += int(r["n"])
        a["calls"] += int(calls_by_path.get(r["path"], 0))
    ranked = sorted(agg.items(), key=lambda kv: (kv[1]["symbols"] + kv[1]["calls"]), reverse=True)
    return {"ok": True, "count": len(ranked),
            "modules": [{"module": m, **v} for m, v in ranked[:limit]]}


def _escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _canonical_symbol(path: str, qualname: str) -> str:
    module = ".".join(_module_parts(path))
    return ".".join(part for part in (module, qualname) if part)


def _definition_candidates(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    limit: int,
) -> list[dict[str, object]]:
    tail = str(symbol).rsplit(".", 1)[-1]
    escaped = _escape_like(tail)
    rows = conn.execute(
        "select path, qualname, kind, lineno, end_lineno, parent, lang from code_symbols "
        "where qualname = ? or qualname like ? escape '\\' "
        "order by path, lineno limit ?",
        (tail, f"%.{escaped}", max(1, min(128, int(limit)))),
    ).fetchall()
    candidates: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        canonical = _canonical_symbol(str(item["path"]), str(item["qualname"]))
        qualname = str(item["qualname"])
        if canonical == symbol:
            tier = 0
        elif symbol == qualname:
            tier = 1
        elif canonical.endswith(f".{symbol}"):
            tier = 2
        elif qualname.rsplit(".", 1)[-1] == tail:
            tier = 3
        else:
            tier = 4
        item["canonical"] = canonical
        item["match_tier"] = tier
        candidates.append(item)
    candidates.sort(
        key=lambda item: (
            int(item["match_tier"]),
            str(item["path"]),
            int(item["lineno"]),
        )
    )
    return candidates


def _reference_matched_on(item: dict[str, object], symbol: str) -> str:
    if str(item.get("target") or "") == symbol:
        return "target"
    if str(item.get("name") or "") == symbol:
        return "name"
    if str(item.get("lexical_name") or "") == symbol:
        return "lexical_name"
    target = str(item.get("target") or "")
    return "target_suffix" if target.endswith(f".{symbol}") else "unknown"


def _matched_on(item: dict[str, object], qualname: str) -> str:
    if str(item.get("target") or "") == qualname:
        return "target"
    if str(item.get("callee") or "") == qualname:
        return "callee"
    if str(item.get("lexical_callee") or "") == qualname:
        return "lexical_callee"
    target = str(item.get("target") or "")
    return "target_suffix" if target.endswith(f".{qualname}") else "unknown"


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield project Python files, skipping common cache/venv dirs and hidden files."""
    skip_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".ai"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn
