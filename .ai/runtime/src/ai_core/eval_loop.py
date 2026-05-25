from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import redact_value

CASES_PATH = (".ai", "eval", "cases.jsonl")
PASS_OUTCOMES = {"pass", "passed", "ok", "success"}
LATEST_FAILURE_LIMIT = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _cases_path(root: Path) -> Path:
    return root.joinpath(*CASES_PATH)


def _ensure_eval_dir(root: Path) -> Path:
    eval_dir = _cases_path(root).parent
    eval_dir.mkdir(parents=True, exist_ok=True)
    try:
        eval_dir.chmod(0o700)
    except OSError:
        pass
    return eval_dir


def _normalize_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _is_pass(outcome: str) -> bool:
    return outcome.strip().lower() in PASS_OUTCOMES


def record_case(
    root: Path,
    *,
    kind: str,
    command: str,
    outcome: str,
    duration_ms: int,
    case_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")

    case = {
        "id": _normalize_text(case_id or f"eval-{uuid.uuid4().hex[:12]}", "id"),
        "kind": _normalize_text(kind, "kind"),
        "command": _normalize_text(command, "command"),
        "outcome": _normalize_text(outcome, "outcome"),
        "duration_ms": int(duration_ms),
        "created_at": created_at or _now_iso(),
    }
    redacted = redact_value(case)
    _ensure_eval_dir(root)
    path = _cases_path(root)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(redacted, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass

    # Signal failure to lessons (separate try/except; must not block record_case)
    if not _is_pass(redacted.get("outcome", "")):
        try:
            from . import lessons as lessons_module
            lessons_module.append_lesson(
                root,
                kind=redacted["kind"],
                command=redacted["command"],
                outcome=redacted["outcome"],
                details=f"duration_ms={redacted.get('duration_ms', 0)}",
            )
        except (OSError, ValueError, json.JSONDecodeError, Exception):
            pass  # Silent fail: record_case always succeeds

    return {"ok": True, "case": redacted, "path": str(path)}


def _iter_cases(root: Path) -> list[dict[str, Any]]:
    path = _cases_path(root)
    if not path.exists():
        return []
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            cases.append(loaded)
    return cases


def summarize_cases(root: Path, *, latest_limit: int = LATEST_FAILURE_LIMIT) -> dict[str, Any]:
    cases = _iter_cases(root)
    passed = sum(1 for case in cases if _is_pass(str(case.get("outcome", ""))))
    total = len(cases)
    failed = total - passed
    failures = [case for case in cases if not _is_pass(str(case.get("outcome", "")))]
    latest_failures = list(reversed(failures[-latest_limit:])) if latest_limit > 0 else []
    return redact_value(
        {
            "ok": True,
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "latest_failures": latest_failures,
            "path": str(_cases_path(root)),
        }
    )
