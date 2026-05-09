"""Slash-command recommendation engine.

Reads accumulated cross-session memory (decisions, todos, audit, session notes,
optionally Claude/Codex global memory filtered to the current project) and
clusters repeating patterns into candidate slash commands.

Heuristic-only — no LLM calls, no network. All write paths go through
`memory.append_jsonl`/`memory.append_audit`. Drafts are run through
`redact.redact_value` and danger-pattern filtering before being persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .memory import (
    append_audit,
    append_jsonl,
    audit_path,
    decisions_path,
    now_iso,
    read_jsonl_all,
    read_jsonl_open_todos,
    read_jsonl_tail,
    read_text_tail,
    session_current_path,
    todos_path,
)
from .portable import hyphen_encode_path
from .redact import redact_value

CATALOG_PATH_PARTS = (".ai", "skills", "catalog.jsonl")
MAX_CANDIDATES_DEFAULT = 5
MIN_SIGNAL_DEFAULT = 3
MAX_BODY_BYTES = 8192
SLUG_RE = re.compile(r"[^a-z0-9]+")
DANGER_PATTERNS = (
    re.compile(r"<system-reminder", re.IGNORECASE),
    re.compile(r"</?system\b", re.IGNORECASE),
    re.compile(r"ignore\s+(previous|prior|all)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(the\s+)?(above|previous)", re.IGNORECASE),
    re.compile(r"</?assistant>", re.IGNORECASE),
    re.compile(r"</?user>", re.IGNORECASE),
)
KOREAN_VERB_HINTS = ("하기", "하라", "추가", "삭제", "수정", "확인", "검증", "배포", "롤백", "정리")
ENGLISH_VERB_HINTS = (
    "fix", "add", "remove", "update", "verify", "deploy", "rollback",
    "refactor", "migrate", "test", "build", "release", "investigate",
    "audit", "harden", "implement", "wire",
)


@dataclass
class Signals:
    decisions: list[dict[str, Any]] = field(default_factory=list)
    todos_open: list[dict[str, Any]] = field(default_factory=list)
    todos_all: list[dict[str, Any]] = field(default_factory=list)
    audit_actions: list[str] = field(default_factory=list)
    session_tail: str = ""
    global_claude_titles: list[str] = field(default_factory=list)
    global_codex_threads: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Candidate:
    id: str
    slug: str
    description: str
    body: str
    evidence: dict[str, Any]
    rejected_reason: str | None = None


@dataclass
class CatalogEntry:
    id: str
    slug: str
    status: str
    draft: dict[str, Any]
    evidence: dict[str, Any]
    created_at: str
    installed_paths: list[str]
    body_sha256: str

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "status": self.status,
            "draft": self.draft,
            "evidence": self.evidence,
            "created_at": self.created_at,
            "installed_paths": self.installed_paths,
            "body_sha256": self.body_sha256,
        }


# ---------- helpers ----------

def catalog_path(root: Path) -> Path:
    return root.joinpath(*CATALOG_PATH_PARTS)


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = SLUG_RE.sub("-", text)
    text = text.strip("-")
    return text[:48] or "skill"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate_id(slug: str, body: str) -> str:
    return "sk-" + _sha256(slug + "\x00" + body)[:8]


def _danger_match(text: str) -> str | None:
    for pat in DANGER_PATTERNS:
        m = pat.search(text)
        if m:
            return m.re.pattern
    return None


def _hyphen_encode_path(path: Path) -> str:
    return hyphen_encode_path(str(path))


def _claude_global_dir(home: Path, project_root: Path) -> Path:
    return home / ".claude" / "projects" / hyphen_encode_path(str(project_root))


def _codex_memories_path(home: Path) -> Path:
    return home / ".codex" / "memories" / "raw_memories.md"


# ---------- gather ----------

def gather_signals(
    root: Path,
    *,
    include_global: bool = True,
    home: Path | None = None,
) -> Signals:
    sig = Signals()
    sig.decisions = read_jsonl_tail(decisions_path(root), 200)
    sig.todos_open = read_jsonl_open_todos(todos_path(root), 200)
    sig.todos_all = read_jsonl_all(todos_path(root))
    sig.session_tail = read_text_tail(session_current_path(root), 200)

    audit = read_jsonl_tail(audit_path(root), 500)
    sig.audit_actions = [
        f"{e.get('category', '?')}.{str(e.get('action') or '').split('.', 1)[-1]}"
        for e in audit
    ]

    if include_global:
        h = home or Path.home()
        sig.global_claude_titles = _gather_claude_global(h, root)
        sig.global_codex_threads = _gather_codex_global(h, root)
    return sig


def _gather_claude_global(home: Path, root: Path) -> list[str]:
    proj_dir = _claude_global_dir(home, root)
    titles: list[str] = []
    mem_dir = proj_dir / "memory"
    if mem_dir.is_dir():
        for entry in sorted(mem_dir.glob("*.md"))[:50]:
            stem = entry.stem
            if stem in {"MEMORY", "memory_summary"}:
                head = read_text_tail(entry, 2)
                if head:
                    titles.append(head.splitlines()[0][:160])
            else:
                titles.append(stem.replace("_", " ").replace("-", " ")[:160])
    return titles


def _gather_codex_global(home: Path, root: Path) -> list[dict[str, Any]]:
    path = _codex_memories_path(home)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    threads: list[dict[str, Any]] = []
    target_cwd = str(root)
    blocks = re.split(r"^## Thread `", text, flags=re.MULTILINE)[1:]
    for block in blocks:
        m_cwd = re.search(r"^cwd:\s*(.+)$", block, re.MULTILINE)
        m_task = re.search(r"^task_group:\s*(.+)$", block, re.MULTILINE)
        m_outcome = re.search(r"^task_outcome:\s*(.+)$", block, re.MULTILINE)
        m_keywords = re.search(r"^keywords:\s*(.+)$", block, re.MULTILINE)
        if not m_cwd:
            continue
        cwd_val = m_cwd.group(1).strip()
        if not cwd_val.startswith(target_cwd):
            continue
        threads.append(
            {
                "cwd": cwd_val,
                "task_group": m_task.group(1).strip() if m_task else "",
                "task_outcome": m_outcome.group(1).strip() if m_outcome else "",
                "keywords": m_keywords.group(1).strip() if m_keywords else "",
            }
        )
        if len(threads) >= 30:
            break
    return threads


# ---------- cluster ----------

def cluster_candidates(
    signals: Signals,
    *,
    limit: int = MAX_CANDIDATES_DEFAULT,
    min_signal: int = MIN_SIGNAL_DEFAULT,
) -> list[Candidate]:
    candidates: list[Candidate] = []

    candidates.extend(_candidates_from_decision_tags(signals, min_signal=min_signal))
    candidates.extend(_candidates_from_todo_tokens(signals, min_signal=min_signal))
    candidates.extend(_candidates_from_audit_actions(signals, min_signal=min_signal))
    candidates.extend(_candidates_from_codex_groups(signals, min_signal=min_signal))

    deduped: dict[str, Candidate] = {}
    for c in candidates:
        if c.id in deduped:
            continue
        if _danger_match(c.body):
            c.rejected_reason = "danger_pattern"
        deduped[c.id] = c
    ranked = [c for c in deduped.values() if c.rejected_reason is None]
    ranked.sort(key=lambda c: -len(c.evidence.get("signals", [])))
    return ranked[:limit]


def _evidence_snippets(items: Iterable[str], head: int = 3) -> list[str]:
    out: list[str] = []
    for item in items:
        text = redact_value(str(item)).strip()
        if not text:
            continue
        out.append(text[:160])
        if len(out) >= head:
            break
    return out


def _candidates_from_decision_tags(signals: Signals, *, min_signal: int) -> list[Candidate]:
    tag_counts: Counter[str] = Counter()
    tag_to_decisions: dict[str, list[str]] = {}
    for entry in signals.decisions:
        tags = entry.get("tags") or []
        text = str(entry.get("decision") or entry.get("text") or "")
        for raw in tags:
            tag = str(raw).strip().lower()
            if not tag:
                continue
            tag_counts[tag] += 1
            tag_to_decisions.setdefault(tag, []).append(text)
    out: list[Candidate] = []
    for tag, count in tag_counts.most_common():
        if count < min_signal:
            continue
        slug = _slugify(f"recall {tag} decisions")
        evidence = {
            "signals": [f"decisions:{count}"],
            "sources": _evidence_snippets(tag_to_decisions.get(tag, [])),
            "rationale": f"tag '{tag}' appears in {count} decisions",
        }
        body = _draft_body_for_decision_tag(tag, evidence["sources"])
        desc = f"이 프로젝트의 '{tag}' 관련 결정을 한 줄씩 나열."
        cid = _candidate_id(slug, body)
        out.append(Candidate(id=cid, slug=slug, description=desc, body=body, evidence=evidence))
    return out


def _candidates_from_todo_tokens(signals: Signals, *, min_signal: int) -> list[Candidate]:
    bigrams: Counter[tuple[str, str]] = Counter()
    bigram_titles: dict[tuple[str, str], list[str]] = {}
    for entry in signals.todos_all:
        title = str(entry.get("title") or "")
        if not title:
            continue
        tokens = [t for t in re.split(r"\s+", title.lower()) if t]
        for i in range(len(tokens) - 1):
            pair = (tokens[i], tokens[i + 1])
            if not _is_meaningful_bigram(pair):
                continue
            bigrams[pair] += 1
            bigram_titles.setdefault(pair, []).append(title)
    out: list[Candidate] = []
    for pair, count in bigrams.most_common():
        if count < min_signal:
            continue
        phrase = " ".join(pair)
        slug = _slugify(f"task {phrase}")
        evidence = {
            "signals": [f"todos:{count}"],
            "sources": _evidence_snippets(bigram_titles.get(pair, [])),
            "rationale": f"bigram '{phrase}' appears in {count} todos",
        }
        body = _draft_body_for_todo_pattern(phrase, evidence["sources"])
        desc = f"'{phrase}' 패턴 작업 — 관련 열린 todo 나열 + 다음 단계 제안."
        cid = _candidate_id(slug, body)
        out.append(Candidate(id=cid, slug=slug, description=desc, body=body, evidence=evidence))
    return out


def _is_meaningful_bigram(pair: tuple[str, str]) -> bool:
    a, b = pair
    if len(a) < 2 or len(b) < 2:
        return False
    if a in {"the", "a", "an", "of", "to", "in", "on", "and", "or"}:
        return False
    if not (a in ENGLISH_VERB_HINTS or b in ENGLISH_VERB_HINTS or any(h in a for h in KOREAN_VERB_HINTS) or any(h in b for h in KOREAN_VERB_HINTS)):
        return False
    return True


def _candidates_from_audit_actions(signals: Signals, *, min_signal: int) -> list[Candidate]:
    counts = Counter(signals.audit_actions)
    out: list[Candidate] = []
    for action, count in counts.most_common(8):
        if count < min_signal * 2:
            continue
        if action.startswith("memory."):
            continue
        slug = _slugify(f"automation {action}")
        evidence = {
            "signals": [f"audit:{count}"],
            "sources": [action],
            "rationale": f"action '{action}' fired {count}× recently",
        }
        body = _draft_body_for_audit_action(action, count)
        desc = f"'{action}' 자동화 후보 — 최근 {count}회 발생."
        cid = _candidate_id(slug, body)
        out.append(Candidate(id=cid, slug=slug, description=desc, body=body, evidence=evidence))
    return out


def _candidates_from_codex_groups(signals: Signals, *, min_signal: int) -> list[Candidate]:
    if not signals.global_codex_threads:
        return []
    groups: Counter[str] = Counter()
    group_outcomes: dict[str, list[str]] = {}
    for thread in signals.global_codex_threads:
        tg = str(thread.get("task_group") or "").strip()
        if not tg:
            continue
        groups[tg] += 1
        group_outcomes.setdefault(tg, []).append(str(thread.get("task_outcome") or ""))
    out: list[Candidate] = []
    for group, count in groups.most_common():
        if count < min_signal:
            continue
        slug = _slugify(f"runbook {group}")
        evidence = {
            "signals": [f"codex_threads:{count}"],
            "sources": _evidence_snippets([f"{o} ({group})" for o in group_outcomes.get(group, [])]),
            "rationale": f"codex task_group '{group}' touched {count} threads",
        }
        body = _draft_body_for_codex_group(group, evidence["sources"])
        desc = f"'{group}' 런북 후보 — 과거 {count}회 처리 이력."
        cid = _candidate_id(slug, body)
        out.append(Candidate(id=cid, slug=slug, description=desc, body=body, evidence=evidence))
    return out


# ---------- draft body composition ----------

_BODY_RULES_FOOTER = (
    "규칙:\n"
    "- 표·박스·이모지·헤더 금지. 평문만.\n"
    "- 위 형식 외 한 글자도 추가하지 않는다.\n"
    "- shell 명령은 *참조 텍스트*로만 인용 (자동 실행 금지).\n"
)


def _draft_body_for_decision_tag(tag: str, sources: list[str]) -> str:
    bullets = "\n".join(f"- {s}" for s in sources) if sources else "- (no examples)"
    body = (
        f"`.ai/bin/ai memory decision list --tag {tag} --json` 실행. "
        "결과의 `decisions` 배열을 한 줄씩 나열한다. 각 줄: `- [{decided_at:0:19}] {decision}`.\n\n"
        "결과가 비었으면 `'{tag}' 결정 없음.` 한 줄 출력 후 stop.\n\n"
        f"참고 — 이 명령은 다음 누적 결정으로 추천됨:\n{bullets}\n\n"
        + _BODY_RULES_FOOTER
    )
    return body[:MAX_BODY_BYTES]


def _draft_body_for_todo_pattern(phrase: str, sources: list[str]) -> str:
    bullets = "\n".join(f"- {s}" for s in sources) if sources else "- (no examples)"
    body = (
        f"`.ai/bin/ai memory todo list --status open --json` 실행. "
        f"`title`이 '{phrase}'를 포함하는 항목만 한 줄씩 나열. 각 줄: `- {{title}} [{{owner}}]`.\n\n"
        "결과 0건이면 `'{phrase}' 관련 열린 todo 없음.` 한 줄 출력 후 stop.\n\n"
        f"참고 — 이 명령은 다음 누적 todo로 추천됨:\n{bullets}\n\n"
        + _BODY_RULES_FOOTER
    )
    return body[:MAX_BODY_BYTES]


def _draft_body_for_audit_action(action: str, count: int) -> str:
    body = (
        f"`.ai/bin/ai obs search --action {action} --limit 10 --json` 실행. "
        "결과의 `entries` 배열을 한 줄씩 나열. 각 줄: `- [{ts:0:19}] {action}: {payload_summary}`.\n\n"
        "결과 0건이면 `'{action}' 최근 기록 없음.` 한 줄 출력 후 stop.\n\n"
        f"참고 — '{action}'은 최근 {count}회 발생한 반복 액션.\n\n"
        + _BODY_RULES_FOOTER
    )
    return body[:MAX_BODY_BYTES]


def _draft_body_for_codex_group(group: str, sources: list[str]) -> str:
    bullets = "\n".join(f"- {s}" for s in sources) if sources else "- (no examples)"
    body = (
        f"이 슬래시 명령은 '{group}' 작업의 런북 진입점이다. "
        "사용자가 명령을 호출하면 다음을 1회 출력 후 stop:\n\n"
        f"'{group}' 런북 — 과거 처리 이력 요약\n"
        f"{bullets}\n\n"
        "다음 단계 제안: 사용자에게 '진행 의도가 무엇인지' 물어본 후 추가 동작.\n\n"
        + _BODY_RULES_FOOTER
    )
    return body[:MAX_BODY_BYTES]


# ---------- catalog persistence ----------

def list_catalog(root: Path) -> list[CatalogEntry]:
    path = catalog_path(root)
    if not path.exists():
        return []
    out: list[CatalogEntry] = []
    seen: dict[str, CatalogEntry] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        entry = CatalogEntry(
            id=str(rec["id"]),
            slug=str(rec.get("slug") or ""),
            status=str(rec.get("status") or "pending"),
            draft=rec.get("draft") or {},
            evidence=rec.get("evidence") or {},
            created_at=str(rec.get("created_at") or ""),
            installed_paths=list(rec.get("installed_paths") or []),
            body_sha256=str(rec.get("body_sha256") or ""),
        )
        seen[entry.id] = entry
    out = list(seen.values())
    return out


def _persist_entry(root: Path, entry: CatalogEntry) -> None:
    append_jsonl(catalog_path(root), entry.to_record())


def upsert_pending_candidate(root: Path, candidate: Candidate) -> CatalogEntry:
    body_sha = _sha256(candidate.body)
    existing = {e.id: e for e in list_catalog(root)}
    if candidate.id in existing:
        return existing[candidate.id]
    entry = CatalogEntry(
        id=candidate.id,
        slug=candidate.slug,
        status="pending",
        draft={
            "description": candidate.description,
            "body": candidate.body,
        },
        evidence=candidate.evidence,
        created_at=now_iso(),
        installed_paths=[],
        body_sha256=body_sha,
    )
    _persist_entry(root, entry)
    append_audit(
        root,
        action="skill.recommend_pending",
        category="memory",
        payload={"id": entry.id, "slug": entry.slug},
    )
    return entry


def recommend(
    root: Path,
    *,
    limit: int = MAX_CANDIDATES_DEFAULT,
    include_global: bool = True,
    min_signal: int = MIN_SIGNAL_DEFAULT,
    home: Path | None = None,
) -> dict[str, Any]:
    signals = gather_signals(root, include_global=include_global, home=home)
    cands = cluster_candidates(signals, limit=limit, min_signal=min_signal)
    if not cands:
        return {"ok": True, "candidates": [], "note": "signals_below_threshold"}
    existing = {e.id: e for e in list_catalog(root)}
    slug_status: dict[str, str] = {}
    for e in existing.values():
        slug_status[e.slug] = e.status
    out: list[dict[str, Any]] = []
    for c in cands:
        if c.id in existing and existing[c.id].status not in {"pending"}:
            continue
        prior_slug_status = slug_status.get(c.slug)
        if prior_slug_status in {"rejected", "installed", "uninstalled"}:
            continue
        if c.id not in existing:
            upsert_pending_candidate(root, c)
        out.append(
            {
                "id": c.id,
                "slug": c.slug,
                "status": "pending",
                "description": c.description,
                "body": c.body,
                "evidence": c.evidence,
            }
        )
    return {"ok": True, "candidates": out}


# ---------- accept / reject / uninstall ----------

def _frontmatter(description: str, catalog_id: str, body_sha256: str) -> str:
    return (
        "---\n"
        f"description: {description[:160]}\n"
        "managed-by: code-brain\n"
        f"catalog-id: {catalog_id}\n"
        f"body-sha256: {body_sha256}\n"
        "---\n"
    )


def _write_skill_file(path: Path, frontmatter: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body, encoding="utf-8")


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
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    out["__body__"] = text[end + 5 :]
    return out


def _disk_body_sha(path: Path) -> str:
    info = _read_marker(path)
    if not info:
        return ""
    return _sha256(info.get("__body__", ""))


def _entry_by_id(root: Path, candidate_id: str) -> CatalogEntry | None:
    for e in list_catalog(root):
        if e.id == candidate_id:
            return e
    return None


def _entry_by_slug(root: Path, slug: str) -> CatalogEntry | None:
    last: CatalogEntry | None = None
    for e in list_catalog(root):
        if e.slug == slug:
            last = e
    return last


def accept(root: Path, candidate_id: str) -> dict[str, Any]:
    entry = _entry_by_id(root, candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status not in {"pending", "rejected"}:
        return {"ok": False, "reason": f"status_{entry.status}"}
    body = str(entry.draft.get("body") or "")
    desc = str(entry.draft.get("description") or entry.slug)
    if _danger_match(body):
        rejected = CatalogEntry(
            id=entry.id, slug=entry.slug, status="rejected",
            draft=entry.draft, evidence=entry.evidence,
            created_at=entry.created_at, installed_paths=[],
            body_sha256=entry.body_sha256,
        )
        _persist_entry(root, rejected)
        append_audit(root, action="skill.danger_rejected", category="memory", payload={"id": entry.id})
        return {"ok": False, "reason": "danger_pattern"}
    body = redact_value(body)
    if not body.startswith("\n"):
        body = "\n" + body
    body_sha = _sha256(body)
    fm = _frontmatter(desc, entry.id, body_sha)

    targets = [
        root / ".claude" / "commands" / f"{entry.slug}.md",
        root / ".codex" / "prompts" / f"{entry.slug}.md",
    ]
    for tgt in targets:
        if tgt.exists():
            existing_marker = _read_marker(tgt)
            if existing_marker.get("managed-by") != "code-brain":
                return {
                    "ok": False,
                    "reason": "user_owned_target",
                    "path": str(tgt.relative_to(root)),
                }
    installed: list[str] = []
    for tgt in targets:
        _write_skill_file(tgt, fm, body)
        installed.append(tgt.relative_to(root).as_posix())

    accepted = CatalogEntry(
        id=entry.id, slug=entry.slug, status="installed",
        draft={"description": desc, "body": body},
        evidence=entry.evidence,
        created_at=entry.created_at, installed_paths=installed,
        body_sha256=body_sha,
    )
    _persist_entry(root, accepted)
    append_audit(root, action="skill.accept_install", category="memory", payload={"id": entry.id, "slug": entry.slug})
    return {
        "ok": True,
        "id": entry.id,
        "slug": entry.slug,
        "installed_paths": installed,
        "body_sha256": body_sha,
    }


def reject(root: Path, candidate_id: str) -> dict[str, Any]:
    entry = _entry_by_id(root, candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status == "installed":
        return {"ok": False, "reason": "already_installed"}
    rejected = CatalogEntry(
        id=entry.id, slug=entry.slug, status="rejected",
        draft=entry.draft, evidence=entry.evidence,
        created_at=entry.created_at, installed_paths=[],
        body_sha256=entry.body_sha256,
    )
    _persist_entry(root, rejected)
    append_audit(root, action="skill.reject", category="memory", payload={"id": entry.id})
    return {"ok": True, "id": entry.id}


def uninstall(root: Path, slug: str, *, force: bool = False) -> dict[str, Any]:
    entry = _entry_by_slug(root, slug)
    if entry is None or entry.status != "installed":
        return {"ok": False, "reason": "not_installed"}
    drift_paths: list[str] = []
    for rel in entry.installed_paths:
        path = root / rel
        if not path.exists():
            continue
        marker = _read_marker(path)
        disk_sha = _sha256(marker.get("__body__", ""))
        recorded = entry.body_sha256
        if recorded and disk_sha != recorded:
            drift_paths.append(rel)
    if drift_paths and not force:
        return {"ok": False, "reason": "drift_detected", "paths": drift_paths}
    removed: list[str] = []
    for rel in entry.installed_paths:
        path = root / rel
        if path.exists():
            path.unlink()
            removed.append(rel)
    uninstalled = CatalogEntry(
        id=entry.id, slug=entry.slug, status="uninstalled",
        draft=entry.draft, evidence=entry.evidence,
        created_at=entry.created_at, installed_paths=[],
        body_sha256=entry.body_sha256,
    )
    _persist_entry(root, uninstalled)
    append_audit(
        root, action="skill.uninstall", category="memory",
        payload={"id": entry.id, "slug": entry.slug, "force": force, "drift": bool(drift_paths)},
    )
    return {"ok": True, "id": entry.id, "slug": entry.slug, "removed": removed, "drift_overridden": bool(drift_paths)}


def list_visible(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in list_catalog(root):
        out.append({
            "id": e.id,
            "slug": e.slug,
            "status": e.status,
            "description": str(e.draft.get("description") or "")[:160],
            "installed_paths": e.installed_paths,
            "created_at": e.created_at,
        })
    return out
