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
import os
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
    jsonl_lock_path,
    now_iso,
    read_jsonl_all,
    read_jsonl_open_todos,
    read_jsonl_tail,
    read_text_tail,
    session_current_path,
    todos_path,
)
from .private_write import atomic_write_private_text, private_file_lock, read_root_confined_text
from .portable import hyphen_encode_path
from .redact import redact_value

CATALOG_PATH_PARTS = (".ai", "skills", "catalog.jsonl")
MAX_CANDIDATES_DEFAULT = 5
MIN_SIGNAL_DEFAULT = 3
# A "<tool>-runbook" is only worth suggesting when the tool is used across several
# distinct subcommands (a real domain), not run the same way over and over. Below
# this many distinct subcommands, a runbook is a low-value stub (e.g. uv→`uv run`).
MIN_DOMAIN_DIVERSITY = 3
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
    bash_head_counts: Counter[str] = field(default_factory=Counter)
    # head -> number of distinct subcommands seen (usage diversity). Empty when
    # unknown (old cache / direct test setup) → diversity gating fails open.
    bash_head_diversity: dict[str, int] = field(default_factory=dict)
    procedural_hints: list[dict[str, Any]] = field(default_factory=list)


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

    sig.procedural_hints = _gather_procedural_hints(root)

    if include_global:
        h = home or Path.home()
        sig.global_claude_titles = _gather_claude_global(h, root)
        sig.global_codex_threads = _gather_codex_global(h, root)
        sig.bash_head_counts = _gather_bash_heads(root)
        sig.bash_head_diversity = _gather_bash_head_diversity(root)
    return sig


# Tools eligible for a "<tool>-runbook" slash-command suggestion. Restricted to
# SUBCOMMAND-oriented domains (git status|log|…, ai exec|doctor|…) where a runbook
# entry point is genuinely useful. Single-purpose, argument-oriented utilities
# (rg/fd/jq/cat/…) are intentionally excluded: their second token is an argument,
# not a subcommand, so they have no "domain" to document and a runbook adds nothing.
# Frequency alone is not enough — _candidates_from_bash_heads also gates on usage
# DIVERSITY (distinct subcommands), so e.g. uv used only as `uv run …` is dropped.
_BASH_DOMAIN_TOOLS = {
    "git", "gh", "kubectl", "docker", "docker-compose", "npm", "pnpm", "yarn",
    "cargo", "pytest", "uv", "hatch", "poetry", "pip", "make", "terraform",
    "ansible", "aws", "gcloud", "az", "helm", "bun", "deno", "ai", "codex",
}


_BASH_HEAD_CACHE_TTL_SECONDS = 300


def _bash_head_cache_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "bash_heads.json"


def _compute_bash_head_stats(root: Path) -> tuple[Counter[str], dict[str, int]]:
    """Single pass over Bash invocations → (head counts, head→distinct-subcommand count).

    The subcommand is the first non-flag token after the tool head (e.g. ``status``
    in ``git status -s``). Diversity = number of distinct subcommands, used to drop
    low-value single-pattern runbooks (uv→only ``run``) while keeping real domains.
    """
    try:
        from .precall_recommend import gather_bash_invocations
    except Exception:
        return Counter(), {}
    try:
        invs = gather_bash_invocations(root, include_transcripts=True)
    except Exception:
        return Counter(), {}
    counts: Counter[str] = Counter()
    subs: dict[str, set[str]] = {}
    for cmd in invs:
        cmd = (cmd or "").strip()
        if not cmd or cmd.startswith("|"):
            continue
        parts = cmd.split()
        i = 0
        while i < len(parts) and ("=" in parts[i] or parts[i] in {"sudo", "time", "nohup", "exec", "env"}):
            i += 1
        if i >= len(parts):
            continue
        head = parts[i].split("/")[-1]
        if head not in _BASH_DOMAIN_TOOLS:
            continue
        counts[head] += 1
        j = i + 1
        while j < len(parts) and parts[j].startswith("-"):
            j += 1
        sub = parts[j] if j < len(parts) else "(none)"
        subs.setdefault(head, set()).add(sub)
    diversity = {h: len(s) for h, s in subs.items()}
    return counts, diversity


def _compute_bash_heads(root: Path) -> Counter[str]:
    return _compute_bash_head_stats(root)[0]


def _write_bash_head_cache(
    root: Path, counts: Counter[str], diversity: dict[str, int] | None = None
) -> None:
    cache_path = _bash_head_cache_path(root)
    try:
        payload: dict[str, Any] = {"counts": dict(counts)}
        if diversity is not None:
            payload["diversity"] = dict(diversity)
        atomic_write_private_text(cache_path, json.dumps(payload), root=root)
    except OSError:
        pass


def _read_bash_head_cache(root: Path) -> tuple[dict[str, Any], float] | None:
    cache_path = _bash_head_cache_path(root)
    try:
        text, state = read_root_confined_text(
            cache_path,
            root=root,
            max_bytes=1_000_000,
            require_private=True,
        )
        payload = json.loads(text)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload, float(state.st_mtime)


def _spawn_bash_head_cache_rebuild(root: Path) -> None:
    """Fire-and-forget background rebuild of bash_heads cache."""
    import os
    import subprocess
    import sys
    import time

    try:
        from .portable import detached_popen_kwargs
        from .process_janitor import cleanup_children, register_child
        cleanup_children(root)

        lock_path = _bash_head_cache_path(root).with_suffix(".lock")
        try:
            if lock_path.exists() and time.time() - lock_path.stat().st_mtime < 600:
                return
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return
        except OSError:
            pass

        cmd = [
            sys.executable, "-c",
            "from ai_core.recommend import _compute_bash_head_stats, _write_bash_head_cache; "
            "from pathlib import Path; "
            "import os; "
            f"r=Path({str(root)!r}); lock=Path({str(lock_path)!r}); "
            "\ntry:\n    _c, _d = _compute_bash_head_stats(r); _write_bash_head_cache(r, _c, _d)\nfinally:\n    lock.unlink(missing_ok=True)",
        ]
        env = {**os.environ, "PYTHONPATH": str(root / ".ai" / "runtime" / "src")}
        with open(os.devnull, "wb") as devnull:
            proc = subprocess.Popen(
                cmd, stdout=devnull, stderr=devnull, stdin=subprocess.DEVNULL,
                env=env, **detached_popen_kwargs(),
            )
        register_child(root, pid=proc.pid, kind="bash_head_cache", command=cmd)
    except Exception:
        pass


def _gather_procedural_hints(root: Path) -> list[dict[str, Any]]:
    """Gather procedural memory hints for recommend signals.

    Returns list of {"trigger", "procedure", "tags"} dicts (limit=50).
    If procedural.jsonl does not exist, returns empty list (backward compat).
    """
    try:
        from .procedural_memory import procedural_path
    except ImportError:
        return []

    proc_path = procedural_path(root)
    if not proc_path.exists():
        return []

    try:
        records = read_jsonl_all(proc_path)
    except Exception:
        return []

    hints: list[dict[str, Any]] = []
    for rec in records[-50:]:  # Latest 50
        if not isinstance(rec, dict):
            continue
        trigger = str(rec.get("trigger") or "").strip()
        procedure = str(rec.get("procedure") or "").strip()
        tags = rec.get("tags") or []
        if trigger and procedure:
            hints.append({
                "trigger": trigger,
                "procedure": procedure[:160],  # Truncate for signal brevity
                "tags": list(tags)[:5],  # Keep only first 5 tags
            })
    return hints


def _gather_bash_heads(root: Path) -> Counter[str]:
    """Stale-while-revalidate cache: use cache if present, schedule rebuild if stale or missing."""
    import time

    cached = _read_bash_head_cache(root)
    if cached is not None:
        payload, cache_mtime = cached
        counts_dict = payload.get("counts")
        if isinstance(counts_dict, dict):
            counts = Counter({str(k): int(v) for k, v in counts_dict.items() if isinstance(v, int)})
            if time.time() - cache_mtime >= _BASH_HEAD_CACHE_TTL_SECONDS:
                _spawn_bash_head_cache_rebuild(root)
            return counts
    _spawn_bash_head_cache_rebuild(root)
    return Counter()


def _gather_bash_head_diversity(root: Path) -> dict[str, int]:
    """Read head→distinct-subcommand counts from the bash_head cache.

    Returns {} when absent (old cache before diversity was tracked, or no cache) so
    diversity gating fails open — we only suppress a runbook when we KNOW its tool is
    low-diversity, never on missing data. The cache is (re)built by _gather_bash_heads.
    """
    cached = _read_bash_head_cache(root)
    if cached is None:
        return {}
    payload, _cache_mtime = cached
    div = payload.get("diversity")
    if not isinstance(div, dict):
        return {}
    return {str(k): int(v) for k, v in div.items() if isinstance(v, int)}


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
    candidates.extend(_candidates_from_codex_keywords(signals, min_signal=min_signal))
    candidates.extend(_candidates_from_bash_heads(signals, min_signal=min_signal))
    candidates.extend(_candidates_from_procedural_hints(signals, min_signal=min_signal))

    deduped: dict[str, Candidate] = {}
    for c in candidates:
        if c.id in deduped:
            continue
        if _danger_match(c.body):
            c.rejected_reason = "danger_pattern"
        deduped[c.id] = c
    ranked = [c for c in deduped.values() if c.rejected_reason is None]
    norm = _per_signal_max(ranked)
    ranked.sort(key=lambda c: (-_normalized_strength(c, norm), -_signal_strength(c), c.slug))
    return ranked[:limit]


def _signal_strength(c: Candidate) -> int:
    sigs = c.evidence.get("signals") or []
    if not isinstance(sigs, list) or not sigs:
        return 0
    first = str(sigs[0])
    if ":" not in first:
        return 0
    try:
        return int(first.split(":", 1)[1])
    except ValueError:
        return 0


def _signal_kind(c: Candidate) -> str:
    sigs = c.evidence.get("signals") or []
    if not isinstance(sigs, list) or not sigs:
        return ""
    first = str(sigs[0])
    return first.split(":", 1)[0] if ":" in first else ""


def _per_signal_max(cands: list[Candidate]) -> dict[str, int]:
    """For each signal kind, find the max raw count across candidates — used for fair normalization."""
    out: dict[str, int] = {}
    for c in cands:
        kind = _signal_kind(c)
        if not kind:
            continue
        strength = _signal_strength(c)
        if strength > out.get(kind, 0):
            out[kind] = strength
    return out


def _normalized_strength(c: Candidate, per_kind_max: dict[str, int]) -> float:
    """0..1 score: count / max(count_in_same_kind). Treats codex_keywords:3 (of 3 max) and bash_heads:53 (of 53 max) as equally strong."""
    kind = _signal_kind(c)
    if not kind:
        return 0.0
    m = per_kind_max.get(kind, 0)
    if m <= 0:
        return 0.0
    return _signal_strength(c) / m


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
        if count < min_signal:
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
        if not tg or _is_path_like_task_group(tg):
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


_CODEX_KEYWORD_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "code",
    "fix", "add", "use", "new", "all", "via", "etc", "any", "one",
}


def _candidates_from_codex_keywords(signals: Signals, *, min_signal: int) -> list[Candidate]:
    if not signals.global_codex_threads:
        return []
    kw_counts: Counter[str] = Counter()
    kw_outcomes: dict[str, list[str]] = {}
    for thread in signals.global_codex_threads:
        raw = str(thread.get("keywords") or "")
        if not raw:
            continue
        outcome = str(thread.get("task_outcome") or "")
        for token in re.split(r"[,;\s]+", raw):
            t = token.strip().lower()
            if len(t) < 3 or t in _CODEX_KEYWORD_STOPWORDS:
                continue
            kw_counts[t] += 1
            kw_outcomes.setdefault(t, []).append(outcome)
    out: list[Candidate] = []
    for kw, count in kw_counts.most_common(12):
        if count < min_signal:
            continue
        slug = _slugify(f"recall {kw} history")
        evidence = {
            "signals": [f"codex_keywords:{count}"],
            "sources": _evidence_snippets(kw_outcomes.get(kw, [])),
            "rationale": f"keyword '{kw}' tagged {count} codex threads",
        }
        body = _draft_body_for_codex_group(kw, evidence["sources"])
        desc = f"'{kw}' 관련 과거 codex 작업 이력 한 줄 요약."
        cid = _candidate_id(slug, body)
        out.append(Candidate(id=cid, slug=slug, description=desc, body=body, evidence=evidence))
    return out


def _candidates_from_procedural_hints(signals: Signals, *, min_signal: int) -> list[Candidate]:
    """Mine procedural memory triggers for automation candidates.

    Procedural hints encode learned patterns (lesson, skill, precall rules, fix patterns).
    Triggers like "pytest_failure", "import_error" suggest automation/handling procedures.
    Group by trigger, threshold by min_signal (typically 3).
    """
    if not signals.procedural_hints:
        return []

    trigger_counts: Counter[str] = Counter()
    trigger_to_hints: dict[str, list[dict[str, Any]]] = {}

    for hint in signals.procedural_hints:
        trigger = str(hint.get("trigger", "")).strip()
        if not trigger:
            continue
        trigger_counts[trigger] += 1
        trigger_to_hints.setdefault(trigger, []).append(hint)

    out: list[Candidate] = []
    for trigger, count in trigger_counts.most_common(8):
        if count < min_signal:
            continue
        slug = _slugify(f"procedural {trigger}")
        hints_sample = trigger_to_hints.get(trigger, [])[:3]
        evidence = {
            "signals": [f"procedural:{count}"],
            "sources": _evidence_snippets([f"[{h.get('trigger')}] {h.get('procedure', '')}" for h in hints_sample]),
            "rationale": f"procedural trigger '{trigger}' learned {count}×",
        }
        body = _draft_body_for_procedural_trigger(trigger, evidence["sources"])
        desc = f"'{trigger}' 절차 자동화 — 누적 패턴 {count}회."
        cid = _candidate_id(slug, body)
        out.append(Candidate(id=cid, slug=slug, description=desc, body=body, evidence=evidence))

    return out


def _candidates_from_bash_heads(signals: Signals, *, min_signal: int) -> list[Candidate]:
    bash_threshold = max(min_signal * 4, 10)
    out: list[Candidate] = []
    for head, count in signals.bash_head_counts.most_common(8):
        if count < bash_threshold:
            continue
        # Diversity gate: a runbook is only worth it when the tool is used across
        # several distinct subcommands. Fail open when diversity is unknown (not in
        # the map) so we never suppress on missing data; suppress only known-low.
        diversity = signals.bash_head_diversity.get(head)
        if diversity is not None and diversity < MIN_DOMAIN_DIVERSITY:
            continue
        slug = _slugify(f"{head}-runbook")
        evidence = {
            "signals": [f"bash_heads:{count}"]
            + ([f"subcommands:{diversity}"] if diversity is not None else []),
            "sources": [f"{head}"],
            "rationale": (
                f"`{head}` invoked {count}× across transcripts"
                + (f" over {diversity} distinct subcommands" if diversity is not None else "")
            ),
        }
        body = _draft_body_for_bash_head(head, count)
        desc = f"'{head}' 워크플로우 런북 — 트랜스크립트에서 {count}회 반복 호출."
        cid = _candidate_id(slug, body)
        out.append(Candidate(id=cid, slug=slug, description=desc, body=body, evidence=evidence))
    return out


def _draft_body_for_procedural_trigger(trigger: str, sources: list[str]) -> str:
    bullets = "\n".join(f"- {s}" for s in sources) if sources else "- (no examples)"
    body = (
        f"`.ai/bin/ai` 명령으로 '{trigger}' 관련 절차를 조회. "
        "결과를 한 줄씩 나열. 각 줄: `- [{{ts:0:19}}] {{trigger}}: {{procedure_summary}}`.\n\n"
        "결과 0건이면 `'{trigger}' 관련 절차 없음.` 한 줄 출력 후 stop.\n\n"
        f"참고 — 이 명령은 다음 누적 절차로 추천됨:\n{bullets}\n\n"
        + _BODY_RULES_FOOTER
    )
    return body[:MAX_BODY_BYTES]


def _draft_body_for_bash_head(head: str, count: int) -> str:
    body = (
        f"이 슬래시 명령은 '{head}' 도메인 작업의 런북 진입점이다. "
        "사용자가 호출하면 다음을 1회 출력 후 stop:\n\n"
        f"'{head}' 런북 — 최근 트랜스크립트 {count}회 호출 이력\n\n"
        f"다음 단계 제안: 사용자에게 '{head}로 무엇을 하시려는지' 물어본 후 추가 동작.\n\n"
        + _BODY_RULES_FOOTER
    )
    return body[:MAX_BODY_BYTES]


def _adaptive_min_signal(signals: Signals, requested: int) -> int:
    """Cold-start downgrade: when project signal volume is low, drop threshold to 2.
    Only applies when caller is using DEFAULT (3) or lower — explicit higher requests
    (e.g. hook-level adaptive bump from user-ignored surfacings) are respected as-is.

    Invariant: called ONLY from recommend() — cluster_candidates() receives the
    already-adapted min_signal. Do not re-apply inside cluster_candidates or
    candidate-mining helpers; that would double-discount the threshold."""
    volume = (
        len(signals.decisions)
        + len(signals.todos_all)
        + sum(1 for a in signals.audit_actions if not a.startswith("memory."))
        + len(signals.global_codex_threads)
    )
    if volume < 50 and 2 < requested <= MIN_SIGNAL_DEFAULT:
        return 2
    return requested


def _adaptive_min_signal_lower(root: Path, base: int) -> int:
    """Inverse of hooks._adaptive_min_signal_from_satisfaction: when the user is happily
    accepting more than half of acted recommendations across >= threshold acts, drop
    min_signal by 1 so we surface more candidates. Floors at 1. Symmetric path to the
    noise-reduction bump in hooks.py."""
    try:
        threshold = int(os.environ.get("AI_ADAPTIVE_HEALTHY_THRESHOLD", "5"))
    except (TypeError, ValueError):
        threshold = 5
    if threshold <= 0:
        return base
    audit_dir = root / ".ai" / "memory" / "audit"
    if not audit_dir.is_dir():
        return base
    accepted = 0
    rejected = 0
    try:
        for audit_file in sorted(audit_dir.glob("*.jsonl")):
            try:
                text = audit_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                act = str(rec.get("action") or "")
                if not act.startswith(("skill.", "agent.", "precall.")):
                    continue
                tail = act.split(".", 1)[1]
                if tail.startswith("accept"):
                    accepted += 1
                elif tail == "reject":
                    rejected += 1
    except OSError:
        return base
    total_acted = accepted + rejected
    if total_acted >= threshold and accepted / total_acted > 0.5:
        return max(base - 1, 1)
    return base


def compact_skill_catalog(root: Path) -> dict[str, Any]:
    """Rewrite catalog.jsonl keeping only the latest record per id. Skips files below
    AI_CATALOG_COMPACT_THRESHOLD_BYTES (default 256KB). Atomic via .tmp + os.replace.

    Returns {ok, before_lines, after_lines, saved_bytes}. When skipped, includes
    `skipped` reason and zero deltas."""
    try:
        threshold_bytes = int(os.environ.get("AI_CATALOG_COMPACT_THRESHOLD_BYTES", str(256 * 1024)))
    except (TypeError, ValueError):
        threshold_bytes = 256 * 1024
    path = catalog_path(root)
    try:
        with private_file_lock(jsonl_lock_path(path), root=root):
            try:
                text, state = read_root_confined_text(
                    path,
                    root=root,
                    max_bytes=50_000_000,
                    require_private=False,
                )
            except FileNotFoundError:
                return {
                    "ok": True,
                    "before_lines": 0,
                    "after_lines": 0,
                    "saved_bytes": 0,
                    "skipped": "missing",
                }
            size_before = int(state.st_size)
            if size_before < threshold_bytes:
                return {
                    "ok": True,
                    "before_lines": 0,
                    "after_lines": 0,
                    "saved_bytes": 0,
                    "skipped": "below_threshold",
                }
            latest_by_id: dict[str, dict[str, Any]] = {}
            order: list[str] = []
            before_lines = 0
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                before_lines += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                rid = rec.get("id")
                if not rid:
                    continue
                rid = str(rid)
                if rid not in latest_by_id:
                    order.append(rid)
                latest_by_id[rid] = rec
            compacted = "".join(
                json.dumps(latest_by_id[rid], ensure_ascii=False, sort_keys=True) + "\n"
                for rid in order
            )
            atomic_write_private_text(path, compacted, root=root)
            size_after = len(compacted.encode("utf-8"))
    except OSError as exc:
        return {"ok": False, "reason": f"catalog_io_error:{exc}"}
    after_lines = len(order)
    saved_bytes = max(size_before - size_after, 0)
    result = {
        "ok": True,
        "before_lines": before_lines,
        "after_lines": after_lines,
        "saved_bytes": saved_bytes,
    }
    try:
        append_audit(
            root,
            action="skill.catalog_compacted",
            category="memory",
            payload={
                "before_lines": before_lines,
                "after_lines": after_lines,
                "saved_bytes": saved_bytes,
            },
        )
    except Exception:
        pass
    return result


def _is_path_like_task_group(text: str) -> bool:
    if text.startswith(("/", "~")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    if "/workspace/" in text or "\\workspace\\" in text:
        return True
    return False


# ---------- draft body composition ----------

_BODY_RULES_FOOTER = "규칙: 평문만; shell은 참조 인용만 (실행 금지).\n"


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
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=50_000_000,
            require_private=False,
        )
    except (OSError, UnicodeDecodeError):
        return []
    out: list[CatalogEntry] = []
    seen: dict[str, CatalogEntry] = {}
    for line in text.splitlines():
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
    """Persist a candidate. T42: when a same-slug PENDING entry already exists
    (different id because evidence/body changed), treat the new candidate as
    *evidence drift* on the existing pending entry — refresh body / evidence
    in place under the original id rather than spawning a duplicate row. This
    is the explicit "skill enhancement mode" the user mandated to stop the
    infinite-duplicate-skill churn (e.g. ai-runbook 58회 vs 59회 vs 60회 ...).
    """
    body_sha = _sha256(candidate.body)
    existing_list = list_catalog(root)
    existing_by_id = {e.id: e for e in existing_list}
    if candidate.id in existing_by_id:
        return existing_by_id[candidate.id]
    # T42 evidence drift: same slug, still pending, but new body fingerprint
    same_slug_pending = next(
        (
            e
            for e in existing_list
            if e.slug == candidate.slug and e.status == "pending"
        ),
        None,
    )
    if same_slug_pending is not None:
        refreshed = CatalogEntry(
            id=same_slug_pending.id,  # preserve original id
            slug=candidate.slug,
            status="pending",
            draft={
                "description": candidate.description,
                "body": candidate.body,
            },
            evidence=candidate.evidence,
            created_at=same_slug_pending.created_at,  # preserve original ts
            installed_paths=[],
            body_sha256=body_sha,
        )
        _persist_entry(root, refreshed)
        append_audit(
            root,
            action="skill.recommend_refresh",
            category="memory",
            payload={
                "id": refreshed.id,
                "slug": refreshed.slug,
                "shadowed_new_id": candidate.id,
            },
        )
        return refreshed
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


def _collect_occupied_slug_space(root: Path) -> set[str]:
    """T40: every slug-shaped identifier already serving the user, normalized
    to a canonical form so near-duplicates like 'git-runbook' vs 'gitrunbook'
    map together. Sources:
      - catalog slugs (.ai/skills/catalog.jsonl, any status)
      - installed slash commands (.claude/commands/*.md)
      - installed sub-agents (.claude/agents/*.md)
      - cb-* skill convention (plugin marketplace skills if locally vended)
    """
    out: set[str] = set()
    try:
        for entry in list_catalog(root):
            if entry.slug:
                out.add(_canonical_slug(entry.slug))
    except Exception:
        pass
    for sub in (".claude/commands", ".claude/agents"):
        d = root / sub
        if d.is_dir():
            for f in d.glob("*.md"):
                out.add(_canonical_slug(f.stem))
    return out


def _canonical_slug(slug: str) -> str:
    """Reduce a slug to a comparison-friendly form: lowercase, alnum-only."""
    return re.sub(r"[^a-z0-9]", "", slug.lower())


def _slug_overlaps_existing(new_slug: str, occupied: set[str]) -> bool:
    """True if `new_slug` is the same root concept as something in `occupied`.

    Conservative overlap: canonical equality OR shared 6+ char prefix OR one
    is a substring of the other AND both share their first 4+ chars. This
    catches 'git-runbook' vs 'git-helper' vs 'gitops' as a single family while
    leaving genuinely different commands ('git', 'kubectl') untouched.
    """
    cn = _canonical_slug(new_slug)
    if not cn:
        return False
    for ex in occupied:
        if not ex:
            continue
        if cn == ex:
            return True
        # shared prefix family (e.g. git*, kubectl*)
        common = 0
        for a, b in zip(cn, ex):
            if a != b:
                break
            common += 1
        if common >= 6:
            return True
        if common >= 4 and (cn in ex or ex in cn):
            return True
    return False


def recommend(
    root: Path,
    *,
    limit: int = MAX_CANDIDATES_DEFAULT,
    include_global: bool = True,
    min_signal: int = MIN_SIGNAL_DEFAULT,
    home: Path | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    signals = gather_signals(root, include_global=include_global, home=home)
    effective_min_signal = _adaptive_min_signal(signals, min_signal)
    cands = cluster_candidates(signals, limit=limit, min_signal=effective_min_signal)
    if not cands:
        return {"ok": True, "candidates": [], "note": "signals_below_threshold"}
    existing = {e.id: e for e in list_catalog(root)}
    slug_status: dict[str, str] = {}
    terminal_slugs: set[str] = set()
    for e in existing.values():
        slug_status[e.slug] = e.status
        if e.status in {"rejected", "installed", "uninstalled"}:
            terminal_slugs.add(e.slug)
    # T40: collect ALL slug-shaped identifiers already serving the user so
    # newly proposed candidates can be deduped against them, not just against
    # the local catalog. Sources:
    #   - catalog slugs (any status, including installed)
    #   - existing user-owned .claude/commands/*.md basenames
    #   - existing user-owned .claude/agents/*.md basenames
    occupied_slugs = _collect_occupied_slug_space(root)
    out: list[dict[str, Any]] = []
    for c in cands:
        if c.id in existing and existing[c.id].status not in {"pending"}:
            continue
        if c.slug in terminal_slugs:
            continue
        prior_slug_status = slug_status.get(c.slug)
        if prior_slug_status in {"rejected", "installed", "uninstalled"}:
            continue
        # T40 fuzzy overlap vs OTHER slugs only. T42: exclude this candidate's
        # own slug from the occupied set so a same-slug pending entry can be
        # refreshed (skill-enhancement mode) instead of being blocked as a
        # near-duplicate of itself.
        own_canonical = _canonical_slug(c.slug)
        other_occupied = {s for s in occupied_slugs if s != own_canonical}
        if _slug_overlaps_existing(c.slug, other_occupied):
            continue
        if persist:
            entry = upsert_pending_candidate(root, c)
            out_id = entry.id  # may differ from c.id when T42 refresh kicks in
        else:
            out_id = c.id
        out.append(
            {
                "id": out_id,
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
        root / ".agents" / "skills" / entry.slug / "SKILL.md",
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
