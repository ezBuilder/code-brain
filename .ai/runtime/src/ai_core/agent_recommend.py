"""Sub-agent recommendation engine.

Mines accumulated decisions/todos/audit + Claude transcripts for repeating
sub-agent invocation intents (Explore, Plan, code-reviewer, etc.) and proposes
project-local `.claude/agents/<name>.md` definitions.

Heuristic-only — no LLM, no network. Reuses recommend.py catalog patterns.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import (
    append_audit,
    append_jsonl,
    decisions_path,
    now_iso,
    read_jsonl_all,
    read_jsonl_tail,
    todos_path,
)
from .portable import hyphen_encode_path
from .redact import redact_value

CATALOG_PATH_PARTS = (".ai", "agents_catalog", "catalog.jsonl")
DEFAULT_LIMIT = 5
DEFAULT_MIN_SIGNAL = 3
DANGER_PATTERNS = (
    re.compile(r"<system-reminder", re.IGNORECASE),
    re.compile(r"</?system\b", re.IGNORECASE),
    re.compile(r"ignore\s+(previous|prior|all)\s+instructions?", re.IGNORECASE),
)
SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class AgentCandidate:
    id: str
    slug: str
    description: str
    body: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentCatalogEntry:
    id: str
    slug: str
    status: str
    description: str
    body: str
    body_sha256: str
    installed_paths: list[str]
    created_at: str

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "status": self.status,
            "description": self.description,
            "body": self.body,
            "body_sha256": self.body_sha256,
            "installed_paths": self.installed_paths,
            "created_at": self.created_at,
        }


def catalog_path(root: Path) -> Path:
    return root.joinpath(*CATALOG_PATH_PARTS)


def _slugify(text: str) -> str:
    out = SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return out[:48] or "agent"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate_id(slug: str, body: str) -> str:
    return "ag-" + _sha256(slug + "\x00" + body)[:8]


def _danger_match(text: str) -> bool:
    return any(p.search(text) for p in DANGER_PATTERNS)


def _gather_subagent_intents(root: Path) -> Counter[str]:
    """Counter of subagent_type values invoked from this project's Claude transcripts."""
    home = Path("~/.claude").expanduser()
    proj = home / "projects" / hyphen_encode_path(str(root))
    if not proj.is_dir():
        return Counter()
    counts: Counter[str] = Counter()
    for sess in sorted(proj.glob("*.jsonl"))[:30]:
        try:
            text = sess.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"Agent"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use" and item.get("name") == "Agent":
                    inp = item.get("input") or {}
                    if isinstance(inp, dict):
                        sub = inp.get("subagent_type")
                        if isinstance(sub, str) and sub.strip():
                            counts[sub.strip()] += 1
    return counts


def _gather_decision_tags(root: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for entry in read_jsonl_tail(decisions_path(root), 200):
        for tag in entry.get("tags") or []:
            t = str(tag).strip().lower()
            if t:
                counts[t] += 1
    return counts


def _draft_agent_body(slug: str, focus_topic: str, evidence_lines: list[str]) -> str:
    """Compose a `.claude/agents/<slug>.md` body. Plain text per cb-* convention."""
    bullets = "\n".join(f"- {line}" for line in evidence_lines[:5])
    body = (
        f"You are an isolated sub-agent specialized in {focus_topic} for this project.\n\n"
        "Read-only investigation by default — do not edit code unless the parent agent\n"
        "explicitly delegates writes. Use Code Brain MCP tools (`code_query`, `context_pack`,\n"
        "`memory_query`) before falling back to bash grep/find.\n\n"
        "When asked, return a short bulleted report (≤200 words) with file paths and line\n"
        "numbers. Cite memory entries (decisions, todos) verbatim — do not paraphrase.\n\n"
        f"Recurring evidence from this project's memory:\n{bullets}\n"
    )
    return body


# ---------- recommend ----------

def cluster_candidates(
    root: Path,
    *,
    min_signal: int = DEFAULT_MIN_SIGNAL,
    limit: int = DEFAULT_LIMIT,
) -> list[AgentCandidate]:
    out: list[AgentCandidate] = []

    intents = _gather_subagent_intents(root)
    for sub_type, count in intents.most_common():
        if count < min_signal:
            continue
        slug = _slugify(f"{sub_type}-helper")
        desc = f"이 프로젝트에서 {sub_type} 서브에이전트가 {count}회 호출됨 — 프로젝트 전용 helper로 승격."
        body = _draft_agent_body(
            slug,
            sub_type,
            evidence_lines=[f"sub-agent type {sub_type} invoked {count} times"],
        )
        if _danger_match(body):
            continue
        cid = _candidate_id(slug, body)
        out.append(
            AgentCandidate(
                id=cid, slug=slug, description=desc, body=body,
                evidence={"signals": [f"transcripts:{count}"], "rationale": f"`{sub_type}` 호출 {count}회"},
            )
        )

    tags = _gather_decision_tags(root)
    for tag, count in tags.most_common():
        if count < min_signal:
            continue
        slug = _slugify(f"{tag}-investigator")
        desc = f"'{tag}' 도메인 결정이 {count}건 누적됨 — 도메인 전용 investigator 후보."
        body = _draft_agent_body(
            slug,
            f"the '{tag}' domain",
            evidence_lines=[f"decisions tagged '{tag}': {count}"],
        )
        if _danger_match(body):
            continue
        cid = _candidate_id(slug, body)
        out.append(
            AgentCandidate(
                id=cid, slug=slug, description=desc, body=body,
                evidence={"signals": [f"decisions:{count}"], "rationale": f"tag '{tag}' {count} decisions"},
            )
        )

    seen_slugs = set()
    deduped = []
    for c in out:
        if c.slug in seen_slugs:
            continue
        seen_slugs.add(c.slug)
        deduped.append(c)
    return deduped[:limit]


def list_catalog(root: Path) -> list[AgentCatalogEntry]:
    p = catalog_path(root)
    if not p.exists():
        return []
    seen: dict[str, AgentCatalogEntry] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        seen[str(rec["id"])] = AgentCatalogEntry(
            id=str(rec["id"]),
            slug=str(rec.get("slug") or ""),
            status=str(rec.get("status") or "pending"),
            description=str(rec.get("description") or ""),
            body=str(rec.get("body") or ""),
            body_sha256=str(rec.get("body_sha256") or ""),
            installed_paths=list(rec.get("installed_paths") or []),
            created_at=str(rec.get("created_at") or ""),
        )
    return list(seen.values())


def _persist(root: Path, entry: AgentCatalogEntry) -> None:
    append_jsonl(catalog_path(root), entry.to_record())


def recommend(
    root: Path, *, limit: int = DEFAULT_LIMIT, min_signal: int = DEFAULT_MIN_SIGNAL,
) -> dict[str, Any]:
    cands = cluster_candidates(root, min_signal=min_signal, limit=limit)
    if not cands:
        return {"ok": True, "candidates": [], "note": "signals_below_threshold"}
    existing = {e.id: e for e in list_catalog(root)}
    seen_slugs = {e.slug: e.status for e in existing.values()}
    out: list[dict[str, Any]] = []
    for c in cands:
        if c.id in existing and existing[c.id].status != "pending":
            continue
        if seen_slugs.get(c.slug) in ("rejected", "installed", "uninstalled"):
            continue
        if c.id not in existing:
            entry = AgentCatalogEntry(
                id=c.id, slug=c.slug, status="pending",
                description=c.description, body=c.body,
                body_sha256=_sha256(c.body),
                installed_paths=[], created_at=now_iso(),
            )
            _persist(root, entry)
            append_audit(
                root, action="agent.recommend_pending", category="memory",
                payload={"id": c.id, "slug": c.slug},
            )
        out.append({
            "id": c.id, "slug": c.slug, "description": c.description,
            "body": c.body, "evidence": c.evidence, "status": "pending",
        })
    return {"ok": True, "candidates": out}


def _frontmatter(slug: str, description: str, cid: str, body_sha: str) -> str:
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {description[:160]}\n"
        "managed-by: code-brain\n"
        f"catalog-id: {cid}\n"
        f"body-sha256: {body_sha}\n"
        "---\n"
    )


def _read_marker(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    fm = text[4:end]
    out: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    out["__body__"] = text[end + 5:]
    return out


def accept(root: Path, candidate_id: str) -> dict[str, Any]:
    existing = {e.id: e for e in list_catalog(root)}
    entry = existing.get(candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status not in ("pending",):
        return {"ok": False, "reason": f"status_{entry.status}"}
    body = redact_value(entry.body)
    if _danger_match(body):
        rejected = AgentCatalogEntry(
            id=entry.id, slug=entry.slug, status="rejected",
            description=entry.description, body=body, body_sha256=_sha256(body),
            installed_paths=[], created_at=entry.created_at,
        )
        _persist(root, rejected)
        return {"ok": False, "reason": "danger_pattern"}
    if not body.startswith("\n"):
        body = "\n" + body
    body_sha = _sha256(body)
    fm = _frontmatter(entry.slug, entry.description, entry.id, body_sha)
    target = root / ".claude" / "agents" / f"{entry.slug}.md"
    if target.exists():
        m = _read_marker(target)
        if m.get("managed-by") != "code-brain":
            return {"ok": False, "reason": "user_owned_target", "path": str(target.relative_to(root))}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fm + body, encoding="utf-8")
    installed = [target.relative_to(root).as_posix()]
    accepted = AgentCatalogEntry(
        id=entry.id, slug=entry.slug, status="installed",
        description=entry.description, body=body, body_sha256=body_sha,
        installed_paths=installed, created_at=entry.created_at,
    )
    _persist(root, accepted)
    append_audit(
        root, action="agent.accept_install", category="memory",
        payload={"id": entry.id, "slug": entry.slug},
    )
    return {"ok": True, "id": entry.id, "slug": entry.slug, "installed_paths": installed}


def reject(root: Path, candidate_id: str) -> dict[str, Any]:
    existing = {e.id: e for e in list_catalog(root)}
    entry = existing.get(candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status == "installed":
        return {"ok": False, "reason": "already_installed"}
    rejected = AgentCatalogEntry(
        id=entry.id, slug=entry.slug, status="rejected",
        description=entry.description, body=entry.body, body_sha256=entry.body_sha256,
        installed_paths=[], created_at=entry.created_at,
    )
    _persist(root, rejected)
    append_audit(root, action="agent.reject", category="memory", payload={"id": entry.id})
    return {"ok": True, "id": entry.id}


def uninstall(root: Path, slug: str, *, force: bool = False) -> dict[str, Any]:
    last = None
    for e in list_catalog(root):
        if e.slug == slug:
            last = e
    if last is None or last.status != "installed":
        return {"ok": False, "reason": "not_installed"}
    drift = []
    for rel in last.installed_paths:
        path = root / rel
        if path.exists():
            m = _read_marker(path)
            disk_sha = _sha256(m.get("__body__", ""))
            if last.body_sha256 and disk_sha != last.body_sha256:
                drift.append(rel)
    if drift and not force:
        return {"ok": False, "reason": "drift_detected", "paths": drift}
    removed = []
    for rel in last.installed_paths:
        path = root / rel
        if path.exists():
            path.unlink()
            removed.append(rel)
    uninstalled = AgentCatalogEntry(
        id=last.id, slug=last.slug, status="uninstalled",
        description=last.description, body=last.body, body_sha256=last.body_sha256,
        installed_paths=[], created_at=last.created_at,
    )
    _persist(root, uninstalled)
    append_audit(
        root, action="agent.uninstall", category="memory",
        payload={"id": last.id, "slug": last.slug, "force": force, "drift": bool(drift)},
    )
    return {"ok": True, "slug": slug, "removed": removed}


def list_visible(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "id": e.id, "slug": e.slug, "status": e.status,
            "description": e.description[:160], "installed_paths": e.installed_paths,
            "created_at": e.created_at,
        }
        for e in list_catalog(root)
    ]
