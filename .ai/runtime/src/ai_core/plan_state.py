"""Durable per-plan step progress — the checkbox IS the state (G2, OmO Boulder-inspired).

OmO keeps task progress in a human-readable plan's `- [x]` checkboxes, not a JSON blob, so it
survives crashes and context compaction: the truth is re-derived from disk on every read. CB's
queue already gives request-level crash recovery (inbox/processing/done/dead + lease); the gap
this fills is *ordered per-step progress within one goal*.

Self-contained: plans live under `.ai/memory/plans/<plan_id>/plan.md` (NOT the loop queue, which
stays the single recovery authority for requests). Pure parser, re-derived every read, atomic
rewrite, redacted labels. stdlib only; no LLM, no network. OmO's `## TODOs`/`N.`/`FN.` authoring
conventions are deliberately NOT imported — CB defines its own minimal `## Steps` convention.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .redact import redact_value

_PLAN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s+(.+?)\s*$")
_LABEL_MAX = 240
MAX_STEPS = 200


def _safe_plan_id(plan_id: str) -> str:
    pid = str(plan_id or "").strip()
    if not _PLAN_ID_RE.fullmatch(pid):
        raise ValueError("invalid plan_id (use [A-Za-z0-9_-], <=64 chars)")
    return pid


def plans_root(root: Path) -> Path:
    return Path(root) / ".ai" / "memory" / "plans"


def plan_path(root: Path, plan_id: str) -> Path:
    return plans_root(root) / _safe_plan_id(plan_id) / "plan.md"


def parse_steps(text: str) -> list[dict[str, Any]]:
    """Parse `- [ ]` / `- [x]` checkbox lines into ordered steps. Pure; ignores other lines."""
    steps: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        steps.append({"label": m.group(2).strip()[:_LABEL_MAX], "done": m.group(1).lower() == "x"})
        if len(steps) >= MAX_STEPS:
            break
    return steps


def render(steps: list[dict[str, Any]], *, title: str = "") -> str:
    lines = [f"# Plan: {title}".rstrip(), "", "## Steps", ""]
    for s in steps:
        box = "x" if s.get("done") else " "
        lines.append(f"- [{box}] {str(s.get('label', '')).strip()[:_LABEL_MAX]}")
    return "\n".join(lines) + "\n"


def _summarize(plan_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(steps)
    completed = sum(1 for s in steps if s.get("done"))
    next_label = next((s["label"] for s in steps if not s.get("done")), None)
    return {
        "ok": True,
        "plan_id": plan_id,
        "steps": steps,
        "total": total,
        "completed": completed,
        "remaining": total - completed,
        "next_label": next_label,
    }


def init_plan(root: Path, *, plan_id: str, steps: list[str], title: str = "", force: bool = False) -> dict[str, Any]:
    """Create a plan from step labels. Refuses to clobber an existing plan unless force."""
    pid = _safe_plan_id(plan_id)
    path = plan_path(root, pid)
    if path.exists() and not force:
        return {"ok": False, "reason": "plan_exists", "plan_id": pid}
    clean = [{"label": str(redact_value(str(s))).strip()[:_LABEL_MAX], "done": False}
             for s in (steps or []) if str(s).strip()][:MAX_STEPS]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render(clean, title=str(redact_value(title))[:120]), encoding="utf-8")
    tmp.replace(path)
    return _summarize(pid, clean)


def read_plan(root: Path, plan_id: str) -> dict[str, Any]:
    """Re-derive plan state from disk every call (never trust an in-memory copy). Fail-soft."""
    pid = _safe_plan_id(plan_id)
    path = plan_path(root, pid)
    if not path.exists():
        return {"ok": False, "reason": "not_found", "plan_id": pid}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"ok": False, "reason": "read_error", "plan_id": pid}
    return _summarize(pid, parse_steps(text))


def mark_step(root: Path, *, plan_id: str, match: str | None = None, index: int | None = None,
              done: bool = True) -> dict[str, Any]:
    """Toggle a step by 1-based index or case-insensitive label substring. Atomic rewrite."""
    pid = _safe_plan_id(plan_id)
    state = read_plan(root, pid)
    if not state.get("ok"):
        return state
    steps = state["steps"]
    target = None
    if index is not None:
        if 1 <= int(index) <= len(steps):
            target = int(index) - 1
    elif match:
        needle = str(match).strip().lower()
        for i, s in enumerate(steps):
            if needle and needle in str(s["label"]).lower():
                target = i
                break
    if target is None:
        return {"ok": False, "reason": "step_not_found", "plan_id": pid}
    steps[target]["done"] = bool(done)
    path = plan_path(root, pid)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render(steps), encoding="utf-8")
    tmp.replace(path)
    return _summarize(pid, steps)


def list_plans(root: Path) -> dict[str, Any]:
    base = plans_root(root)
    items: list[dict[str, Any]] = []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            p = d / "plan.md"
            if p.is_file():
                st = read_plan(root, d.name)
                if st.get("ok"):
                    items.append({"plan_id": st["plan_id"], "completed": st["completed"],
                                  "total": st["total"], "remaining": st["remaining"]})
    return {"ok": True, "count": len(items), "plans": items}


def active_summary(root: Path) -> dict[str, Any] | None:
    """The most-recently-modified plan that still has remaining steps (for context surfacing)."""
    base = plans_root(root)
    if not base.is_dir():
        return None
    best: tuple[float, str] | None = None
    for d in base.iterdir():
        p = d / "plan.md"
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        st = read_plan(root, d.name)
        if st.get("ok") and st["remaining"] > 0 and (best is None or mtime > best[0]):
            best = (mtime, d.name)
    if best is None:
        return None
    return read_plan(root, best[1])
