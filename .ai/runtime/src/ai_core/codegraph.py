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


def query_callers(root: Path, qualname: str, *, limit: int = 20) -> dict:
    """Return rows where callee == qualname (exact match)."""
    from .search import connect, init_schema

    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            "select path, caller, callee, lineno, lang from code_calls "
            "where callee = ? order by path, lineno limit ?",
            (qualname, limit),
        ).fetchall()
    return {
        "ok": True,
        "callee": qualname,
        "count": len(rows),
        "callers": [dict(r) for r in rows],
    }


def query_callees(root: Path, qualname: str, *, limit: int = 20) -> dict:
    """Return rows where caller == qualname (exact match)."""
    from .search import connect, init_schema

    with connect(root) as conn:
        init_schema(conn)
        rows = conn.execute(
            "select path, caller, callee, lineno, lang from code_calls "
            "where caller = ? order by lineno limit ?",
            (qualname, limit),
        ).fetchall()
    return {
        "ok": True,
        "caller": qualname,
        "count": len(rows),
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
                "select distinct callee from code_calls where caller = ? limit 200", (node,)
            ).fetchall()
            for r in rows:
                callee = r["callee"]
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
                "select distinct caller from code_calls where callee = ? limit 200", (node,)
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


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield project Python files, skipping common cache/venv dirs and hidden files."""
    skip_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".ai"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn
