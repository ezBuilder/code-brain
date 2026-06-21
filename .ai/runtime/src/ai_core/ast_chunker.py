"""cAST-style AST-aware chunking for Python (pilot, opt-in, stdlib only).

cAST = recursively split AST nodes that exceed a size budget, then merge
adjacent small siblings into size-balanced, syntactically-coherent chunks.
Compared with fixed-size or strict function-boundary chunking this keeps each
chunk syntactically whole while avoiding both oversized blobs and a swarm of
tiny single-statement fragments, which improves retrieval recall.

Scope of this pilot:
- Python source only, parsed with the stdlib :mod:`ast` (no tree-sitter, no
  third-party dependency, no network, no LLM).
- Pure and deterministic: same input always yields the same chunks.
- Fail-soft: any :class:`SyntaxError` (or other parse failure) returns ``[]``
  so the caller transparently falls back to the existing chunker.

Default OFF. The indexer only reaches this module through
:func:`maybe_ast_chunks`, which returns ``None`` unless ``AI_AST_CHUNK`` is
truthy in the environment.
"""

from __future__ import annotations

import ast
import os
from typing import Any

# Node types that introduce their own nameable scope. These are the recursion
# boundaries: a class/def can be split into its own member sub-chunks when its
# span is too large.
_CONTAINER_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "off", "false", "no"}


def ast_chunk_enabled() -> bool:
    """Whether opt-in AST chunking is enabled via the ``AI_AST_CHUNK`` env var."""
    return _truthy(os.environ.get("AI_AST_CHUNK"))


def _node_start_line(node: ast.AST) -> int:
    """1-indexed first line of a node, including any preceding decorators."""
    decorators = getattr(node, "decorator_list", None) or []
    start = int(getattr(node, "lineno", 1))
    for dec in decorators:
        dec_line = getattr(dec, "lineno", None)
        if isinstance(dec_line, int) and dec_line < start:
            start = dec_line
    return start


def _node_end_line(node: ast.AST, *, total_lines: int) -> int:
    """1-indexed last line of a node (inclusive), clamped to file length."""
    end = getattr(node, "end_lineno", None)
    if not isinstance(end, int):
        end = int(getattr(node, "lineno", 1))
    return min(end, total_lines)


def _slice(lines: list[str], start_line: int, end_line: int) -> str:
    """Join 1-indexed inclusive ``[start_line, end_line]`` from ``lines``."""
    return "\n".join(lines[start_line - 1 : end_line])


def _span_chars(lines: list[str], start_line: int, end_line: int) -> int:
    return len(_slice(lines, start_line, end_line))


def _make_chunk(lines: list[str], start_line: int, end_line: int) -> dict[str, Any]:
    return {
        "text": _slice(lines, start_line, end_line),
        "start_line": start_line,
        "end_line": end_line,
    }


def _merge_siblings(
    spans: list[tuple[int, int]],
    lines: list[str],
    *,
    max_chars: int,
    min_chars: int,
) -> list[tuple[int, int]]:
    """Greedily merge adjacent sibling spans whose combined span < max_chars.

    Spans are ``(start_line, end_line)`` pairs in source order. A run of small
    siblings is accumulated into one span as long as the merged size stays
    under ``max_chars``; a span that is already large enough (>= ``min_chars``)
    or that would overflow the budget flushes the current accumulator and
    stands on its own. This is the cAST "merge small siblings" step.
    """
    merged: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for span in spans:
        if cur is None:
            cur = span
            continue
        combined_end = span[1]
        combined_chars = _span_chars(lines, cur[0], combined_end)
        cur_chars = _span_chars(lines, cur[0], cur[1])
        # Keep merging while the running chunk is still under min_chars (too
        # tiny to stand alone) and the merge stays within the max budget.
        if cur_chars < min_chars and combined_chars <= max_chars:
            cur = (cur[0], combined_end)
        else:
            merged.append(cur)
            cur = span
    if cur is not None:
        merged.append(cur)
    return merged


def _split_node(
    node: ast.AST,
    lines: list[str],
    *,
    max_chars: int,
    min_chars: int,
) -> list[tuple[int, int]]:
    """Return the line spans for a single top-level/container node.

    If the node fits within ``max_chars`` it yields one span. Otherwise, for a
    container (class/def) it recurses into its body, emitting a span for the
    header region (signature + any code before the first nested container) plus
    spans for each nested container, then merges small siblings. Non-container
    oversized nodes are emitted whole (cannot be split syntactically here).
    """
    total = len(lines)
    start = _node_start_line(node)
    end = _node_end_line(node, total_lines=total)
    if _span_chars(lines, start, end) <= max_chars:
        return [(start, end)]

    body = list(getattr(node, "body", []) or [])
    nested = [child for child in body if isinstance(child, _CONTAINER_NODES)]
    if not isinstance(node, _CONTAINER_NODES) or not nested:
        # Oversized but not splittable into nested scopes — keep it whole so the
        # chunk stays syntactically coherent.
        return [(start, end)]

    spans: list[tuple[int, int]] = []
    first_nested_start = _node_start_line(nested[0])
    # Header span: decorators + signature + any leading body before the first
    # nested container (docstring, attributes, etc.).
    if first_nested_start - 1 >= start:
        spans.append((start, first_nested_start - 1))

    for child in nested:
        spans.extend(_split_node(child, lines, max_chars=max_chars, min_chars=min_chars))

    # Tail span: any body after the last nested container (e.g. trailing code).
    last_nested_end = max(s[1] for s in spans)
    if end > last_nested_end:
        spans.append((last_nested_end + 1, end))

    spans.sort()
    return _merge_siblings(spans, lines, max_chars=max_chars, min_chars=min_chars)


def chunk_python(
    source: str,
    *,
    max_chars: int = 1500,
    min_chars: int = 200,
) -> list[dict[str, Any]]:
    """Chunk Python ``source`` into AST-aware, size-balanced chunks.

    Returns a list of ``{"text", "start_line", "end_line"}`` dicts in source
    order. ``start_line``/``end_line`` are 1-indexed and inclusive.

    Behavior:
    - Walks top-level statements; ``def``/``class`` whose source span exceeds
      ``max_chars`` are recursively split into nested-scope sub-chunks.
    - Adjacent small siblings whose combined span stays under ``max_chars`` are
      merged so tiny fragments do not become separate chunks.
    - Pure, deterministic, no network/LLM.
    - On :class:`SyntaxError` (or any parse failure) returns ``[]`` so the
      caller can fall back to its existing chunker.
    """
    if not isinstance(source, str) or not source.strip():
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []

    lines = source.split("\n")
    total = len(lines)
    top_level = list(getattr(tree, "body", []) or [])
    if not top_level:
        return []

    # First, expand each top-level node into (possibly recursively-split) spans.
    raw_spans: list[tuple[int, int]] = []
    for node in top_level:
        node_spans = _split_node(node, lines, max_chars=max_chars, min_chars=min_chars)
        raw_spans.extend(node_spans)

    if not raw_spans:
        return []

    raw_spans.sort()
    # Clamp any span to file bounds defensively.
    bounded = [
        (max(1, s), min(e, total))
        for (s, e) in raw_spans
        if 1 <= s <= e
    ]
    # Merge adjacent small siblings at the top level too.
    final_spans = _merge_siblings(bounded, lines, max_chars=max_chars, min_chars=min_chars)

    chunks: list[dict[str, Any]] = []
    for (start, end) in final_spans:
        if start < 1 or end < start or end > total:
            continue
        chunks.append(_make_chunk(lines, start, end))
    return chunks


def maybe_ast_chunks(path: str, source: str) -> list[dict[str, Any]] | None:
    """Opt-in entry point for the indexer.

    Returns ``None`` (signalling "use the existing chunker") unless ALL hold:
    - ``AI_AST_CHUNK`` is truthy in the environment, and
    - ``path`` is a Python file (``.py``).

    When enabled for a ``.py`` file, returns :func:`chunk_python`'s result
    (which itself is ``[]`` on parse failure, letting the caller fall back).
    Default behavior (env unset) is ``None`` for every path, so the existing
    chunking path stays byte-identical.
    """
    if not ast_chunk_enabled():
        return None
    if not isinstance(path, str) or not path.endswith(".py"):
        return None
    return chunk_python(source)
