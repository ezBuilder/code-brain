"""Multi-project federated recommendation.

Scans the user's workspace for other Code Brain installations (`.ai/generated/install-manifest.json`)
and surfaces patterns that recur across multiple projects — common decision tags,
common todos, common precall rule kinds. The point is to suggest *cross-project*
skills/rules without leaking project-specific text.

Read-only, redacted, no network. Each cross-project record carries only frequency
counts and short canonical labels — never raw decision/todo text from another
project.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .memory import read_jsonl_all, read_jsonl_tail


def discover_installations(home: Path | None = None) -> list[Path]:
    """Find every Code Brain install under the user's home (workspace dirs)."""
    h = home or Path.home()
    candidates: list[Path] = []
    workspaces = [h / "workspace", h / "Projects", h / "projects", h / "src"]
    for ws in workspaces:
        if not ws.is_dir():
            continue
        for child in ws.iterdir():
            if not child.is_dir():
                continue
            manifest = child / ".ai" / "generated" / "install-manifest.json"
            if manifest.exists():
                candidates.append(child)
    return sorted(set(candidates))


def gather_cross_project_signals(
    self_root: Path,
    *,
    home: Path | None = None,
    include_self: bool = False,
) -> dict[str, Any]:
    """Aggregate decision tags + todo title bigrams + precall rule kinds across
    other Code Brain projects on this machine.

    Returns counts only — never includes another project's raw text. The caller
    can use this as evidence ("3 of your 5 projects share tag 'release'").
    """
    self_resolved = self_root.resolve()
    projects = discover_installations(home=home)
    if not include_self:
        projects = [p for p in projects if p.resolve() != self_resolved]

    decision_tags: Counter[str] = Counter()
    todo_bigrams: Counter[str] = Counter()
    precall_kinds: Counter[str] = Counter()
    skills_slugs: Counter[str] = Counter()

    for proj in projects:
        try:
            for entry in read_jsonl_tail(proj / ".ai" / "memory" / "decisions.jsonl", 200):
                for tag in entry.get("tags") or []:
                    t = str(tag).strip().lower()
                    if t:
                        decision_tags[t] += 1
            for entry in read_jsonl_all(proj / ".ai" / "memory" / "todos.jsonl"):
                title = str(entry.get("title") or "").lower().strip()
                if not title:
                    continue
                tokens = [t for t in title.split() if t and t.isalpha()]
                for i in range(len(tokens) - 1):
                    todo_bigrams[f"{tokens[i]} {tokens[i+1]}"] += 1
            for entry in read_jsonl_all(proj / ".ai" / "precall_rules" / "catalog.jsonl"):
                kind = str(entry.get("kind") or "").strip().lower()
                if kind:
                    precall_kinds[kind] += 1
            for entry in read_jsonl_all(proj / ".ai" / "skills" / "catalog.jsonl"):
                slug = str(entry.get("slug") or "").strip().lower()
                status = str(entry.get("status") or "").lower()
                if slug and status in ("installed", "accepted"):
                    skills_slugs[slug] += 1
        except Exception:
            continue

    return {
        "ok": True,
        "scanned_projects": len(projects),
        "decision_tags": dict(decision_tags.most_common(20)),
        "todo_bigrams": dict(todo_bigrams.most_common(20)),
        "precall_kinds": dict(precall_kinds.most_common(10)),
        "skills_slugs": dict(skills_slugs.most_common(20)),
    }


def cross_project_summary(self_root: Path, *, home: Path | None = None) -> dict[str, Any]:
    """Top-level summary suitable for `cb-federated` slash command output."""
    sig = gather_cross_project_signals(self_root, home=home)
    n = sig.get("scanned_projects", 0)
    if n == 0:
        return {"ok": True, "scanned_projects": 0, "note": "no_other_installs"}
    return {
        "ok": True,
        "scanned_projects": n,
        "common_tags": [
            {"tag": k, "projects": v}
            for k, v in sig.get("decision_tags", {}).items()
            if v >= 2
        ][:5],
        "common_todo_patterns": [
            {"bigram": k, "projects": v}
            for k, v in sig.get("todo_bigrams", {}).items()
            if v >= 2
        ][:5],
        "common_precall_kinds": [
            {"kind": k, "projects": v}
            for k, v in sig.get("precall_kinds", {}).items()
            if v >= 2
        ],
        "common_skills": [
            {"slug": k, "projects": v}
            for k, v in sig.get("skills_slugs", {}).items()
            if v >= 2
        ],
    }
