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
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .memory import read_jsonl_all, read_jsonl_tail


_FEDERATED_CACHE_TTL_SECONDS = 300


def _federated_cache_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "federated_hot.json"


def _federated_cache_enabled() -> bool:
    val = os.environ.get("AI_FEDERATED_CACHE")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "no", "off")


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


def _compute_cross_project_summary(self_root: Path, *, home: Path | None = None) -> dict[str, Any]:
    """Compute fresh cross-project summary (no cache)."""
    sig = gather_cross_project_signals(self_root, home=home)
    n = sig.get("scanned_projects", 0)
    if n == 0:
        return {"ok": True, "scanned_projects": 0, "note": "no_other_installs"}
    self_resolved = self_root.resolve()
    others = [p for p in discover_installations(home=home) if p.resolve() != self_resolved]
    antigravity_coverage = sum(
        1 for p in others if (p / ".agents" / "mcp_config.json").exists()
    )
    return {
        "ok": True,
        "scanned_projects": n,
        "antigravity_coverage": {
            "wired": antigravity_coverage,
            "total": len(others),
        },
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


def _write_federated_cache(cache_path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON payload to cache_path via .tmp + os.replace."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError:
        pass


def _audit_index_newer_than(cache_mtime: float, *, home: Path | None = None) -> bool:
    """Return True if any scanned project's audit-index.jsonl is newer than cache_mtime."""
    for proj in discover_installations(home=home):
        idx = proj / ".ai" / "memory" / "audit-index.jsonl"
        try:
            if idx.exists() and idx.stat().st_mtime > cache_mtime:
                return True
        except OSError:
            continue
    return False


def cross_project_summary(self_root: Path, *, home: Path | None = None) -> dict[str, Any]:
    """Top-level summary suitable for `cb-federated` slash command output.

    Stale-while-revalidate cache wrapper around ``_compute_cross_project_summary``.
    Cache is invalidated when older than ``_FEDERATED_CACHE_TTL_SECONDS`` or when
    any scanned project's ``audit-index.jsonl`` is newer than the cache file.

    Bypass via ``AI_FEDERATED_CACHE=0`` (or false/no/off).
    """
    if not _federated_cache_enabled():
        return _compute_cross_project_summary(self_root, home=home)

    cache_path = _federated_cache_path(self_root)
    try:
        if cache_path.exists():
            st = cache_path.stat()
            age = time.time() - st.st_mtime
            if age < _FEDERATED_CACHE_TTL_SECONDS and not _audit_index_newer_than(st.st_mtime, home=home):
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    fresh = _compute_cross_project_summary(self_root, home=home)
    _write_federated_cache(cache_path, fresh)
    return fresh
