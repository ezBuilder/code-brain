"""Deterministic, read-only evaluation runner for Code Brain itself.

The runner exercises production decision functions directly. It never calls
an LLM, the network, or mutable memory paths, which keeps supported axes
reproducible in CI and on developer machines.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from typing import Any


EVALS_DIR = pathlib.Path(__file__).resolve().parent
CASES_DIR = EVALS_DIR / "cases"
REPO_ROOT = EVALS_DIR.parents[1]
RUNTIME_SRC = REPO_ROOT / ".ai" / "runtime" / "src"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

Observed = dict[str, Any]
Adapter = Callable[[dict[str, Any]], Observed]


def load_cases(axis: str) -> Iterable[dict[str, Any]]:
    path = CASES_DIR / f"{axis}.jsonl"
    if not path.exists():
        raise ValueError(f"unknown axis: {axis} (no {path})")
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(case, dict):
                raise ValueError(f"{path}:{line_number}: case must be an object")
            if not str(case.get("id") or "").strip():
                raise ValueError(f"{path}:{line_number}: case id is required")
            expectations = case.get("expect")
            if not isinstance(expectations, list) or not expectations:
                raise ValueError(f"{path}:{line_number}: non-empty expect list is required")
            yield case


def _observe_precall(case: dict[str, Any]) -> Observed:
    from ai_core.precall import evaluate

    command = str(case.get("cmd") or "")
    result = evaluate("Bash", {"command": command})
    return {"tool": "Bash", "command": command, **result}


def _observe_context_budget(case: dict[str, Any]) -> Observed:
    from ai_core.context_budget import apply

    results = case.get("results")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise ValueError("context_budget case requires an object list in results")
    return apply(
        results,
        mode=str(case.get("mode") or "balanced"),
        limit=int(case.get("limit") or len(results) or 1),
        base_max_bytes=int(case.get("base_max_bytes") or 4096),
    )


def _observe_tool_discovery(case: dict[str, Any]) -> Observed:
    from ai_core.mcp_server import _dispatch_tool

    query_text = str(case.get("query") or "")
    return _dispatch_tool(
        REPO_ROOT,
        "tool_search",
        {"query": query_text, "limit": int(case.get("limit") or 5)},
    )


def _observe_autoresearch_retrieval(case: dict[str, Any]) -> Observed:
    from ai_core.autoresearch import evalset, fts, storage

    corpus = case.get("corpus")
    golden = case.get("golden")
    if not isinstance(corpus, list) or not all(isinstance(item, dict) for item in corpus):
        raise ValueError("autoresearch_retrieval case requires an object list in corpus")
    if not isinstance(golden, list) or not all(isinstance(item, dict) for item in golden):
        raise ValueError("autoresearch_retrieval case requires an object list in golden")

    with tempfile.TemporaryDirectory(prefix="codebrain_retrieval_eval_") as tmpdir:
        ar_root = pathlib.Path(tmpdir) / "autoresearch"
        storage.ensure_tree(ar_root)
        wiki_root = storage.wiki_root(ar_root).resolve()
        for item in corpus:
            rel = pathlib.PurePosixPath(str(item.get("path") or ""))
            if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"invalid corpus path: {rel}")
            destination = wiki_root.joinpath(*rel.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(str(item.get("content") or ""), encoding="utf-8")
        fts.rebuild_index(ar_root)
        return evalset.evaluate(ar_root, golden, k=int(case.get("k") or 5))


def _observe_code_retrieval(case: dict[str, Any]) -> Observed:
    from ai_core import code_retrieval_eval
    from ai_core.search import rebuild

    corpus = case.get("corpus")
    golden = case.get("golden")
    if not isinstance(corpus, list) or not all(isinstance(item, dict) for item in corpus):
        raise ValueError("code_retrieval case requires an object list in corpus")
    if not isinstance(golden, list) or not all(isinstance(item, dict) for item in golden):
        raise ValueError("code_retrieval case requires an object list in golden")

    with tempfile.TemporaryDirectory(prefix="codebrain_code_retrieval_eval_") as tmpdir:
        repo = pathlib.Path(tmpdir) / "repo"
        (repo / ".ai").mkdir(parents=True)
        (repo / ".ai" / "config.yaml").write_text("project_name: code-retrieval-eval\n", encoding="utf-8")
        for item in corpus:
            rel = pathlib.PurePosixPath(str(item.get("path") or ""))
            if not rel.parts or rel.is_absolute() or ".." in rel.parts or rel.parts[0] == ".ai":
                raise ValueError(f"invalid corpus path: {rel}")
            destination = repo.joinpath(*rel.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(str(item.get("content") or ""), encoding="utf-8")
        rebuilt = rebuild(repo)
        if rebuilt.get("ok") is not True:
            raise RuntimeError(f"code retrieval index rebuild failed: {rebuilt.get('error') or rebuilt}")
        return code_retrieval_eval.evaluate(repo, golden, k=int(case.get("k") or 5))


def _observe_memory_retrieval(case: dict[str, Any]) -> Observed:
    from datetime import datetime, timezone

    from ai_core import memory_retrieval_eval

    store = case.get("store")
    golden = case.get("golden")
    if not isinstance(store, dict):
        raise ValueError("memory_retrieval case requires a store object")
    if not isinstance(golden, list) or not all(isinstance(item, dict) for item in golden):
        raise ValueError("memory_retrieval case requires an object list in golden")

    with tempfile.TemporaryDirectory(prefix="codebrain_memory_retrieval_eval_") as tmpdir:
        repo = pathlib.Path(tmpdir) / "repo"
        mem = repo / ".ai" / "memory"
        mem.mkdir(parents=True)
        (repo / ".ai" / "config.yaml").write_text("project_name: memory-retrieval-eval\n", encoding="utf-8")
        for key, filename in (
            ("decisions", "decisions.jsonl"),
            ("lessons", "lessons.jsonl"),
            ("procedures", "procedural.jsonl"),
        ):
            rows = store.get(key) or []
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"memory_retrieval store.{key} must be an object list")
            if rows:
                target = mem / filename
                target.write_text(
                    "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8",
                )
                target.chmod(0o600)
        # Pins recency decay only. Liveness (expires_at) folds against the wall
        # clock inside live_decision_records, so fixtures use far past/future
        # bounds — see memory_retrieval_eval's module docstring.
        now_raw = str(case.get("now") or "2026-01-01T00:00:00Z")
        moment = datetime.fromisoformat(now_raw.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return memory_retrieval_eval.evaluate(repo, golden, k=int(case.get("k") or 5), now=moment)


def _observe_line_span_retrieval(case: dict[str, Any]) -> Observed:
    """Evaluate verified spans emitted by the production context-pack path."""
    from ai_core.search import context_pack, rebuild
    from line_span_eval import evaluate

    corpus = case.get("corpus")
    if not isinstance(corpus, list) or not all(isinstance(item, dict) for item in corpus):
        raise ValueError("line_span_retrieval case requires an object list in corpus")
    query_text = str(case.get("query") or "").strip()
    if not query_text:
        raise ValueError("line_span_retrieval case requires a non-empty query")
    bounded_k = int(case.get("k") or 5)

    with tempfile.TemporaryDirectory(prefix="codebrain_line_span_eval_") as tmpdir:
        repo = pathlib.Path(tmpdir) / "repo"
        (repo / ".ai").mkdir(parents=True)
        (repo / ".ai" / "config.yaml").write_text(
            "project_name: line-span-retrieval-eval\n",
            encoding="utf-8",
        )
        for item in corpus:
            rel = pathlib.PurePosixPath(str(item.get("path") or ""))
            if not rel.parts or rel.is_absolute() or ".." in rel.parts or rel.parts[0] == ".ai":
                raise ValueError(f"invalid corpus path: {rel}")
            destination = repo.joinpath(*rel.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(str(item.get("content") or ""), encoding="utf-8")
        rebuilt = rebuild(repo)
        if rebuilt.get("ok") is not True:
            raise RuntimeError(f"line/span index rebuild failed: {rebuilt.get('error') or rebuilt}")
        payload = context_pack(
            repo,
            query_text,
            limit=bounded_k,
            representation="refs-only",
        )
        ranked = [
            {
                "path": ref["path"],
                **(
                    {"start_line": ref["start_line"], "end_line": ref["end_line"]}
                    if ref.get("start_line") is not None and ref.get("end_line") is not None
                    else {}
                ),
            }
            for ref in payload.get("lexical_refs", [])
        ]
        report = evaluate(case.get("qrels"), ranked, k=bounded_k)
        report.update(
            {
                "evaluation_mode": "production_context_pack_v2",
                "context_pack_version": payload.get("context_pack_version"),
                "retrieval_policy": payload.get("retrieval_policy"),
                "representation": payload.get("representation"),
            }
        )
        return report


ADAPTERS: dict[str, Adapter] = {
    "precall_routing": _observe_precall,
    "context_budget": _observe_context_budget,
    "tool_discovery": _observe_tool_discovery,
    "autoresearch_retrieval": _observe_autoresearch_retrieval,
    "code_retrieval": _observe_code_retrieval,
    "line_span_retrieval": _observe_line_span_retrieval,
    "memory_retrieval": _observe_memory_retrieval,
}


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _matches(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return pattern in text


def _field(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(path)
    return current


def _assert_expectation(expectation: dict[str, Any], observed: Observed) -> str | None:
    kind = str(expectation.get("kind") or "")
    text = _serialized(observed)

    if kind == "assert_blocked":
        expected_tool = str(expectation.get("tool") or "")
        pattern = str(expectation.get("pattern") or "")
        if observed.get("action") != "block":
            return f"expected block, observed action={observed.get('action')!r}"
        if expected_tool and observed.get("tool") != expected_tool:
            return f"expected tool={expected_tool!r}, observed={observed.get('tool')!r}"
        if pattern and not _matches(pattern, text):
            return f"blocked call did not match pattern={pattern!r}"
        return None

    if kind == "assert_no_match":
        pattern = str(expectation.get("pattern") or "")
        if pattern and _matches(pattern, text):
            return f"unexpected match for pattern={pattern!r}"
        return None

    if kind == "assert_size_under":
        threshold = int(expectation.get("bytes") or 0)
        actual = len(str(observed.get("additionalContext") or "").encode("utf-8"))
        if threshold <= 0 or actual >= threshold:
            return f"expected additionalContext bytes < {threshold}, observed={actual}"
        return None

    if kind == "assert_field_equals":
        path = str(expectation.get("path") or "")
        try:
            actual = _field(observed, path)
        except KeyError:
            return f"missing field path={path!r}"
        expected = expectation.get("value")
        if actual != expected:
            return f"expected {path}={expected!r}, observed={actual!r}"
        return None

    if kind in {"assert_field_at_most", "assert_field_at_least"}:
        path = str(expectation.get("path") or "")
        try:
            actual = _field(observed, path)
        except KeyError:
            return f"missing field path={path!r}"
        try:
            actual_number = float(actual)
            threshold = float(expectation.get("value"))
        except (TypeError, ValueError):
            return f"non-numeric comparison for {path!r}: {actual!r} vs {expectation.get('value')!r}"
        if kind == "assert_field_at_most" and actual_number > threshold:
            return f"expected {path} <= {threshold}, observed={actual_number}"
        if kind == "assert_field_at_least" and actual_number < threshold:
            return f"expected {path} >= {threshold}, observed={actual_number}"
        return None

    if kind == "assert_contains":
        path = str(expectation.get("path") or "")
        pattern = str(expectation.get("pattern") or "")
        try:
            target = str(_field(observed, path)) if path else text
        except KeyError:
            return f"missing field path={path!r}"
        if not _matches(pattern, target):
            return f"expected pattern={pattern!r} in {path or 'observed output'}"
        return None

    if kind == "assert_list_item_rank_at_most":
        path = str(expectation.get("path") or "")
        field = str(expectation.get("field") or "")
        expected = expectation.get("value")
        max_rank = int(expectation.get("rank") or 0)
        try:
            target = _field(observed, path)
        except KeyError:
            return f"missing field path={path!r}"
        if not isinstance(target, list):
            return f"expected list at path={path!r}, observed={type(target).__name__}"
        for rank, item in enumerate(target, start=1):
            if isinstance(item, dict) and item.get(field) == expected:
                if max_rank > 0 and rank <= max_rank:
                    return None
                return f"expected {field}={expected!r} at rank <= {max_rank}, observed rank={rank}"
        return f"expected list item with {field}={expected!r} at path={path!r}"

    if kind == "assert_action_logged":
        expected_action = str(expectation.get("action") or "")
        actions = observed.get("actions")
        if not isinstance(actions, list) or expected_action not in actions:
            return f"expected logged action={expected_action!r}"
        return None

    return f"unsupported assertion kind={kind!r}"


def _run_case(case: dict[str, Any], adapter: Adapter) -> dict[str, Any]:
    started = time.perf_counter_ns()
    case_id = str(case["id"])
    if case.get("human_review") is True:
        return {"id": case_id, "status": "skipped", "reason": "human_review_required"}
    try:
        observed = adapter(case)
        failures = [
            failure
            for expectation in case["expect"]
            if (failure := _assert_expectation(expectation, observed)) is not None
        ]
    except Exception as exc:
        observed = {"error": f"{type(exc).__name__}: {exc}"}
        failures = ["adapter_error"]
    duration_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    return {
        "id": case_id,
        "status": "failed" if failures else "passed",
        "duration_ms": duration_ms,
        "input": {key: value for key, value in case.items() if key != "expect"},
        "expected": case["expect"],
        "observed": observed,
        "failures": failures,
    }


def run_axis(axis: str, *, wired: bool) -> dict[str, Any]:
    started = time.perf_counter_ns()
    cases = list(load_cases(axis))
    adapter = ADAPTERS.get(axis)
    if not wired:
        results = [
            {"id": str(case["id"]), "status": "skipped", "reason": "runner_not_wired"}
            for case in cases
        ]
    elif adapter is None:
        results = [
            {"id": str(case["id"]), "status": "skipped", "reason": "axis_adapter_unsupported"}
            for case in cases
        ]
    else:
        results = [_run_case(case, adapter) for case in cases]

    passed = [result["id"] for result in results if result["status"] == "passed"]
    failed = [result["id"] for result in results if result["status"] == "failed"]
    skipped = [result["id"] for result in results if result["status"] == "skipped"]
    measured = len(passed) + len(failed)
    return {
        "axis": axis,
        "supported": adapter is not None,
        "wired": wired,
        "cases": len(cases),
        "measured": measured,
        "passed": len(passed),
        "failed": failed,
        "skipped": skipped,
        "pass_rate": round(len(passed) / measured, 4) if measured else None,
        "duration_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
        "case_results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--axis", action="append", help="axis to run; repeat for multiple axes")
    parser.add_argument("--all", action="store_true", help="run every axis under cases/")
    parser.add_argument("--wired", action="store_true", help="exercise supported production adapters")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any measured case fails")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="exit 2 when any selected case is skipped or its axis is unsupported",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = parser.parse_args(argv)

    if args.all:
        axes = sorted(path.stem for path in CASES_DIR.glob("*.jsonl"))
    elif args.axis:
        axes = list(dict.fromkeys(args.axis))
    else:
        parser.error("pass --axis <name> or --all")

    try:
        reports = [run_axis(axis, wired=args.wired) for axis in axes]
    except ValueError as exc:
        parser.error(str(exc))

    summary = {
        "axes": len(reports),
        "cases": sum(report["cases"] for report in reports),
        "measured": sum(report["measured"] for report in reports),
        "passed": sum(report["passed"] for report in reports),
        "failed": sum(len(report["failed"]) for report in reports),
        "skipped": sum(len(report["skipped"]) for report in reports),
    }
    payload = {"summary": summary, "reports": reports}

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        for report in reports:
            print(
                f"{report['axis']}: {report['passed']}/{report['measured']} measured passed, "
                f"{len(report['skipped'])} skipped"
            )
        print(
            f"total: {summary['passed']}/{summary['measured']} measured passed, "
            f"{summary['skipped']} skipped"
        )

    if args.strict and summary["failed"]:
        return 1
    if args.require_complete and summary["skipped"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
