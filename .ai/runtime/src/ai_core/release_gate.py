from __future__ import annotations

import importlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .doctor import as_payload, run_checks
from .redact import redact_value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def summary(root: Path) -> dict[str, Any]:
    """Read-only release dashboard. No network, no writes."""
    doctor = as_payload(run_checks(root))
    git = git_dirty_summary(root)
    eval_block = eval_summary(root)
    recommendations = recommendation_status(root)
    gates = {
        "doctor": bool(doctor.get("ok")),
        "git_clean": not bool(git.get("dirty")),
        "eval": eval_block.get("ok") is not False,
    }
    payload = {
        "ok": all(gates.values()),
        "schema_version": 1,
        "generated_at": now_iso(),
        "runtime_version": __version__,
        "mode": {"read_only": True, "network": "disabled"},
        "gates": gates,
        "doctor": doctor,
        "git": git,
        "eval": eval_block,
        "recommendations": recommendations,
    }
    return redact_value(payload)


def git_dirty_summary(root: Path) -> dict[str, Any]:
    branch = _git(root, "branch", "--show-current")
    head = _git(root, "rev-parse", "--short=12", "HEAD")
    porcelain = _git_status(root)
    if porcelain is None:
        return {
            "ok": False,
            "available": False,
            "branch": None,
            "head": None,
            "dirty": None,
            "changed": 0,
            "staged": 0,
            "unstaged": 0,
            "untracked": 0,
            "by_status": {},
            "sample": [],
        }
    lines = [line for line in porcelain.splitlines() if line.strip()]
    by_status: Counter[str] = Counter()
    staged = 0
    unstaged = 0
    untracked = 0
    sample: list[dict[str, str]] = []
    for line in lines:
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        by_status[status] += 1
        if status == "??":
            untracked += 1
        else:
            if status[0] != " ":
                staged += 1
            if len(status) > 1 and status[1] != " ":
                unstaged += 1
        if len(sample) < 20:
            sample.append({"status": status, "path": path})
    return {
        "ok": True,
        "available": True,
        "branch": branch or None,
        "head": head or None,
        "dirty": bool(lines),
        "changed": len(lines),
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "by_status": dict(sorted(by_status.items())),
        "sample": sample,
    }


def eval_summary(root: Path) -> dict[str, Any]:
    for rel in (
        "dist/eval-summary.json",
        "dist/eval_summary.json",
        ".ai/cache/eval/summary.json",
        ".ai/eval/summary.json",
    ):
        path = root / rel
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"present": True, "ok": False, "source": rel, "error": str(exc)}
        if not isinstance(payload, dict):
            return {"present": True, "ok": False, "source": rel, "error": "invalid-schema"}
        return {"present": True, "source": rel, **payload}
    optional = _optional_eval_summary(root)
    if optional is not None:
        return optional
    return {"present": False, "ok": None, "source": None}


def recommendation_status(root: Path) -> dict[str, Any]:
    return {
        "skills": _catalog_status(root, ".ai/skills/catalog.jsonl", _skill_entries),
        "agents": _catalog_status(root, ".ai/agents_catalog/catalog.jsonl", _agent_entries),
        "precall": _catalog_status(root, ".ai/precall_rules/catalog.jsonl", _precall_entries),
    }


def _catalog_status(
    root: Path,
    rel: str,
    loader: Callable[[Path], list[dict[str, Any]]],
) -> dict[str, Any]:
    path = root / rel
    if not path.exists():
        return {"present": False, "total": 0, "by_status": {}, "sample": []}
    try:
        rows = loader(root)
    except Exception as exc:
        return {"present": True, "ok": False, "total": 0, "by_status": {}, "sample": [], "error": str(exc)}
    by_status = Counter(str(row.get("status") or "unknown") for row in rows)
    return {
        "present": True,
        "ok": True,
        "total": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "sample": rows[:10],
    }


def _skill_entries(root: Path) -> list[dict[str, Any]]:
    from .recommend import list_visible

    return list_visible(root)


def _agent_entries(root: Path) -> list[dict[str, Any]]:
    from .agent_recommend import list_visible

    return list_visible(root)


def _precall_entries(root: Path) -> list[dict[str, Any]]:
    from .precall_recommend import list_visible

    return list_visible(root)


def _optional_eval_summary(root: Path) -> dict[str, Any] | None:
    for module_name in ("ai_core.eval", "ai_core.evals", "ai_core.eval_loop"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for func_name in ("summary", "eval_summary", "status", "summarize_cases"):
            func = getattr(module, func_name, None)
            if not callable(func):
                continue
            try:
                if func_name == "summarize_cases":
                    payload = func(root, latest_limit=5)
                else:
                    payload = func(root)
            except TypeError:
                try:
                    payload = func(root)
                except TypeError:
                    payload = func()
            if isinstance(payload, dict):
                return {"present": True, "source": f"{module_name}.{func_name}", **payload}
    return None


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_status(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")
