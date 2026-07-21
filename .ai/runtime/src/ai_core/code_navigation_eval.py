"""Deterministic qrel evaluation for production code-navigation fallbacks."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from . import codegraph, lsp
from .code_retrieval_eval import corpus_snapshot_sha256
from .ranking_metrics import evaluate_ranked_retrieval


def _caller_id(item: dict[str, Any]) -> str:
    return f"{item.get('path')}::{item.get('caller')}@{int(item.get('lineno') or 0)}"


def _definition_id(item: dict[str, Any]) -> str:
    return f"{item.get('path')}::{item.get('qualname')}"


def _symbol_id(item: dict[str, Any]) -> str:
    return f"{item.get('path')}::{item.get('name')}"


def _reference_id(item: dict[str, Any]) -> str:
    return (
        f"{item.get('path')}::{item.get('scope')}@"
        f"{int(item.get('lineno') or 0)}:{int(item.get('column') or 0)}:"
        f"{item.get('kind')}"
    )


def evaluate(root: Path, golden: list[dict[str, Any]], *, k: int = 5) -> dict[str, Any]:
    specs = {str(item.get("query") or ""): item for item in golden}
    operation_counts: Counter[str] = Counter()
    backend_counts: Counter[str] = Counter()
    diagnostics: dict[str, Any] = {}

    def ranked_search(query_id: str, requested_k: int) -> list[str]:
        spec = specs[query_id]
        operation = str(spec.get("operation") or "")
        operation_counts[operation] += 1
        ranked: list[str]
        payload: dict[str, Any]

        if operation == "callers":
            payload = codegraph.query_callers(
                root,
                str(spec.get("symbol") or ""),
                limit=requested_k,
            )
            ranked = [_caller_id(item) for item in payload.get("callers") or []]
        elif operation == "references":
            payload = codegraph.query_references(
                root,
                str(spec.get("symbol") or ""),
                limit=requested_k,
            )
            ranked = [_reference_id(item) for item in payload.get("references") or []]
        elif operation == "definition":
            payload = lsp._syntactic_definition(
                root,
                str(spec.get("path") or ""),
                int(spec.get("line") or 0),
                int(spec.get("column") or 0),
                fallback_reason="deterministic_eval",
            ) or {"ok": False, "definition": None, "backend": "unavailable"}
            definition = payload.get("definition")
            ranked = [_definition_id(definition)] if isinstance(definition, dict) else []
        elif operation == "workspace_symbols":
            payload = lsp._syntactic_workspace_symbols(
                root,
                str(spec.get("symbol") or ""),
                limit=requested_k,
                fallback_reason="deterministic_eval",
            ) or {"ok": False, "symbols": [], "backend": "unavailable"}
            ranked = [_symbol_id(item) for item in payload.get("symbols") or []]
        elif operation == "trace":
            payload = codegraph.trace_call_path(
                root,
                src=str(spec.get("src") or ""),
                dst=str(spec.get("dst") or ""),
                max_depth=int(spec.get("max_depth") or 6),
            )
            path = payload.get("path")
            ranked = ["->".join(str(item) for item in path)] if payload.get("found") and isinstance(path, list) else []
        else:
            raise ValueError(f"unsupported code navigation operation: {operation}")

        backend = str(payload.get("backend") or ("syntactic_codegraph" if operation in {"callers", "trace"} else "unknown"))
        backend_counts[backend] += 1
        diagnostics[query_id] = {
            "operation": operation,
            "backend": backend,
            "precision": payload.get("precision") or "syntactic",
            "returned": len(ranked),
            "complete": bool(payload.get("complete", True)),
            "partial": bool(payload.get("partial", False)),
            "ambiguous": bool(payload.get("ambiguous", False)),
            "definition_candidate_count": int(payload.get("definition_candidate_count", 0) or 0),
            "best_definition_count": int(payload.get("best_definition_count", 0) or 0),
        }
        return ranked[:requested_k]

    report = evaluate_ranked_retrieval(golden, ranked_search, k=k)
    report.update(
        {
            "corpus_sha256": corpus_snapshot_sha256(root),
            "operation_counts": dict(sorted(operation_counts.items())),
            "backend_counts": dict(sorted(backend_counts.items())),
            "query_diagnostics": diagnostics,
        }
    )
    return report


__all__ = ["evaluate"]
