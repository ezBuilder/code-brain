"""Precall rule recommendation engine.

Mines accumulated PreToolUse Bash invocations (from .ai/memory/audit/<year>.jsonl
and optionally Claude/Codex session transcripts), clusters repeating patterns,
and proposes user-defined precall rules. Rules go through pending → dry_run →
active. Active rules are consulted by `precall.evaluate(extra_rules=...)`.

Heuristic-only — no LLM calls, no network. Stdlib `re`/`shlex`/`Counter` only.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .memory import (
    append_audit,
    append_jsonl,
    audit_path,
    now_iso,
    read_jsonl_all,
)

CATALOG_PATH_PARTS = (".ai", "precall_rules", "catalog.jsonl")
DEFAULT_LIMIT = 5
DEFAULT_MIN_SIGNAL = 5
DEFAULT_REQUIRED_OBSERVATIONS = 5
DEFAULT_AUTO_DISABLE_THRESHOLD = 3

# Whitelist commands that should never be matched by any user rule. If a candidate
# pattern accidentally matches one of these, accept() rejects it (sanity probe).
SAFE_PROBE_COMMANDS = (
    "echo ok",
    "ls",
    "pwd",
    "git status",
    "true",
    "cat README.md",
)

# Tokens that mark a hatched pipeline; we skip these when extracting candidate
# bigrams (already-capped output is not a long_output_custom candidate).
HATCH_HINTS = ("| head", "| tail", "| wc", "| less", "| more", ">/dev/null", "2>/dev/null")


@dataclass
class RuleCandidate:
    id: str
    kind: str
    pattern: str
    canonical_pattern: str
    suggestion: str
    sample_command: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class CatalogEntry:
    id: str
    kind: str
    pattern: str
    canonical_pattern: str
    suggestion: str
    status: str
    dry_run_observations: int
    required_observations: int
    user_overrides: int
    auto_disable_threshold: int
    created_at: str
    sample_command: str
    last_blocked_command: str
    last_blocked_at: str

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "pattern": self.pattern,
            "canonical_pattern": self.canonical_pattern,
            "suggestion": self.suggestion,
            "status": self.status,
            "dry_run_observations": self.dry_run_observations,
            "required_observations": self.required_observations,
            "user_overrides": self.user_overrides,
            "auto_disable_threshold": self.auto_disable_threshold,
            "created_at": self.created_at,
            "sample_command": self.sample_command,
            "last_blocked_command": self.last_blocked_command,
            "last_blocked_at": self.last_blocked_at,
        }


# ---------- helpers ----------

def catalog_path(root: Path) -> Path:
    return root.joinpath(*CATALOG_PATH_PARTS)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_pattern(pattern: str) -> str:
    """Normalize a regex into a canonical form for dedup keying.

    Maps `\\s+`/`\\s*` → ` `, removes `\\b`/`^`/`$`, lowercases, collapses
    repeated whitespace. Two patterns that match the same set of literal
    command prefixes share a canonical form so the catalog dedups them.
    """
    s = pattern
    s = re.sub(r"\\s[+*]", " ", s)
    s = s.replace("\\b", "").replace("^", "").replace("$", "")
    s = s.replace("\\\\", " ").replace("\\", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _candidate_id(kind: str, canonical_pattern: str) -> str:
    return "pc-" + _sha256(kind + "\x00" + canonical_pattern)[:8]


def _has_hatch_hint(command: str) -> bool:
    return any(token in command for token in HATCH_HINTS)


# ---------- gather ----------

def gather_bash_invocations(root: Path, *, include_transcripts: bool = False) -> list[str]:
    """Return PreToolUse Bash command strings from .ai/memory/events/events.jsonl.

    `include_transcripts` is False by default to keep the call cheap; transcripts
    parsing is opt-in via `--include-transcripts`.
    """
    invocations: list[str] = []
    events_path = root / ".ai" / "memory" / "events" / "events.jsonl"
    event_records = read_jsonl_all(events_path)
    for rec in event_records:
        if rec.get("kind") != "PreToolUse":
            continue
        payload = rec.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("tool_name") != "Bash":
            continue
        ti = payload.get("tool_input") or {}
        if not isinstance(ti, dict):
            continue
        cmd = ti.get("command")
        if isinstance(cmd, str) and cmd.strip():
            invocations.append(cmd.strip())
    if include_transcripts:
        invocations.extend(_extract_claude_transcript_bash(root))
        invocations.extend(_extract_codex_transcript_bash(root))
    return invocations


def _extract_claude_transcript_bash(root: Path) -> list[str]:
    """Extract Bash tool_use commands from this project's Claude session JSONLs."""
    from .portable import hyphen_encode_path
    home = Path("~/.claude").expanduser()
    proj_dir = home / "projects" / hyphen_encode_path(str(root))
    if not proj_dir.is_dir():
        return []
    out: list[str] = []
    for sess in sorted(proj_dir.glob("*.jsonl"))[:30]:
        try:
            text = sess.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"Bash"' not in line:
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
                if item.get("type") == "tool_use" and item.get("name") == "Bash":
                    inp = item.get("input") or {}
                    if isinstance(inp, dict):
                        cmd = inp.get("command")
                        if isinstance(cmd, str) and cmd.strip():
                            out.append(cmd.strip())
    return out


def _extract_codex_transcript_bash(root: Path) -> list[str]:
    """Extract Bash invocations from this project's Codex rollout JSONLs.

    Codex rollouts live under ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. We filter
    by `cwd` field equality to avoid leaking other projects' commands.
    """
    home = Path("~/.codex").expanduser()
    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        return []
    target = str(root.resolve())
    out: list[str] = []
    for path in sorted(sessions_root.rglob("rollout-*.jsonl"))[-50:]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cwd_match = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if not cwd_match:
                if '"cwd"' in line:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd_val = ""
                    if isinstance(rec, dict):
                        cwd_val = str(rec.get("cwd") or (rec.get("payload") or {}).get("cwd") or "")
                    if cwd_val and cwd_val.startswith(target):
                        cwd_match = True
                continue
            if '"shell"' not in line and '"command"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if not isinstance(payload, dict):
                continue
            cmd_field = payload.get("command")
            if isinstance(cmd_field, list) and cmd_field:
                cmd_str = " ".join(str(t) for t in cmd_field)
            elif isinstance(cmd_field, str):
                cmd_str = cmd_field
            else:
                continue
            if cmd_str.strip():
                out.append(cmd_str.strip())
    return out


# ---------- cluster ----------

def cluster_bash_patterns(
    invocations: Iterable[str],
    *,
    min_signal: int = DEFAULT_MIN_SIGNAL,
) -> list[RuleCandidate]:
    """Return RuleCandidate list. Detection rules:

    - **long_output_custom**: command starts with a non-builtin verb (`flutter`,
      `cargo`, `pytest`, `npm`, `pnpm`, `yarn`, `bundle`, `mvn`, `gradle`, `make`, `dotnet`)
      and is not hatched. Bigram `(verb, subcmd)` frequency ≥ min_signal → candidate.
    - **compound_pipeline**: pipe-containing command with no hatch token. Frequency
      of the leading binary ≥ min_signal → candidate (sandbox routing suggestion).
    """
    bigram_counts: Counter[tuple[str, str]] = Counter()
    bigram_samples: dict[tuple[str, str], str] = {}

    pipeline_counts: Counter[str] = Counter()
    pipeline_samples: dict[str, str] = {}

    custom_verbs = {
        "flutter", "cargo", "pytest", "npm", "pnpm", "yarn", "bundle",
        "mvn", "gradle", "make", "dotnet", "go", "rake", "tox", "poetry",
    }

    for cmd in invocations:
        cmd_str = cmd.strip()
        if not cmd_str or _has_hatch_hint(cmd_str):
            continue
        try:
            tokens = shlex.split(cmd_str)
        except ValueError:
            continue
        if not tokens:
            continue
        head = tokens[0].rsplit("/", 1)[-1].lower()
        if head in custom_verbs and len(tokens) >= 2:
            sub = ""
            for tok in tokens[1:]:
                if not tok.startswith("-"):
                    sub = tok.lower()
                    break
            if not sub:
                # all flags, no subcommand — fall back to head-only signal
                pair = (head, "")
            else:
                pair = (head, sub)
            bigram_counts[pair] += 1
            bigram_samples.setdefault(pair, cmd_str)
        if "|" in cmd_str and not _has_hatch_hint(cmd_str):
            pipeline_counts[head] += 1
            pipeline_samples.setdefault(head, cmd_str)

    out: list[RuleCandidate] = []
    for (verb, sub), count in bigram_counts.most_common():
        if count < min_signal:
            continue
        if sub:
            # head anchored, sub matched anywhere later (handles flag-prefixed forms
            # like `pytest -v tests/`, `cargo build --release`, `flutter test --tags x`).
            pattern = rf"^{re.escape(verb)}\b(?=.*\b{re.escape(sub)}\b)"
            label = f"{verb} {sub}"
        else:
            pattern = rf"^{re.escape(verb)}\b"
            label = verb
        canonical = canonicalize_pattern(pattern)
        cid = _candidate_id("long_output_custom", canonical)
        sample = bigram_samples.get((verb, sub), label)
        out.append(
            RuleCandidate(
                id=cid,
                kind="long_output_custom",
                pattern=pattern,
                canonical_pattern=canonical,
                suggestion=f"ai exec run -- {label} ...",
                sample_command=sample,
                evidence={
                    "signals": [f"bash_invocations:{count}"],
                    "rationale": f"`{label}` 패턴 {count}회 누적 (long-output 후보)",
                },
            )
        )
    for verb, count in pipeline_counts.most_common():
        if count < min_signal:
            continue
        pattern = rf"^{re.escape(verb)}\b.*\|"
        canonical = canonicalize_pattern(pattern)
        cid = _candidate_id("compound_pipeline", canonical)
        sample = pipeline_samples.get(verb, f"{verb} ... | ...")
        # Avoid duplicate (verb, sub) collisions: if pipeline canonical clashes
        # with an existing long_output_custom canonical, the catalog's slug-style
        # dedup later filters it. Here we still emit.
        out.append(
            RuleCandidate(
                id=cid,
                kind="compound_pipeline",
                pattern=pattern,
                canonical_pattern=canonical,
                suggestion=f"ai exec run -- {sample}",
                sample_command=sample,
                evidence={
                    "signals": [f"bash_invocations:{count}"],
                    "rationale": f"`{verb} ... | ...` 다단 파이프 {count}회 (sandbox 빨림 권장)",
                },
            )
        )
    return out


# ---------- safety ----------

CATCH_ALL_PATTERNS = (
    re.compile(r"^\^\.\*"),
    re.compile(r"^\^\.\+"),
    re.compile(r"^\^\\?\.[*+?]"),
)


def is_safe_pattern(pattern: str) -> tuple[bool, str]:
    """Return (ok, reason). Refuse catch-all patterns and patterns that match a
    safe whitelist command. Caller must additionally `re.compile` the pattern."""
    if not pattern.startswith("^"):
        return False, "pattern_must_be_anchored"
    for catch in CATCH_ALL_PATTERNS:
        if catch.search(pattern):
            return False, "catch_all_rejected"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return False, f"regex_error:{exc}"
    for probe in SAFE_PROBE_COMMANDS:
        if compiled.search(probe):
            return False, f"matches_safe_probe:{probe}"
    return True, "ok"


# ---------- catalog ----------

def list_catalog(root: Path) -> list[CatalogEntry]:
    path = catalog_path(root)
    if not path.exists():
        return []
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
            kind=str(rec.get("kind") or ""),
            pattern=str(rec.get("pattern") or ""),
            canonical_pattern=str(rec.get("canonical_pattern") or ""),
            suggestion=str(rec.get("suggestion") or ""),
            status=str(rec.get("status") or "pending"),
            dry_run_observations=int(rec.get("dry_run_observations") or 0),
            required_observations=int(rec.get("required_observations") or DEFAULT_REQUIRED_OBSERVATIONS),
            user_overrides=int(rec.get("user_overrides") or 0),
            auto_disable_threshold=int(rec.get("auto_disable_threshold") or DEFAULT_AUTO_DISABLE_THRESHOLD),
            created_at=str(rec.get("created_at") or ""),
            sample_command=str(rec.get("sample_command") or ""),
            last_blocked_command=str(rec.get("last_blocked_command") or ""),
            last_blocked_at=str(rec.get("last_blocked_at") or ""),
        )
        seen[entry.id] = entry
    return list(seen.values())


def _persist(root: Path, entry: CatalogEntry) -> None:
    append_jsonl(catalog_path(root), entry.to_record())


def _entry_by_id(root: Path, candidate_id: str) -> CatalogEntry | None:
    for e in list_catalog(root):
        if e.id == candidate_id:
            return e
    return None


def load_active_rules(root: Path) -> list[dict[str, Any]]:
    """Return active+dry_run rules with `_compiled` regex attached, ready for
    `precall.evaluate(extra_rules=...)`."""
    out: list[dict[str, Any]] = []
    for e in list_catalog(root):
        if e.status not in ("active", "dry_run"):
            continue
        try:
            compiled = re.compile(e.pattern)
        except re.error:
            continue
        rec = e.to_record()
        rec["_compiled"] = compiled
        out.append(rec)
    return out


# ---------- recommend / accept / activate / reject / disable ----------

def recommend(
    root: Path,
    *,
    limit: int = DEFAULT_LIMIT,
    min_signal: int = DEFAULT_MIN_SIGNAL,
    include_transcripts: bool = False,
) -> dict[str, Any]:
    invocations = gather_bash_invocations(root, include_transcripts=include_transcripts)
    cands = cluster_bash_patterns(invocations, min_signal=min_signal)
    if not cands:
        return {"ok": True, "candidates": [], "note": "signals_below_threshold"}

    existing = list_catalog(root)
    seen_ids = {e.id for e in existing}
    seen_canonicals = {e.canonical_pattern: e.status for e in existing}

    out: list[dict[str, Any]] = []
    for c in cands:
        if c.id in seen_ids:
            entry_status = next((e.status for e in existing if e.id == c.id), "pending")
            if entry_status != "pending":
                continue
        prior_status = seen_canonicals.get(c.canonical_pattern)
        if prior_status in ("rejected", "active", "dry_run", "disabled"):
            continue
        if c.id not in seen_ids:
            entry = CatalogEntry(
                id=c.id, kind=c.kind, pattern=c.pattern,
                canonical_pattern=c.canonical_pattern, suggestion=c.suggestion,
                status="pending", dry_run_observations=0,
                required_observations=DEFAULT_REQUIRED_OBSERVATIONS,
                user_overrides=0,
                auto_disable_threshold=DEFAULT_AUTO_DISABLE_THRESHOLD,
                created_at=now_iso(), sample_command=c.sample_command,
                last_blocked_command="", last_blocked_at="",
            )
            _persist(root, entry)
            append_audit(
                root, action="precall.recommend_pending", category="memory",
                payload={"id": c.id, "kind": c.kind},
            )
        out.append(
            {
                "id": c.id,
                "kind": c.kind,
                "pattern": c.pattern,
                "suggestion": c.suggestion,
                "sample_command": c.sample_command,
                "evidence": c.evidence,
                "status": "pending",
            }
        )
        if len(out) >= limit:
            break
    return {"ok": True, "candidates": out}


def accept(root: Path, candidate_id: str) -> dict[str, Any]:
    entry = _entry_by_id(root, candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status not in ("pending",):
        return {"ok": False, "reason": f"status_{entry.status}"}
    ok, why = is_safe_pattern(entry.pattern)
    if not ok:
        rejected = CatalogEntry(
            id=entry.id, kind=entry.kind, pattern=entry.pattern,
            canonical_pattern=entry.canonical_pattern, suggestion=entry.suggestion,
            status="rejected", dry_run_observations=entry.dry_run_observations,
            required_observations=entry.required_observations,
            user_overrides=entry.user_overrides,
            auto_disable_threshold=entry.auto_disable_threshold,
            created_at=entry.created_at, sample_command=entry.sample_command,
            last_blocked_command="", last_blocked_at="",
        )
        _persist(root, rejected)
        append_audit(root, action="precall.unsafe_rejected", category="memory",
                     payload={"id": entry.id, "reason": why})
        return {"ok": False, "reason": why}
    promoted = CatalogEntry(
        id=entry.id, kind=entry.kind, pattern=entry.pattern,
        canonical_pattern=entry.canonical_pattern, suggestion=entry.suggestion,
        status="dry_run", dry_run_observations=0,
        required_observations=entry.required_observations,
        user_overrides=0, auto_disable_threshold=entry.auto_disable_threshold,
        created_at=entry.created_at, sample_command=entry.sample_command,
        last_blocked_command="", last_blocked_at="",
    )
    _persist(root, promoted)
    append_audit(root, action="precall.accept", category="memory",
                 payload={"id": entry.id, "kind": entry.kind})
    return {"ok": True, "id": entry.id, "status": "dry_run"}


def activate(root: Path, candidate_id: str, *, force: bool = False) -> dict[str, Any]:
    entry = _entry_by_id(root, candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status != "dry_run":
        return {"ok": False, "reason": f"status_{entry.status}"}
    if entry.dry_run_observations < entry.required_observations and not force:
        return {
            "ok": False,
            "reason": "insufficient_observations",
            "observed": entry.dry_run_observations,
            "required": entry.required_observations,
        }
    activated = CatalogEntry(
        id=entry.id, kind=entry.kind, pattern=entry.pattern,
        canonical_pattern=entry.canonical_pattern, suggestion=entry.suggestion,
        status="active",
        dry_run_observations=entry.dry_run_observations,
        required_observations=entry.required_observations,
        user_overrides=0,
        auto_disable_threshold=entry.auto_disable_threshold,
        created_at=entry.created_at, sample_command=entry.sample_command,
        last_blocked_command=entry.last_blocked_command,
        last_blocked_at=entry.last_blocked_at,
    )
    _persist(root, activated)
    append_audit(
        root,
        action="precall.activate_forced" if force else "precall.activate",
        category="memory",
        payload={"id": entry.id, "kind": entry.kind, "forced": force},
    )
    return {"ok": True, "id": entry.id, "status": "active", "forced": force}


def reject(root: Path, candidate_id: str) -> dict[str, Any]:
    entry = _entry_by_id(root, candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status == "active":
        return {"ok": False, "reason": "active_use_disable"}
    rejected = CatalogEntry(
        id=entry.id, kind=entry.kind, pattern=entry.pattern,
        canonical_pattern=entry.canonical_pattern, suggestion=entry.suggestion,
        status="rejected",
        dry_run_observations=entry.dry_run_observations,
        required_observations=entry.required_observations,
        user_overrides=entry.user_overrides,
        auto_disable_threshold=entry.auto_disable_threshold,
        created_at=entry.created_at, sample_command=entry.sample_command,
        last_blocked_command="", last_blocked_at="",
    )
    _persist(root, rejected)
    append_audit(root, action="precall.reject", category="memory", payload={"id": entry.id})
    return {"ok": True, "id": entry.id}


def disable(root: Path, candidate_id: str, *, reason: str = "manual") -> dict[str, Any]:
    entry = _entry_by_id(root, candidate_id)
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    if entry.status not in ("active", "dry_run"):
        return {"ok": False, "reason": f"status_{entry.status}"}
    disabled = CatalogEntry(
        id=entry.id, kind=entry.kind, pattern=entry.pattern,
        canonical_pattern=entry.canonical_pattern, suggestion=entry.suggestion,
        status="disabled",
        dry_run_observations=entry.dry_run_observations,
        required_observations=entry.required_observations,
        user_overrides=entry.user_overrides,
        auto_disable_threshold=entry.auto_disable_threshold,
        created_at=entry.created_at, sample_command=entry.sample_command,
        last_blocked_command=entry.last_blocked_command,
        last_blocked_at=entry.last_blocked_at,
    )
    _persist(root, disabled)
    append_audit(root, action="precall.disable", category="memory",
                 payload={"id": entry.id, "reason": reason})
    return {"ok": True, "id": entry.id, "status": "disabled", "reason": reason}


# ---------- runtime counters (called by hooks.py) ----------

def record_dry_run_observation(root: Path, rule_id: str) -> None:
    entry = _entry_by_id(root, rule_id)
    if entry is None or entry.status != "dry_run":
        return
    entry.dry_run_observations += 1
    _persist(root, entry)
    append_audit(
        root, action="precall.dry_run_match", category="memory",
        payload={"id": rule_id, "observed": entry.dry_run_observations},
    )


def record_user_override(root: Path, rule_id: str, command: str) -> dict[str, Any]:
    entry = _entry_by_id(root, rule_id)
    if entry is None or entry.status != "active":
        return {"ok": False, "reason": "not_active"}
    entry.user_overrides += 1
    entry.last_blocked_command = str(command)[:200]
    entry.last_blocked_at = now_iso()
    if entry.user_overrides >= entry.auto_disable_threshold:
        entry.status = "disabled"
        _persist(root, entry)
        append_audit(
            root, action="precall.auto_disabled", category="memory",
            payload={"id": rule_id, "user_overrides": entry.user_overrides},
        )
        return {"ok": True, "auto_disabled": True}
    _persist(root, entry)
    append_audit(
        root, action="precall.user_override", category="memory",
        payload={"id": rule_id, "count": entry.user_overrides},
    )
    return {"ok": True, "auto_disabled": False, "count": entry.user_overrides}


def list_visible(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "id": e.id,
            "kind": e.kind,
            "pattern": e.pattern,
            "status": e.status,
            "observed": e.dry_run_observations,
            "required": e.required_observations,
            "user_overrides": e.user_overrides,
            "sample_command": e.sample_command,
        }
        for e in list_catalog(root)
    ]
