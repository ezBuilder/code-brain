from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .memory import (
    all_audit_files,
    append_event,
    audit_path,
    read_jsonl_open_todos as _read_jsonl_open_todos,
    read_jsonl_tail as _read_jsonl_tail,
    read_text_tail as _read_text_tail,
)
from .policy import is_ci
from .redact import redact_value

import os as _os

HOT_PATH_TARGET_MS = 200
INJECTION_HOOKS = {"SessionStart", "UserPromptSubmit"}
AUTO_REBUILD_HOOKS = {"Stop", "SubagentStop"}
CONTEXT_INJECTION_HOOKS = {"UserPromptSubmit", "SessionStart"}
SKILL_RECOMMENDATION_HOOKS = {"SessionStart"}
try:
    MAX_INJECTION_BYTES = max(256, min(8192, int(_os.environ.get("AI_INJECTION_MAX_BYTES", "4096"))))
except (ValueError, TypeError):
    MAX_INJECTION_BYTES = 4096
try:
    SESSION_START_MAX_INJECTION_BYTES = max(
        MAX_INJECTION_BYTES,
        min(32768, int(_os.environ.get("AI_SESSION_START_MAX_BYTES", "12288"))),
    )
except (ValueError, TypeError):
    SESSION_START_MAX_INJECTION_BYTES = max(MAX_INJECTION_BYTES, 12288)
DECISIONS_TAIL = 5
TODOS_LIMIT = 5
SESSION_TAIL_LINES = 8
PRIOR_SESSION_TAIL_LINES = 8
DELTA_NOTICE_SHORT = "cb-ctx: Δ"
DELTA_NOTICE_VERBOSE = "Code Brain context unchanged since last injection (delta-skipped)."
SKILL_RECOMMENDATION_DISABLE_VALUES = {"0", "false", "no", "off"}
_ENV_ENABLE_VALUES = {"1", "true", "yes", "on"}


def _env_enabled(name: str, default: str = "0") -> bool:
    return _os.environ.get(name, default).lower() in _ENV_ENABLE_VALUES


def _env_disabled(name: str, default: str = "1") -> bool:
    return _os.environ.get(name, default).lower() in SKILL_RECOMMENDATION_DISABLE_VALUES


def _injection_marker_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "last_injection.sha"


def _max_injection_bytes_for(hook_name: str) -> int:
    if hook_name == "SessionStart":
        return SESSION_START_MAX_INJECTION_BYTES
    return MAX_INJECTION_BYTES


def _maybe_apply_delta(root: Path, hook_name: str, full_context: str) -> tuple[str, bool, int]:
    """For UserPromptSubmit only, replace identical repeat injections with a tiny notice.

    Returns (effective_context, delta_skipped, original_bytes).
    SessionStart always sends full context (start of session is the high-value moment).
    """
    if hook_name != "UserPromptSubmit":
        return full_context, False, len(full_context.encode("utf-8"))
    import hashlib
    sha = hashlib.sha256(full_context.encode("utf-8")).hexdigest()
    marker = _injection_marker_path(root)
    prev = ""
    if marker.exists():
        try:
            prev = marker.read_text(encoding="utf-8").strip()
        except OSError:
            prev = ""
    original_bytes = len(full_context.encode("utf-8"))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(sha, encoding="utf-8")
    except OSError:
        pass
    if prev == sha and prev:
        verbose = _env_enabled("AI_DELTA_NOTICE_VERBOSE")
        return (DELTA_NOTICE_VERBOSE if verbose else DELTA_NOTICE_SHORT), True, original_bytes
    return full_context, False, original_bytes


def _spawn_background_rebuild(root: Path) -> None:
    import os
    import subprocess

    from .portable import IS_WINDOWS, detached_popen_kwargs

    ai_bin_unix = root / ".ai" / "bin" / "ai"
    ai_bin_ps = root / ".ai" / "bin" / "ai.ps1"
    if IS_WINDOWS and ai_bin_ps.exists():
        cmd = ["powershell", "-NoProfile", "-File", str(ai_bin_ps), "index", "rebuild", "--single-flight", "--json"]
    elif ai_bin_unix.exists():
        cmd = [str(ai_bin_unix), "index", "rebuild", "--single-flight", "--json"]
    else:
        return
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                cmd,
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(root),
                **detached_popen_kwargs(),
            )
    except Exception:
        pass


def _recently_surfaced_ids(root: Path, cooldown_hours: float) -> set[str]:
    """Return candidate IDs whose recommend_pending audit event landed within cooldown_hours.

    Binary fallback cooldown — kept intact for when Ebbinghaus decay is disabled
    (AI_COOLDOWN_HALF_LIFE_HOURS=0).
    """
    if cooldown_hours <= 0:
        return set()
    audit_files = all_audit_files(root)
    if not audit_files:
        return set()
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    recent: set[str] = set()
    for audit_file in audit_files:
        try:
            content = audit_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            act = str(rec.get("action") or "")
            if not act.endswith(".recommend_pending"):
                continue
            ts = str(rec.get("ts") or "")
            if not ts:
                continue
            try:
                if ts.endswith("Z"):
                    parsed = datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
                else:
                    parsed = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if parsed < cutoff:
                continue
            cid = (rec.get("payload") or {}).get("id")
            if isinstance(cid, str) and cid:
                recent.add(cid)
    return recent


def _cooldown_score(age_hours: float, half_life_hours: float) -> float:
    """Ebbinghaus exponential-decay cooldown weight in [0, 1].

    score = 0.5 ** (age / half_life)

    - age_hours <= 0       → 1.0 (just surfaced; full penalty)
    - half_life_hours <= 0 → 0.0 (Ebbinghaus disabled; no penalty)
    """
    if half_life_hours <= 0:
        return 0.0
    if age_hours <= 0:
        return 1.0
    return 0.5 ** (age_hours / half_life_hours)


def _cooldown_weights(root: Path, half_life_hours: float) -> dict[str, float]:
    """Build {candidate_id: decay_weight in [0,1]} from recommend_pending audit events.

    For each candidate id, use the most-recent recommend_pending ts to compute its
    current age in hours, then map via _cooldown_score(age, half_life).

    Disabled (returns empty dict) when half_life_hours <= 0.
    """
    if half_life_hours <= 0:
        return {}
    audit_files = all_audit_files(root)
    if not audit_files:
        return {}
    from datetime import datetime, timezone

    latest: dict[str, datetime] = {}
    for audit_file in audit_files:
        try:
            content = audit_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            act = str(rec.get("action") or "")
            if not act.endswith(".recommend_pending"):
                continue
            ts = str(rec.get("ts") or "")
            if not ts:
                continue
            try:
                if ts.endswith("Z"):
                    parsed = datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
                else:
                    parsed = datetime.fromisoformat(ts)
            except ValueError:
                continue
            cid = (rec.get("payload") or {}).get("id")
            if not isinstance(cid, str) or not cid:
                continue
            prev = latest.get(cid)
            if prev is None or parsed > prev:
                latest[cid] = parsed

    if not latest:
        return {}
    now = datetime.now(timezone.utc)
    weights: dict[str, float] = {}
    for cid, ts in latest.items():
        age_seconds = (now - ts).total_seconds()
        age_hours = age_seconds / 3600.0
        weights[cid] = _cooldown_score(age_hours, half_life_hours)
    return weights


def _adaptive_half_life(root: Path, base_half_life: float) -> float:
    """Adapt the cooldown half-life from accept/reject behaviour.

    - healthy acceptance (acted >= 5 AND accept_ratio > 0.5) → base/2 (faster re-surface)
    - passive ignore (acted == 0 AND surfaced >= 20)         → base*2 (longer silence)
    - else                                                    → base
    """
    if base_half_life <= 0:
        return base_half_life
    audit_files = all_audit_files(root)
    if not audit_files:
        return base_half_life
    accepted = 0
    rejected = 0
    surfaced = 0
    for audit_file in audit_files:
        try:
            content = audit_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
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
            if tail == "recommend_pending":
                surfaced += 1
            elif tail.startswith("accept"):
                accepted += 1
            elif tail == "reject":
                rejected += 1
    total_acted = accepted + rejected
    if total_acted >= 5 and accepted / total_acted > 0.5:
        return base_half_life / 2.0
    if total_acted == 0 and surfaced >= 20:
        return base_half_life * 2.0
    return base_half_life


def _candidate_raw_strength(cand: dict[str, Any]) -> int:
    """Extract the raw signal count from a candidate dict's evidence.signals[0].

    Mirrors recommend._signal_strength but operates on the serialized dict shape
    that hooks.py sees from invoke() callbacks. Signals look like 'decisions:5',
    'audit:12', 'bash_heads:53', etc.
    """
    evidence = cand.get("evidence")
    if not isinstance(evidence, dict):
        return 0
    sigs = evidence.get("signals")
    if not isinstance(sigs, list) or not sigs:
        return 0
    first = str(sigs[0])
    if ":" not in first:
        return 0
    try:
        return int(first.split(":", 1)[1])
    except ValueError:
        return 0


def _candidate_summary_line(cand: dict[str, Any], label_field: str, desc_field: str | tuple[str, ...]) -> str:
    cid = str(cand.get("id") or "")
    label = str(cand.get(label_field) or "")
    if isinstance(desc_field, tuple):
        node: Any = cand
        for key in desc_field:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        desc = str(node or "")[:120]
    else:
        desc = str(cand.get(desc_field) or "")[:120]
    if not cid:
        return ""
    return f"  - {cid} | {label}: {desc}" if label else f"  - {cid}: {desc}"


def _adaptive_min_signal_from_satisfaction(root: Path, base: int) -> int:
    """If user has ignored many surfaced candidates without acting, raise threshold to reduce noise.
    Returns base+1 once surfaced>=20 and acted==0 (passive ignore). Capped at base+2."""
    try:
        threshold = int(_os.environ.get("AI_ADAPTIVE_IGNORE_THRESHOLD", "20"))
    except (TypeError, ValueError):
        threshold = 20
    if threshold <= 0:
        return base
    audit_files = all_audit_files(root)
    if not audit_files:
        return base
    surfaced = 0
    acted = 0
    for audit_file in audit_files:
        try:
            content = audit_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
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
            if tail == "recommend_pending":
                surfaced += 1
            elif tail.startswith("accept") or tail == "reject":
                acted += 1
    if surfaced >= threshold * 2 and acted == 0:
        return base + 2
    if surfaced >= threshold and acted == 0:
        return base + 1
    return base


def _is_compact_mode() -> bool:
    return _env_enabled("AI_RECOMMEND_COMPACT")


def _compact_section_line(source_short: str, fresh: list[dict[str, Any]], label_field: str, accept_cmd: str) -> str:
    parts = []
    for cand in fresh[:3]:
        cid = str(cand.get("id") or "")
        label = str(cand.get(label_field) or "")
        if cid:
            parts.append(f"{cid}={label}" if label else cid)
    if not parts:
        return ""
    return f"{source_short} ({len(fresh)}): {', '.join(parts)} · {accept_cmd}"


def _recommendation_section(
    root: Path,
    hook_name: str,
    payload: dict[str, Any],
    *,
    env_toggle: str,
    env_min_signal: str,
    invoke: "callable",
    header: str,
    approval_line: str,
    label_field: str,
    desc_field: str | tuple[str, ...],
    source_short: str = "",
    accept_cmd_compact: str = "",
) -> str:
    if hook_name not in SKILL_RECOMMENDATION_HOOKS:
        return ""
    if _env_disabled(env_toggle):
        return ""
    try:
        base_min_signal = int(_os.environ.get(env_min_signal, "3"))
    except (TypeError, ValueError):
        base_min_signal = 3
    min_signal = _adaptive_min_signal_from_satisfaction(root, base_min_signal)
    # Ebbinghaus exponential-decay cooldown (default) replaces the binary 24h cliff.
    # Set AI_COOLDOWN_HALF_LIFE_HOURS=0 to disable and fall back to the legacy
    # AI_RECOMMEND_COOLDOWN_HOURS binary set.
    try:
        env_half_life = float(_os.environ.get("AI_COOLDOWN_HALF_LIFE_HOURS", "12"))
    except (TypeError, ValueError):
        env_half_life = 12.0
    recent_ids: set[str] = set()
    cooldown_weights: dict[str, float] = {}
    if env_half_life > 0:
        half_life = _adaptive_half_life(root, env_half_life)
        cooldown_weights = _cooldown_weights(root, half_life)
    else:
        try:
            cooldown_hours = float(_os.environ.get("AI_RECOMMEND_COOLDOWN_HOURS", "24"))
        except (TypeError, ValueError):
            cooldown_hours = 24.0
        recent_ids = _recently_surfaced_ids(root, cooldown_hours)
    try:
        result = invoke(root, min_signal, payload)
    except Exception:
        return ""
    candidates = result.get("candidates") if isinstance(result, dict) else []
    if not isinstance(candidates, list) or not candidates:
        return ""
    fresh: list[dict[str, Any]] = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        cid = str(cand.get("id") or "")
        if cooldown_weights:
            decay = cooldown_weights.get(cid, 0.0)
            raw_strength = _candidate_raw_strength(cand)
            effective = raw_strength * (1.0 - decay)
            if effective < min_signal:
                continue
        elif cid and cid in recent_ids:
            continue
        fresh.append(cand)
    if not fresh:
        return ""
    if _is_compact_mode() and source_short and accept_cmd_compact:
        line = _compact_section_line(source_short, fresh, label_field, accept_cmd_compact)
        return line
    lines = [header]
    for cand in fresh[:3]:
        line = _candidate_summary_line(cand, label_field, desc_field)
        if line:
            lines.append(line)
    if len(lines) <= 1:
        return ""
    lines.append(approval_line)
    return "\n".join(lines)


_RECOMMEND_CACHE_TTL_SECONDS = 300


def _cached_recommend_invoke(
    root: Path,
    *,
    cache_name: str,
    deps: list[Path],
    compute: "callable",
    min_signal: int,
    cache_key_extra: tuple = (),
) -> dict[str, Any]:
    """Shared 5-minute TTL cache for skill/agent/precall recommend() — mtime-invalidated."""
    import time

    cache_path = root / ".ai" / "cache" / f"{cache_name}.json"
    if cache_path.exists():
        try:
            cache_mt = cache_path.stat().st_mtime
            age = time.time() - cache_mt
            if age < _RECOMMEND_CACHE_TTL_SECONDS:
                if all((not p.exists()) or p.stat().st_mtime <= cache_mt for p in deps):
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    if (
                        isinstance(payload, dict)
                        and payload.get("min_signal") == min_signal
                        and tuple(payload.get("extra") or ()) == cache_key_extra
                    ):
                        return payload.get("result") or {"candidates": []}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    result = compute()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"min_signal": min_signal, "extra": list(cache_key_extra), "result": result}),
            encoding="utf-8",
        )
        import os as _os_atomic
        _os_atomic.replace(tmp, cache_path)
    except OSError:
        pass
    return result


def _skill_recommendation_context(root: Path, hook_name: str, payload: dict[str, Any]) -> str:
    def invoke(r: Path, ms: int, pl: dict[str, Any]) -> dict[str, Any]:
        persist = not (is_ci() or pl.get("dry") is True)

        def compute() -> dict[str, Any]:
            from .recommend import recommend

            return recommend(r, limit=3, include_global=True, min_signal=ms, persist=persist)

        # include_global hardcoded True in compute() above; cache_key safe with only persist.
        deps = [
            r / ".ai" / "skills" / "catalog.jsonl",
            r / ".ai" / "memory" / "decisions.jsonl",
            audit_path(r),
            r / ".ai" / "memory" / "session-current.md",
            Path("~/.codex/memories/raw_memories.md").expanduser(),
        ]
        return _cached_recommend_invoke(
            r,
            cache_name="skill_hot",
            deps=deps,
            compute=compute,
            min_signal=ms,
            cache_key_extra=(bool(persist),),
        )

    return _recommendation_section(
        root, hook_name, payload,
        env_toggle="AI_SKILL_RECOMMENDATIONS",
        env_min_signal="AI_SKILL_RECOMMEND_MIN_SIGNAL",
        invoke=invoke,
        header="Skill recommendations available. Surface these to the user; install only after explicit approval:",
        approval_line="Approval: `ai recommend skills accept <id>`; reject noise with `ai recommend skills reject <id>`.",
        label_field="slug",
        desc_field="description",
        source_short="cb-skill",
        accept_cmd_compact="`ai recommend skills accept <id>`",
    )


def _agent_recommendation_context(root: Path, hook_name: str, payload: dict[str, Any]) -> str:
    def invoke(r: Path, ms: int, _pl: dict[str, Any]) -> dict[str, Any]:
        def compute() -> dict[str, Any]:
            from .agent_recommend import recommend as agent_recommend

            return agent_recommend(r, limit=3, min_signal=ms)

        deps = [
            r / ".ai" / "agents_catalog" / "catalog.jsonl",
            r / ".ai" / "memory" / "decisions.jsonl",
            audit_path(r),
            r / ".ai" / "memory" / "session-current.md",
        ]
        return _cached_recommend_invoke(
            r,
            cache_name="agent_hot",
            deps=deps,
            compute=compute,
            min_signal=ms,
        )

    return _recommendation_section(
        root, hook_name, payload,
        env_toggle="AI_AGENT_RECOMMENDATIONS",
        env_min_signal="AI_AGENT_RECOMMEND_MIN_SIGNAL",
        invoke=invoke,
        header="Sub-agent recommendations available. Surface these to the user; install only after explicit approval:",
        approval_line="Approval: `ai agents accept <id>`; reject noise with `ai agents reject <id>`.",
        label_field="slug",
        desc_field="description",
        source_short="cb-agent",
        accept_cmd_compact="`ai agents accept <id>`",
    )


def _precall_recommendation_context(root: Path, hook_name: str, payload: dict[str, Any]) -> str:
    def invoke(r: Path, ms: int, _pl: dict[str, Any]) -> dict[str, Any]:
        def compute() -> dict[str, Any]:
            from .precall_recommend import recommend as precall_recommend

            return precall_recommend(r, limit=3, min_signal=ms)

        deps = [
            r / ".ai" / "memory" / "events" / "events.jsonl",
            r / ".ai" / "memory" / "precall_catalog" / "catalog.jsonl",
            audit_path(r),
            r / ".ai" / "memory" / "session-current.md",
        ]
        return _cached_recommend_invoke(
            r,
            cache_name="precall_hot",
            deps=deps,
            compute=compute,
            min_signal=ms,
        )

    return _recommendation_section(
        root, hook_name, payload,
        env_toggle="AI_PRECALL_RECOMMENDATIONS",
        env_min_signal="AI_PRECALL_RECOMMEND_MIN_SIGNAL",
        invoke=invoke,
        header="Precall routing rule recommendations available. Surface these to the user; activate only after explicit approval:",
        approval_line="Approval: `ai precall accept <id>` → `ai precall activate <id>`; reject noise with `ai precall reject <id>`.",
        label_field="kind",
        desc_field=("evidence", "rationale"),
        source_short="cb-precall",
        accept_cmd_compact="`ai precall accept <id>`",
    )


def _federated_summary_context(root: Path, hook_name: str) -> str:
    if hook_name not in SKILL_RECOMMENDATION_HOOKS:
        return ""
    if _env_disabled("AI_FEDERATED_SUMMARY"):
        return ""
    try:
        from .federated import cross_project_summary

        summary = cross_project_summary(root)
    except Exception:
        return ""
    if not isinstance(summary, dict) or summary.get("scanned_projects", 0) < 2:
        return ""
    parts: list[str] = []
    bigrams = summary.get("common_todo_patterns") or []
    if isinstance(bigrams, list):
        top = [b for b in bigrams if isinstance(b, dict) and b.get("projects", 0) >= 2][:3]
        if top:
            parts.append(
                "todos: "
                + ", ".join(f"{b['bigram']}({b['projects']})" for b in top)
            )
    kinds = summary.get("common_precall_kinds") or []
    if isinstance(kinds, list):
        top_kinds = [k for k in kinds if isinstance(k, dict) and k.get("projects", 0) >= 2][:2]
        if top_kinds:
            parts.append(
                "precall: "
                + ", ".join(f"{k['kind']}({k['projects']})" for k in top_kinds)
            )
    if not parts:
        return ""
    scanned = summary.get("scanned_projects", 0)
    return (
        f"Federated patterns from {scanned} projects — {' | '.join(parts)}. "
        "Inspect with `ai federated summary`."
    )


def _satisfaction_summary_context(root: Path, hook_name: str) -> str:
    if hook_name not in SKILL_RECOMMENDATION_HOOKS:
        return ""
    if _env_disabled("AI_SATISFACTION_SUMMARY"):
        return ""
    audit_files = all_audit_files(root)
    if not audit_files:
        return ""
    from datetime import datetime, timedelta, timezone
    try:
        stale_days = float(_os.environ.get("AI_SATISFACTION_STALE_DAYS", "7"))
    except (TypeError, ValueError):
        stale_days = 7.0
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    counts = {"surfaced": 0, "accepted": 0, "rejected": 0, "stale": 0}
    acted_ids: set[str] = set()
    surfaced_records: list[tuple[datetime, str]] = []
    for audit_file in audit_files:
        try:
            content = audit_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
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
            pid = (rec.get("payload") or {}).get("id")
            if tail == "recommend_pending":
                counts["surfaced"] += 1
                ts = str(rec.get("ts") or "")
                if ts and isinstance(pid, str):
                    try:
                        parsed = (
                            datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
                            if ts.endswith("Z") else datetime.fromisoformat(ts)
                        )
                        surfaced_records.append((parsed, pid))
                    except ValueError:
                        pass
            elif tail.startswith("accept"):
                counts["accepted"] += 1
                if isinstance(pid, str):
                    acted_ids.add(pid)
            elif tail == "reject":
                counts["rejected"] += 1
                if isinstance(pid, str):
                    acted_ids.add(pid)
    for ts, pid in surfaced_records:
        if pid not in acted_ids and ts < stale_cutoff:
            counts["stale"] += 1
    total_acted = counts["accepted"] + counts["rejected"]
    if counts["surfaced"] == 0 and total_acted == 0:
        return ""
    stale_suffix = f", {counts['stale']} stale (>{int(stale_days)}d)" if counts["stale"] else ""
    adaptive_bump = _adaptive_min_signal_from_satisfaction(root, 3) - 3
    adaptive_suffix = f"; adaptive +{adaptive_bump} (auto-noise reduction)" if adaptive_bump > 0 else ""
    if total_acted == 0:
        return (
            f"Recommendation satisfaction: {counts['surfaced']} surfaced, 0 acted{stale_suffix}{adaptive_suffix}. "
            "Inspect: `ai recommend skills|agents|precall`; opt out: AI_*_RECOMMENDATIONS=0."
        )
    sat_pct = int(round(100 * counts["accepted"] / total_acted))
    return (
        f"Recommendation satisfaction: {sat_pct}% accept ({counts['accepted']}/{total_acted} acted, "
        f"{counts['surfaced']} surfaced lifetime{stale_suffix})."
    )


def _compact_meta_line(root: Path) -> str:
    """Compact-mode unified one-liner combining federated + satisfaction data.

    Format: "cb-meta: {surfaced} surfaced/{acted} acted (adaptive +{N}); fed {n} proj — {pat}({c})"
    Returns "" when both sides have no data; renders only the side(s) with data.
    """
    # --- satisfaction side -------------------------------------------------
    sat_part = ""
    audit_files = all_audit_files(root)
    if audit_files:
        surfaced = 0
        acted = 0
        for audit_file in audit_files:
            try:
                content = audit_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in content.splitlines():
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
                if tail == "recommend_pending":
                    surfaced += 1
                elif tail.startswith("accept") or tail == "reject":
                    acted += 1
        if surfaced > 0 or acted > 0:
            adaptive_bump = _adaptive_min_signal_from_satisfaction(root, 3) - 3
            adaptive_suffix = f" (adaptive +{adaptive_bump})" if adaptive_bump > 0 else ""
            sat_part = f"{surfaced} surfaced/{acted} acted{adaptive_suffix}"

    # --- federated side ----------------------------------------------------
    fed_part = ""
    try:
        from .federated import cross_project_summary

        summary = cross_project_summary(root)
    except Exception:
        summary = None
    if isinstance(summary, dict) and summary.get("scanned_projects", 0) >= 2:
        scanned = summary.get("scanned_projects", 0)
        top_label = ""
        bigrams = summary.get("common_todo_patterns") or []
        if isinstance(bigrams, list):
            top = [b for b in bigrams if isinstance(b, dict) and b.get("projects", 0) >= 2]
            if top:
                b = top[0]
                top_label = f"{b.get('bigram')}({b.get('projects')})"
        if not top_label:
            kinds = summary.get("common_precall_kinds") or []
            if isinstance(kinds, list):
                top_kinds = [k for k in kinds if isinstance(k, dict) and k.get("projects", 0) >= 2]
                if top_kinds:
                    k = top_kinds[0]
                    top_label = f"{k.get('kind')}({k.get('projects')})"
        if top_label:
            fed_part = f"fed {scanned} proj — {top_label}"
        else:
            fed_part = f"fed {scanned} proj"

    if not sat_part and not fed_part:
        return ""
    if sat_part and fed_part:
        line = f"cb-meta: {sat_part}; {fed_part}"
    elif sat_part:
        line = f"cb-meta: {sat_part}"
    else:
        line = f"cb-meta: {fed_part}"
    # Trim trailing punctuation and clamp to 200 bytes.
    line = line.rstrip(".; ")
    encoded = line.encode("utf-8")
    if len(encoded) > 200:
        line = encoded[:197].decode("utf-8", errors="ignore") + "..."
    return line


def read_payload(stdin: str | None = None) -> dict[str, Any]:
    raw = stdin if stdin is not None else sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def handle_hook(root: Path, hook_name: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    effective_hook = hook_name or payload.get("hook") or payload.get("event") or "unknown"

    precall_decision: dict[str, Any] | None = None
    if effective_hook == "PreToolUse":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
        raw_input = payload.get("tool_input")
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        try:
            from .precall import evaluate as precall_evaluate

            extra_rules: list[dict[str, Any]] = []
            try:
                from .precall_recommend import load_active_rules

                extra_rules = load_active_rules(root)
            except Exception:
                extra_rules = []
            precall_decision = precall_evaluate(tool_name, tool_input, extra_rules=extra_rules)
            if precall_decision and precall_decision.get("action") == "observe":
                rid = precall_decision.get("rule_id")
                if rid:
                    try:
                        from .precall_recommend import record_dry_run_observation

                        record_dry_run_observation(root, str(rid))
                    except Exception:
                        pass
            elif (
                precall_decision
                and precall_decision.get("action") == "block"
                and precall_decision.get("rule_id")
            ):
                rid = str(precall_decision.get("rule_id"))
                try:
                    from .precall_recommend import record_user_override

                    record_user_override(root, rid, str(tool_input.get("command") or ""))
                except Exception:
                    pass
        except Exception:
            precall_decision = None

    additional_context = build_context(effective_hook, payload, root=root)
    if (
        effective_hook == "PreToolUse"
        and precall_decision
        and precall_decision.get("action") == "block"
    ):
        deny_reason = (
            f"Code Brain auto-routing: {precall_decision.get('reason')}. "
            f"Use this instead: {precall_decision.get('suggestion')}."
        )
        additional_context = f"{deny_reason}\n\n{additional_context}" if additional_context else deny_reason
    additional_context, delta_skipped, original_context_bytes = _maybe_apply_delta(
        root, effective_hook, additional_context
    )
    additional_context_bytes = len(additional_context.encode("utf-8"))
    event = {
        "hook": effective_hook,
        "additional_context_bytes": additional_context_bytes,
        "original_context_bytes": original_context_bytes,
        "delta_skipped": delta_skipped,
        **payload,
    }
    if precall_decision:
        event["precall"] = {
            "action": precall_decision.get("action"),
            "reason": precall_decision.get("reason"),
            "binary": precall_decision.get("binary"),
        }
        if precall_decision.get("action") == "block":
            event["decision"] = "block"
    if is_ci() or payload.get("dry") is True:
        mode = "ci-fast-path" if is_ci() else "local-dry-fast-path"
        persisted = False
    else:
        append_event(root, event)
        mode = "local-append"
        persisted = True
        if effective_hook in AUTO_REBUILD_HOOKS:
            _spawn_background_rebuild(root)
            try:
                from .recommend import _spawn_bash_head_cache_rebuild

                _spawn_bash_head_cache_rebuild(root)
            except Exception:
                pass
            if _env_enabled("AI_AUTO_SESSION_NOTE"):
                last_msg = payload.get("last_assistant_message")
                if isinstance(last_msg, str) and last_msg.strip():
                    first_line = last_msg.strip().splitlines()[0][:200]
                    try:
                        from .memory import append_session_note

                        append_session_note(root, text=f"[{effective_hook}] {first_line}")
                    except Exception:
                        pass
        try:
            _handle_lifecycle_event(root, effective_hook, payload)
        except Exception:
            pass
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if persisted and elapsed_ms > HOT_PATH_TARGET_MS:
        try:
            from .memory import append_audit

            append_audit(
                root,
                action="hook.slow",
                category="hook",
                payload={"hook": effective_hook, "elapsed_ms": elapsed_ms, "target_ms": HOT_PATH_TARGET_MS},
            )
        except Exception:
            pass
    response = {
        "ok": True,
        "hook": effective_hook,
        "mode": mode,
        "persisted": persisted,
        "elapsed_ms": elapsed_ms,
        "target_ms": HOT_PATH_TARGET_MS,
        "additional_context_bytes": additional_context_bytes,
    }
    if effective_hook in CONTEXT_INJECTION_HOOKS:
        response["additionalContext"] = additional_context
        response["hookSpecificOutput"] = {
            "hookEventName": effective_hook,
            "additionalContext": additional_context,
        }
    if precall_decision:
        response["precall"] = precall_decision
        if precall_decision.get("action") == "block":
            import os
            rewrite_mode = os.environ.get("AI_PRECALL_REWRITE", "").lower() in ("1", "true", "yes")
            suggestion = str(precall_decision.get("suggestion") or "")
            if rewrite_mode and suggestion.startswith(".ai/bin/ai exec run --"):
                response["hookSpecificOutput"] = {
                    "hookEventName": effective_hook,
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        f"Code Brain auto-rewrite: {precall_decision.get('reason')} → routed to sandbox."
                    ),
                    "updatedInput": {"command": suggestion},
                    "additionalContext": additional_context,
                }
                response["rewritten"] = True
            else:
                response["decision"] = "block"
                response["hookSpecificOutput"] = {
                    "hookEventName": effective_hook,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Code Brain auto-routing: {precall_decision.get('reason')}. "
                        f"Use this instead: {suggestion}."
                    ),
                    "additionalContext": additional_context,
                }
                response["reason"] = (
                    f"Code Brain auto-routing: {precall_decision.get('reason')}. "
                    f"Use this instead: {suggestion}. "
                    "Or call MCP `mcp__code-brain__sandbox_execute` directly. "
                    "Code Brain stores full output in .ai/cache/sandbox/<exec_id>.txt and returns a short summary "
                    "(first 30 + last 5 lines, total under 4 KB) to keep your context window small."
                )
    return redact_value(response)


def codex_wire_output(response: dict[str, Any]) -> dict[str, Any]:
    """Project the verbose diagnostic hook response to Codex's strict wire schema.

    `ai hook --json` intentionally returns diagnostic fields used by tests and
    observability. Actual Codex hook commands must emit only fields accepted by
    the current hook runtime; otherwise Codex marks the hook as failed and opens.
    """
    hook = str(response.get("hook") or "")
    hook_specific = response.get("hookSpecificOutput")
    hook_specific = hook_specific if isinstance(hook_specific, dict) else {}

    if response.get("decision") == "block":
        reason = str(response.get("reason") or hook_specific.get("permissionDecisionReason") or "Blocked by Code Brain hook.")
        if hook == "PreToolUse":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        if hook in {"UserPromptSubmit", "PostToolUse", "Stop"}:
            return {"decision": "block", "reason": reason}

    additional_context = hook_specific.get("additionalContext")
    if hook in {"SessionStart", "UserPromptSubmit"} and additional_context:
        return {
            "hookSpecificOutput": {
                "hookEventName": hook,
                "additionalContext": str(additional_context),
            }
        }
    if hook == "PostToolUse" and additional_context:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": str(additional_context),
            }
        }
    if hook == "Stop":
        return {"continue": True}
    return {}


LIFECYCLE_SNAPSHOT_HOOKS = {"PreCompact", "SessionEnd"}


def _handle_lifecycle_event(root: Path, hook_name: str, payload: dict[str, Any]) -> None:
    """Side-effect handler for PreCompact / SessionEnd / Notification / PermissionRequest.

    Runs after append_event so audit ordering matches the original event timestamp.
    Errors are swallowed by the caller — never break the hook hot path.
    """
    from .memory import append_audit

    if hook_name in LIFECYCLE_SNAPSHOT_HOOKS:
        session_id = str(payload.get("session_id") or payload.get("sid") or "")
        agent = str(payload.get("agent") or "unknown")
        if session_id:
            try:
                from .session_resume import write_snapshot

                if hook_name == "PreCompact":
                    trigger = str(payload.get("trigger") or "unknown")
                    write_snapshot(
                        root,
                        session_id=session_id,
                        agent=agent,
                        force=True,
                        reason=f"precompact_{trigger}",
                    )
                    append_audit(
                        root,
                        action="compact.snapshot_forced",
                        category="memory",
                        payload={"trigger": trigger, "session_id": session_id},
                    )
                else:
                    reason = str(payload.get("reason") or "unknown")
                    write_snapshot(
                        root,
                        session_id=session_id,
                        agent=agent,
                        force=True,
                        reason=f"session_end_{reason}",
                    )
                    append_audit(
                        root,
                        action="session.end",
                        category="memory",
                        payload={"reason": reason, "session_id": session_id},
                    )
            except Exception:
                pass
        return

    if hook_name == "Notification":
        ntype = str(payload.get("type") or payload.get("notification_type") or "unknown")
        append_audit(
            root,
            action="notification.received",
            category="memory",
            payload={"type": ntype[:64]},
        )
        return

    if hook_name == "PermissionRequest":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "unknown")
        raw_input = payload.get("tool_input")
        description = ""
        if isinstance(raw_input, dict):
            description = str(raw_input.get("description") or "")[:200]
        append_audit(
            root,
            action="permission.requested",
            category="approval",
            payload={"tool_name": tool_name[:64], "description": description},
        )
        return

    if hook_name == "PermissionDenied":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "unknown")
        reason = str(payload.get("reason") or "")[:200]
        append_audit(
            root,
            action="permission.denied",
            category="approval",
            payload={"tool_name": tool_name[:64], "reason": reason},
        )
        return

    if hook_name == "PostCompact":
        trigger = str(payload.get("trigger") or "unknown")
        append_audit(
            root,
            action="compact.completed",
            category="memory",
            payload={"trigger": trigger},
        )
        return

    if hook_name == "CwdChanged":
        prev = str(payload.get("previous_cwd") or "")
        new = str(payload.get("new_cwd") or "")
        cross_project = False
        if prev and new:
            try:
                prev_root = Path(prev).resolve()
                new_root = Path(new).resolve()
                cross_project = (
                    prev_root != new_root
                    and not str(new_root).startswith(str(prev_root))
                    and not str(prev_root).startswith(str(new_root))
                )
            except Exception:
                cross_project = False
        append_audit(
            root,
            action="cwd.changed",
            category="memory",
            payload={
                "previous_cwd": prev[:200],
                "new_cwd": new[:200],
                "cross_project": cross_project,
            },
        )
        return

    if hook_name == "ConfigChange":
        source = str(payload.get("source") or "")
        append_audit(
            root,
            action="config.changed",
            category="memory",
            payload={"source": source[:64]},
        )
        return

    if hook_name == "InstructionsLoaded":
        file_path = str(payload.get("file_path") or "")
        memory_type = str(payload.get("memory_type") or "")
        load_reason = str(payload.get("load_reason") or "")
        append_audit(
            root,
            action="instructions.loaded",
            category="memory",
            payload={
                "file_path": file_path[:200],
                "memory_type": memory_type[:32],
                "load_reason": load_reason[:32],
            },
        )


def build_context(hook_name: str, payload: dict[str, Any], *, root: Path | None = None) -> str:
    agent = payload.get("agent", "unknown")
    writes = "off" if is_ci() or payload.get("dry") is True else "worker-local"
    header = f"Code Brain fast_path: hook={hook_name}, agent={agent}, network=off, writes={writes}."
    if hook_name not in INJECTION_HOOKS or root is None:
        return ""
    sections = [header]
    if _env_enabled("AI_ROUTING_HINT_COMPACT"):
        routing = "Search routing: prefer MCP `code_query`/`context_pack` over grep."
    else:
        routing = (
            "Search routing: prefer MCP `code_query` / `context_pack` over Bash grep/find. "
            "Each MCP query returns ranked snippets (default 5) instead of full grep dumps — "
            "use grep only as fallback when MCP fails."
        )
    sections.append(routing)
    if hook_name == "SessionStart":
        try:
            from .session_resume import read_latest_snapshot
            current_sid = str(payload.get("session_id") or payload.get("sid") or "")
            prior = read_latest_snapshot(root, exclude_session_id=current_sid or None)
        except Exception:
            prior = None
        if prior:
            lines = [f"Prior session resume (session_id={prior.get('session_id')}, written_at={prior.get('written_at')}):"]
            for entry in (prior.get("decisions_tail") or [])[-3:]:
                text = str(entry.get("decision") or entry.get("summary") or entry.get("text") or "")[:160]
                if text:
                    lines.append(f"  decision: {text}")
            for entry in (prior.get("todos_open") or [])[-3:]:
                text = str(entry.get("title") or entry.get("text") or entry.get("summary") or "")[:160]
                if text:
                    lines.append(f"  open todo: {text}")
            actions = prior.get("audit_tail_actions") or []
            if actions:
                lines.append(f"  recent actions: {', '.join(str(a) for a in actions[-5:])}")
            prior_tail = str(prior.get("session_tail") or "")
            tail_lines = [line for line in prior_tail.splitlines() if line.strip()][-PRIOR_SESSION_TAIL_LINES:]
            if tail_lines:
                lines.append("  session tail:")
                for line in tail_lines:
                    lines.append(f"    {line[:220]}")
            sections.append("\n".join(lines))
    decisions = _read_jsonl_tail(root / ".ai" / "memory" / "decisions.jsonl", DECISIONS_TAIL)
    if decisions:
        lines = ["Recent decisions:"]
        for entry in decisions:
            ts = str(entry.get("decided_at") or entry.get("timestamp") or "")[:19]
            text = str(entry.get("decision") or entry.get("summary") or entry.get("text") or "")[:160]
            lines.append(f"  - [{ts}] {text}" if ts else f"  - {text}")
        sections.append("\n".join(lines))
    todos = _read_jsonl_open_todos(root / ".ai" / "memory" / "todos.jsonl", TODOS_LIMIT)
    if todos:
        lines = ["Open todos:"]
        for entry in todos:
            text = str(entry.get("title") or entry.get("text") or entry.get("summary") or "")[:160]
            owner = str(entry.get("owner") or "")
            lines.append(f"  - {text} [{owner}]" if owner else f"  - {text}")
        sections.append("\n".join(lines))
    skill_recommendations = _skill_recommendation_context(root, hook_name, payload)
    if skill_recommendations:
        sections.append(skill_recommendations)
    agent_recommendations = _agent_recommendation_context(root, hook_name, payload)
    if agent_recommendations:
        sections.append(agent_recommendations)
    precall_recommendations = _precall_recommendation_context(root, hook_name, payload)
    if precall_recommendations:
        sections.append(precall_recommendations)
    if _is_compact_mode():
        if hook_name in SKILL_RECOMMENDATION_HOOKS:
            meta = _compact_meta_line(root)
            if meta:
                sections.append(meta)
    else:
        federated = _federated_summary_context(root, hook_name)
        if federated:
            sections.append(federated)
        satisfaction = _satisfaction_summary_context(root, hook_name)
        if satisfaction:
            sections.append(satisfaction)
    if hook_name in SKILL_RECOMMENDATION_HOOKS and not _env_disabled("AI_MEMORY_TIER_SUMMARY"):
        try:
            from .memory_tier import classify as _classify, hot_pressure as _pressure
            cls = _classify(root)
            pres = _pressure(root)
            hot = cls["tiers"]["hot"]["audit_events"]
            warm = cls["tiers"]["warm"]["audit_events"]
            cold = cls["tiers"]["cold"]["audit_events"]
            sline = f"cb-mem: hot={hot} warm={warm} cold={cold} | session={int(pres['session_md_ratio']*100)}%"
            if pres.get("page_out_recommended"):
                sline += " ⚠page-out"
            sections.append(sline)
        except Exception:
            pass
    session_tail = _read_text_tail(root / ".ai" / "memory" / "session-current.md", SESSION_TAIL_LINES)
    if session_tail:
        sections.append("Session-current tail:\n" + session_tail)
    try:
        from .config import load_config
        from .remote_memory import cache_path

        config = load_config(root)
        remote = config.get("remote_memory", {}) if isinstance(config.get("remote_memory"), dict) else {}
        if hook_name == "SessionStart" and bool(remote.get("inject_on_session_start", False)):
            cached = _read_text_tail(cache_path(root), 12)
            if cached:
                sections.append("Remote memory cached summary (no network in hook):\n" + cached)
    except Exception:
        pass
    composed = "\n\n".join(sections)
    max_bytes = _max_injection_bytes_for(hook_name)
    if len(composed.encode("utf-8")) > max_bytes:
        truncated = composed.encode("utf-8")[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."
        composed = truncated
    return composed
