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
SESSION_START_TARGET_MS = 1500
INJECTION_HOOKS = {"SessionStart", "UserPromptSubmit", "SubagentStart"}
AUTO_REBUILD_HOOKS = {"Stop", "SubagentStop", "FileChanged"}
CONTEXT_INJECTION_HOOKS = {"UserPromptSubmit", "SessionStart", "SubagentStart"}
SKILL_RECOMMENDATION_HOOKS = {"SessionStart"}

KNOWN_AGENTS = {"claude", "codex", "antigravity"}


def normalize_agent(payload: dict[str, Any]) -> str:
    """Map a hook payload's agent identifier to one of the canonical names.

    Returns one of ``claude``, ``codex``, ``antigravity``, or ``unknown``. We
    prefer an explicit ``agent`` (or ``agent_name``) field; otherwise we fall
    back to environment variables each host sets (``CLAUDE_PROJECT_DIR`` for
    Claude Code, ``CODEX_HOME`` for OpenAI Codex CLI, ``ANTIGRAVITY_CLI`` /
    ``AGY_HOME`` for Google Antigravity). The result is used both for
    inject-context headers and for obs/audit breakdown.
    """
    raw = payload.get("agent") or payload.get("agent_name") or ""
    norm = str(raw).strip().lower()
    aliases = {
        "claude": "claude", "claude-code": "claude", "claudecode": "claude",
        "codex": "codex", "codex-cli": "codex",
        "antigravity": "antigravity", "agy": "antigravity", "antigravity-cli": "antigravity",
    }
    if norm in aliases:
        return aliases[norm]
    if norm and norm != "unknown":
        return norm
    env = _os.environ
    if env.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    if env.get("CODEX_HOME") or env.get("CODEX_TURN_ID"):
        return "codex"
    if env.get("ANTIGRAVITY_CLI") or env.get("AGY_HOME"):
        return "antigravity"
    return "unknown"


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

KNOWN_AGENTS = {"claude", "codex", "antigravity"}


def normalize_agent(payload: dict[str, Any]) -> str:
    """Map a hook payload's agent identifier to one of the canonical names.

    Returns one of ``claude``, ``codex``, ``antigravity``, or ``unknown``. We
    prefer an explicit ``agent`` (or ``agent_name``) field; otherwise we fall
    back to environment variables each host sets (``CLAUDE_PROJECT_DIR`` for
    Claude Code, ``CODEX_HOME`` for OpenAI Codex CLI, ``ANTIGRAVITY_CLI`` /
    ``GEMINI_HOME`` for Google Antigravity). The result is used both for
    inject-context headers and for obs/audit breakdown.
    """
    raw = payload.get("agent") or payload.get("agent_name") or ""
    norm = str(raw).strip().lower()
    aliases = {
        "claude": "claude", "claude-code": "claude", "claudecode": "claude",
        "codex": "codex", "codex-cli": "codex",
        "antigravity": "antigravity", "agy": "antigravity", "antigravity-cli": "antigravity",
    }
    if norm in aliases:
        return aliases[norm]
    if norm and norm != "unknown":
        return norm
    env = _os.environ
    if env.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    if env.get("CODEX_HOME") or env.get("CODEX_TURN_ID"):
        return "codex"
    if env.get("ANTIGRAVITY_CLI") or env.get("AGY_HOME"):
        return "antigravity"
    return "unknown"
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


def _target_ms_for(hook_name: str) -> int:
    if hook_name == "SessionStart":
        return SESSION_START_TARGET_MS
    return HOT_PATH_TARGET_MS


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
        if _env_enabled("AI_REBUILD_INCREMENTAL", default="1"):
            cmd.append("--incremental")
    elif ai_bin_unix.exists():
        cmd = [str(ai_bin_unix), "index", "rebuild", "--single-flight", "--json"]
        if _env_enabled("AI_REBUILD_INCREMENTAL", default="1"):
            cmd.append("--incremental")
    else:
        return
    try:
        from .process_janitor import cleanup_children, register_child
        cleanup_children(root)
        with open(os.devnull, "wb") as devnull:
            proc = subprocess.Popen(
                cmd,
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(root),
                **detached_popen_kwargs(),
            )
        register_child(root, pid=proc.pid, kind="index_rebuild", command=cmd)
    except Exception:
        pass


def _spawn_agents_md_refresh(root: Path) -> None:
    """Refresh the managed AGENTS.md memory block in a DETACHED process.

    Antigravity fires its ``Stop`` hook but does not wait for / may kill the hook
    process before a synchronous refresh (which calls build_context, ~1s) finishes
    — so the block would never update from an agy turn. Running the refresh
    detached (own session, like _spawn_background_rebuild) lets it complete even
    if the host kills the parent hook. The refresh itself is write-on-change, so
    repeated spawns don't churn AGENTS.md. Never raises into the hook hot path.
    """
    import os
    import subprocess
    import sys

    from .portable import detached_popen_kwargs

    if _env_disabled("AI_AGENTS_MD_MEMORY", default="1"):
        return
    # Cooldown: PostToolUse can fire many times per turn. Spawn at most once per
    # window so we don't launch a build_context process on every tool call.
    try:
        cooldown = float(os.environ.get("AI_AGENTS_MD_REFRESH_COOLDOWN", "15"))
    except (TypeError, ValueError):
        cooldown = 15.0
    lock = root / ".ai" / "cache" / "agents_md_refresh.lock"
    try:
        if cooldown > 0 and lock.exists() and (time.time() - lock.stat().st_mtime) < cooldown:
            return
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    src = str(root / ".ai" / "runtime" / "src")
    code = (
        "import sys;from pathlib import Path;"
        f"sys.path.insert(0,{src!r});"
        "from ai_core.agents_md import refresh;refresh(Path(sys.argv[1]))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                [sys.executable, "-c", code, str(root)],
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(root),
                env=env,
                **detached_popen_kwargs(),
            )
    except Exception:
        pass


def _spawn_memory_sync(root: Path, agent: str) -> None:
    """Spawn the opt-in cross-machine memory sync DETACHED (off the hook hot path). The
    sync does git fetch/push; this only launches it. No-op unless memory_sync.enabled, and
    a cooldown bounds how often it runs so rapid turn-end Stops don't hammer the remote."""
    import os
    import subprocess

    from .portable import detached_popen_kwargs

    try:
        from .memory_sync import sync_enabled

        if not sync_enabled(root):
            return
    except Exception:
        return
    try:
        cooldown = float(os.environ.get("AI_MEMORY_SYNC_COOLDOWN", "120"))
    except (TypeError, ValueError):
        cooldown = 120.0
    lock = root / ".ai" / "cache" / "memory_sync.lock"
    try:
        if cooldown > 0 and lock.exists() and (time.time() - lock.stat().st_mtime) < cooldown:
            return
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    ai_bin = root / ".ai" / "bin" / "ai"
    if not ai_bin.exists():
        return
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                [str(ai_bin), "memory", "sync", "--agent", agent, "--json"],
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(root),
                **detached_popen_kwargs(),
            )
    except Exception:
        pass


def _spawn_sleep_time_jobs(root: Path) -> dict[str, Any]:
    """Spawn background idle-time jobs (memory page-out, audit fold, index refresh).

    Fire-and-forget detached subprocess. Uses lock file (.ai/cache/sleep-time.lock)
    to prevent duplicate spawns within 600 seconds. Opt-out via AI_SLEEP_TIME=0/off.

    Returns:
      {"ok": bool, "spawned": [...], "skipped": bool, "reason": str | None}

    Errors are silently swallowed — hook response never fails.
    """
    import os
    import subprocess

    # Opt-out check
    if _env_disabled("AI_SLEEP_TIME", default="1"):
        return {"ok": True, "spawned": [], "skipped": True, "reason": "AI_SLEEP_TIME disabled"}

    # Lock-based dedup (600s cooldown)
    lock_path = root / ".ai" / "cache" / "sleep-time.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age < 600:
                return {"ok": True, "spawned": [], "skipped": True, "reason": "lock_recent"}
    except OSError:
        pass

    # Update lock
    try:
        lock_path.write_text("running", encoding="utf-8")
    except OSError:
        pass

    # Resolve ai binary
    from .portable import IS_WINDOWS, detached_popen_kwargs

    ai_bin_unix = root / ".ai" / "bin" / "ai"
    ai_bin_ps = root / ".ai" / "bin" / "ai.ps1"

    spawned: list[str] = []

    # Job 1: memory page-out (includes audit fold per T1)
    try:
        from .process_janitor import cleanup_children, register_child

        cleanup_children(root)
        if IS_WINDOWS and ai_bin_ps.exists():
            cmd = ["powershell", "-NoProfile", "-File", str(ai_bin_ps), "memory", "page-out", "--json"]
        elif ai_bin_unix.exists():
            cmd = [str(ai_bin_unix), "memory", "page-out", "--json"]
        else:
            return {"ok": False, "spawned": spawned, "skipped": False, "reason": "ai_bin_not_found"}

        with open(os.devnull, "wb") as devnull:
            proc = subprocess.Popen(
                cmd,
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(root),
                **detached_popen_kwargs(),
            )
        register_child(root, pid=proc.pid, kind="sleep_time_page_out", command=cmd)
        spawned.append(f"page_out(pid={proc.pid})")
    except Exception:
        pass

    # Job 2: index rebuild (optional, only if not just done in Stop handler)
    try:
        from .process_janitor import register_child

        if IS_WINDOWS and ai_bin_ps.exists():
            cmd = [
                "powershell", "-NoProfile", "-File", str(ai_bin_ps),
                "index", "rebuild", "--single-flight", "--incremental", "--json"
            ]
        elif ai_bin_unix.exists():
            cmd = [
                str(ai_bin_unix),
                "index", "rebuild", "--single-flight", "--incremental", "--json"
            ]
        else:
            pass  # skip if no binary

        if ai_bin_unix.exists() or (IS_WINDOWS and ai_bin_ps.exists()):
            with open(os.devnull, "wb") as devnull:
                proc = subprocess.Popen(
                    cmd,
                    stdout=devnull,
                    stderr=devnull,
                    stdin=subprocess.DEVNULL,
                    cwd=str(root),
                    **detached_popen_kwargs(),
                )
            register_child(root, pid=proc.pid, kind="sleep_time_index_rebuild", command=cmd)
            spawned.append(f"index_rebuild(pid={proc.pid})")
    except Exception:
        pass

    # Job 3: sandbox prune — clean stale sandbox executions older than 24h.
    # Without this background trigger, .ai/cache/sandbox accumulates indefinitely
    # (every sandbox_execute writes a .txt + .meta.json pair). Large/long-lived
    # projects had observed 360+ files / 16 MB before this hook was wired.
    try:
        from .process_janitor import register_child

        if IS_WINDOWS and ai_bin_ps.exists():
            cmd = ["powershell", "-NoProfile", "-File", str(ai_bin_ps),
                   "exec", "prune", "--older-than-seconds", "86400", "--json"]
        elif ai_bin_unix.exists():
            cmd = [str(ai_bin_unix), "exec", "prune", "--older-than-seconds", "86400", "--json"]
        else:
            cmd = None

        if cmd is not None:
            with open(os.devnull, "wb") as devnull:
                proc = subprocess.Popen(
                    cmd,
                    stdout=devnull,
                    stderr=devnull,
                    stdin=subprocess.DEVNULL,
                    cwd=str(root),
                    **detached_popen_kwargs(),
                )
            register_child(root, pid=proc.pid, kind="sleep_time_sandbox_prune", command=cmd)
            spawned.append(f"sandbox_prune(pid={proc.pid})")
    except Exception:
        pass

    # Job 4 (P3): refresh origin refs so SessionStart's cb-behind banner can detect a
    # remote machine being ahead. Network — so OPT-IN only (AI_REMOTE_FETCH=1), keeping
    # Code Brain's offline-by-default ethos. Detached + off the hook hot path; failures
    # (offline, no remote, auth) are swallowed. SessionStart never fetches; it only reads
    # the ref this job updated.
    if _env_enabled("AI_REMOTE_FETCH"):
        try:
            from .process_janitor import register_child

            cmd = ["git", "-C", str(root), "fetch", "--quiet", "--no-tags"]
            with open(os.devnull, "wb") as devnull:
                proc = subprocess.Popen(
                    cmd,
                    stdout=devnull,
                    stderr=devnull,
                    stdin=subprocess.DEVNULL,
                    cwd=str(root),
                    **detached_popen_kwargs(),
                )
            register_child(root, pid=proc.pid, kind="sleep_time_git_fetch", command=cmd)
            spawned.append(f"git_fetch(pid={proc.pid})")
        except Exception:
            pass

    return {"ok": True, "spawned": spawned, "skipped": False, "reason": None}


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


def _cooldown_score(age_hours: float, half_life_hours: float, importance: float = 1.0) -> float:
    """Ebbinghaus exponential-decay cooldown weight in [0, 1].

    score = 0.5 ** (age / (half_life * max(importance, 0.1)))

    - age_hours <= 0       → 1.0 (just surfaced; full penalty)
    - half_life_hours <= 0 → 0.0 (Ebbinghaus disabled; no penalty)
    - importance == 1.0    → legacy behaviour (bit-identical)
    - importance > 1.0     → effective half-life is longer, decay is slower
    - importance < 1.0     → effective half-life is shorter, decay is faster
    - importance <= 0      → clamped to 0.1 floor to avoid division-by-zero
    """
    if half_life_hours <= 0:
        return 0.0
    if age_hours <= 0:
        return 1.0
    effective_half_life = half_life_hours * max(importance, 0.1)
    return 0.5 ** (age_hours / effective_half_life)


def _cooldown_weights(
    root: Path,
    half_life_hours: float,
    importance_fn: "Callable[[str], float] | None" = None,
) -> dict[str, float]:
    """Build {candidate_id: decay_weight in [0,1]} from recommend_pending audit events.

    For each candidate id, use the most-recent recommend_pending ts to compute its
    current age in hours, then map via _cooldown_score(age, half_life, importance).

    Disabled (returns empty dict) when half_life_hours <= 0.

    importance_fn: optional callable(candidate_id) -> float. If None, every
    candidate gets importance=1.0 (legacy behaviour). Returning >1.0 slows
    decay for important candidates; <1.0 speeds it up.
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
        importance = 1.0
        if importance_fn is not None:
            try:
                importance = float(importance_fn(cid))
            except Exception:
                importance = 1.0
        weights[cid] = _cooldown_score(age_hours, half_life_hours, importance)
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


def _importance_from_strength(raw_strength: int) -> float:
    """Map raw signal count to a half-life multiplier in [1.0, ~2.75].

    FSFM/YourMemory: strongly-evidenced candidates persist longer in cooldown so
    one-off weak signals are re-surfaced faster than repeatedly-seen patterns.
    raw=0 → 1.0, raw=1 → ~1.25, raw=8 → ~1.79, raw=64 → ~2.5.
    Capped so a single hot candidate cannot dominate the queue forever.
    """
    import math

    if raw_strength <= 0:
        return 1.0
    return min(2.75, 1.0 + math.log2(1 + raw_strength) / 4.0)


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
    # T43: candidate-level importance signal. Explicit `"importance"` key wins;
    # otherwise fall back to raw evidence strength so frequently-evidenced
    # candidates decay slower (FSFM/YourMemory).
    def _cand_importance(cid_lookup: str) -> float:
        for c in candidates:
            if not isinstance(c, dict) or str(c.get("id") or "") != cid_lookup:
                continue
            raw = c.get("importance")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
            return _importance_from_strength(_candidate_raw_strength(c))
        return 1.0

    if cooldown_weights and env_half_life > 0:
        # Re-compute weights using the per-candidate importance hook. The
        # earlier call (above) without importance_fn used legacy weights;
        # we replace it here once we know which candidates the recommender
        # returned. Safe no-op when no candidate sets `"importance"`.
        cooldown_weights = _cooldown_weights(root, half_life, _cand_importance)

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
_HOOK_SUMMARY_CACHE_TTL_SECONDS = 300


def _cached_hook_summary(
    root: Path,
    *,
    cache_name: str,
    deps: list[Path],
    compute: "callable",
    cache_key_extra: tuple = (),
) -> str:
    """Cache expensive hook summary strings so SessionStart stays sublinear.

    Hook summaries are hints, not source-of-truth checks. A short TTL plus mtime
    dependencies keeps them fresh enough while preventing repeated audit-log
    parsing on every startup.
    """
    import time

    cache_path = root / ".ai" / "cache" / f"{cache_name}.json"
    if cache_path.exists():
        try:
            cache_mt = cache_path.stat().st_mtime
            age = time.time() - cache_mt
            if age < _HOOK_SUMMARY_CACHE_TTL_SECONDS:
                if all((not p.exists()) or p.stat().st_mtime <= cache_mt for p in deps):
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict) and tuple(payload.get("extra") or ()) == cache_key_extra:
                        return str(payload.get("text") or "")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    text = str(compute() or "")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"extra": list(cache_key_extra), "text": text}), encoding="utf-8")
        import os as _os_atomic
        _os_atomic.replace(tmp, cache_path)
    except OSError:
        pass
    return text


def _audit_dependency_paths(root: Path) -> list[Path]:
    """Files whose mtimes should invalidate hot recommendation caches."""
    paths = [
        root / ".ai" / "memory" / "audit-index.jsonl",
        audit_path(root),
    ]
    paths.extend(all_audit_files(root))
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _codex_global_memory_path() -> Path:
    return Path("~/.codex/memories/raw_memories.md").expanduser()


def _recommend_memory_deps(
    root: Path,
    *,
    include_todos: bool = False,
    include_codex_global: bool = False,
) -> list[Path]:
    deps = [
        root / ".ai" / "memory" / "decisions.jsonl",
        root / ".ai" / "memory" / "session-current.md",
    ]
    if include_todos:
        deps.append(root / ".ai" / "memory" / "todos.jsonl")
    if include_codex_global:
        deps.append(_codex_global_memory_path())
    deps.extend(_audit_dependency_paths(root))
    return deps


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
        ]
        deps.extend(_recommend_memory_deps(r, include_todos=True, include_codex_global=True))
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


def _try_autonomous_accept(root: Path, trigger: str) -> None:
    """T36: opt-in (AI_AUTONOMOUS_ACCEPT=1). Accept at most one strongest-signal
    pending skill candidate per Stop hook. Seeds the accept_ratio KPI that
    otherwise stays None forever, unlocking adaptive_min_signal_lower.

    Eligibility (all required):
      - candidate is pending (not accepted/rejected/installed)
      - raw signal_strength >= AI_AUTONOMOUS_ACCEPT_MIN_STRENGTH (default 30)
      - haven't auto-accepted within the last AI_AUTONOMOUS_ACCEPT_COOLDOWN_HOURS
        (default 24) to avoid runaway installs.
    Records:
      - audit row `skill.auto_accept` so the user can grep / audit / `ai recommend
        skills reject` to undo.
    """
    try:
        cooldown_hours = float(_os.environ.get("AI_AUTONOMOUS_ACCEPT_COOLDOWN_HOURS", "24"))
    except (TypeError, ValueError):
        cooldown_hours = 24.0
    try:
        min_strength = int(_os.environ.get("AI_AUTONOMOUS_ACCEPT_MIN_STRENGTH", "30"))
    except (TypeError, ValueError):
        min_strength = 30

    # cooldown check via audit
    audit_files = all_audit_files(root)
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    for af in audit_files:
        try:
            for line in af.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or "skill.auto_accept" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str(rec.get("ts") or "")
                try:
                    parsed = (datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
                              if ts.endswith("Z") else datetime.fromisoformat(ts))
                except ValueError:
                    continue
                if parsed >= cutoff:
                    return  # already auto-accepted recently
        except OSError:
            continue

    # find strongest eligible candidate
    try:
        from .recommend import list_catalog, accept as _accept
    except Exception:
        return
    candidates = list_catalog(root)
    best = None
    best_strength = -1
    for entry in candidates:
        if entry.status != "pending":
            continue
        sigs = (entry.evidence or {}).get("signals") or []
        if not sigs:
            continue
        first = str(sigs[0])
        if ":" not in first:
            continue
        try:
            s = int(first.split(":", 1)[1])
        except ValueError:
            continue
        if s < min_strength:
            continue
        if s > best_strength:
            best = entry
            best_strength = s
    if best is None:
        return
    result = _accept(root, best.id)
    from .memory import append_audit
    append_audit(
        root, action="skill.auto_accept", category="memory",
        payload={
            "id": best.id, "slug": best.slug, "strength": best_strength,
            "trigger": trigger, "ok": bool(result.get("ok")),
            "reason": result.get("reason"),
        },
    )


def _agent_recommendation_context(root: Path, hook_name: str, payload: dict[str, Any]) -> str:
    def invoke(r: Path, ms: int, _pl: dict[str, Any]) -> dict[str, Any]:
        def compute() -> dict[str, Any]:
            from .agent_recommend import recommend as agent_recommend

            return agent_recommend(r, limit=3, min_signal=ms)

        deps = [
            r / ".ai" / "agents_catalog" / "catalog.jsonl",
        ]
        deps.extend(_recommend_memory_deps(r, include_todos=False, include_codex_global=True))
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
        ]
        deps.extend(_recommend_memory_deps(r, include_todos=False, include_codex_global=False))
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
    deps = _recommend_memory_deps(root)
    return _cached_hook_summary(
        root,
        cache_name="satisfaction_summary_hot",
        deps=deps,
        compute=lambda: _satisfaction_summary_context_uncached(root),
    )


def _satisfaction_summary_context_uncached(root: Path) -> str:
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


def _session_scope_summary(root: Path) -> str:
    """Nudge to ``/clear`` when many audit events accumulate since the most
    recent ``SessionStart`` marker in the current audit file.

    Returns "" when disabled, when no SessionStart marker is found in the
    tail window, or when the count is below threshold. Intended for
    UserPromptSubmit injection only — at SessionStart the count is zero
    so the line carries no signal.
    """
    if _env_disabled("AI_SESSION_SCOPE_SUMMARY"):
        return ""
    try:
        threshold = max(10, int(_os.environ.get("AI_SESSION_SCOPE_THRESHOLD", "30")))
    except (ValueError, TypeError):
        threshold = 30
    files = all_audit_files(root)
    if not files:
        return ""
    try:
        entries = _read_jsonl_tail(files[-1], 500)
    except Exception:
        return ""
    if not entries:
        return ""
    count = 0
    found_start = False
    for entry in reversed(entries):
        payload = entry.get("payload") or {}
        kind = str(payload.get("kind") or "")
        action = str(entry.get("action") or "")
        if action == "event.append" and kind == "SessionStart":
            found_start = True
            break
        count += 1
    if not found_start or count < threshold:
        return ""
    return (
        f"cb-scope: {count} audit events since SessionStart — "
        "if the topic has shifted, `/clear` before continuing."
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
    if effective_hook in {"SessionStart", "Stop", "SubagentStop"} and not (is_ci() or payload.get("dry") is True):
        try:
            from .process_janitor import cleanup_children
            cleanup_children(root)
        except Exception:
            pass

    precall_decision: dict[str, Any] | None = None
    commit_block_reason: str | None = None
    stream_guard_decision: dict[str, Any] | None = None
    try:
        from .stream_guard import decision_reason, evaluate_hook_payload

        scan = evaluate_hook_payload(str(effective_hook), payload)
        if scan.get("matches"):
            stream_guard_decision = {
                "action": "block" if not scan.get("ok", True) else "observe",
                "reason": decision_reason(scan),
                "matches": scan.get("matches", []),
            }
    except Exception:
        stream_guard_decision = None

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

                    record_user_override(
                        root,
                        rid,
                        str(
                            tool_input.get("command")
                            or tool_input.get("CommandLine")
                            or tool_input.get("commandLine")
                            or ""
                        ),
                    )
                except Exception:
                    pass
        except Exception:
            precall_decision = None

        try:
            command = str(
                tool_input.get("command")
                or tool_input.get("CommandLine")
                or tool_input.get("commandLine")
                or ""
            )
            from .commit_guard import commit_secret_reason

            commit_block_reason = commit_secret_reason(root, command)
        except Exception:
            commit_block_reason = None

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
    if stream_guard_decision:
        event["stream_guard"] = stream_guard_decision
        if stream_guard_decision.get("action") == "block":
            event["decision"] = "block"
    if is_ci() or payload.get("dry") is True:
        mode = "ci-fast-path" if is_ci() else "local-dry-fast-path"
        persisted = False
    else:
        append_event(root, event)
        mode = "local-append"
        persisted = True
        # Spawn the AGENTS.md memory refresh EARLY and detached. Stop/SessionEnd are
        # the natural triggers for Claude/Codex, but Antigravity kills its Stop hook
        # before the work runs (its Stop never reaches append_event) — so we also fire
        # on PostToolUse, which Antigravity DOES complete. A cooldown in the helper
        # bounds frequency; the refresh is write-on-change and detached so it finishes
        # even if the host kills the parent hook.
        if effective_hook in {"Stop", "SessionEnd", "PostToolUse"}:
            _spawn_agents_md_refresh(root)
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
            # T35 autonomous page-out: when MemGPT pressure breaches threshold,
            # the agent should not wait for a human `ai memory page-out` call.
            if not _env_disabled("AI_AUTO_PAGE_OUT"):
                try:
                    from .memory_tier import hot_pressure, page_out

                    if hot_pressure(root).get("page_out_recommended"):
                        page_out(root, dry_run=False)
                        from .memory import append_audit
                        append_audit(
                            root, action="memtier.auto_page_out", category="memory",
                            payload={"trigger": effective_hook},
                        )
                except Exception:
                    pass
            # T36 autonomous accept is write-class and therefore opt-in only.
            # Default automation still surfaces candidates; it does not install
            # commands unless the operator explicitly sets AI_AUTONOMOUS_ACCEPT=1.
            if _env_enabled("AI_AUTONOMOUS_ACCEPT", default="0"):
                try:
                    _try_autonomous_accept(root, effective_hook)
                except Exception:
                    pass
        try:
            _handle_lifecycle_event(root, effective_hook, payload)
        except Exception:
            pass
        # T6: spawn sleep-time idle jobs after SessionEnd/Stop (memory page-out, audit fold, index refresh)
        if effective_hook in {"Stop", "SessionEnd"}:
            try:
                _spawn_sleep_time_jobs(root)
            except Exception:
                pass
            # P4: opt-in cross-machine memory auto-sync. Detached + off the hot path; the
            # sync itself does git fetch/push but this hook only spawns it. Gated by
            # memory_sync.enabled and a cooldown so rapid turn-end Stops don't hammer git.
            _spawn_memory_sync(root, normalize_agent(payload))
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    target_ms = _target_ms_for(effective_hook)
    if persisted and elapsed_ms > target_ms:
        try:
            from .memory import append_audit

            append_audit(
                root,
                action="hook.slow",
                category="hook",
                payload={"hook": effective_hook, "elapsed_ms": elapsed_ms, "target_ms": target_ms},
            )
        except Exception:
            pass
    response = {
        "ok": True,
        "hook": effective_hook,
        "mode": mode,
        "persisted": persisted,
        "elapsed_ms": elapsed_ms,
        "target_ms": target_ms,
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
                    "updatedInput": {
                        "command": suggestion,
                        "CommandLine": suggestion,
                        "commandLine": suggestion,
                    },
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
    if effective_hook == "PreToolUse" and commit_block_reason:
        # Secret-in-commit gate (Claude via per-project ai-hook + Codex). Takes precedence:
        # blocking a credential entering history matters more than search-routing.
        response["decision"] = "block"
        response["hookSpecificOutput"] = {
            "hookEventName": effective_hook,
            "permissionDecision": "deny",
            "permissionDecisionReason": commit_block_reason,
        }
        response["reason"] = commit_block_reason
    if stream_guard_decision:
        response["stream_guard"] = stream_guard_decision
        # Blocking is only meaningful BEFORE a tool runs (PreToolUse) or for a
        # prompt/stop. On PostToolUse the tool already executed, so promoting a
        # match to decision=block is both pointless and emits a wire shape Codex
        # rejects as "invalid post-tool-use JSON output". There the match still
        # drives redaction (updatedToolOutput) and is recorded in the audit.
        if (
            stream_guard_decision.get("action") == "block"
            and effective_hook != "PostToolUse"
            and response.get("decision") != "block"
        ):
            response["decision"] = "block"
            reason = str(stream_guard_decision.get("reason") or "Code Brain stream guard blocked this operation")
            existing = response.get("hookSpecificOutput")
            if not isinstance(existing, dict):
                existing = {"hookEventName": effective_hook}
            existing["permissionDecision"] = "deny"
            existing["permissionDecisionReason"] = reason
            existing["additionalContext"] = additional_context
            response["hookSpecificOutput"] = existing
            response["reason"] = reason
    # T44: PostToolUse `updatedToolOutput` — Claude Code 2026 spec field. When a
    # tool's stdout contains secrets (or long matches), we redact and surface
    # the cleaned version via hookSpecificOutput.updatedToolOutput so the model
    # never sees the raw secret. Opt out with AI_HOOK_REDACT_TOOL_OUTPUT=0.
    if effective_hook == "PostToolUse" and not _env_disabled("AI_HOOK_REDACT_TOOL_OUTPUT"):
        raw_tool_output: Any = None
        if isinstance(payload.get("tool_response"), (str, dict, list)):
            raw_tool_output = payload.get("tool_response")
        elif isinstance(payload.get("tool_output"), (str, dict, list)):
            raw_tool_output = payload.get("tool_output")
        if raw_tool_output is not None:
            cleaned = redact_value(raw_tool_output)
            # `updatedToolOutput` MUST be a string per the Claude Code / Codex hook
            # spec. A dict/list value (e.g. exec_command's {"stdout":...} response
            # after redaction) makes the client reject the whole hook as "invalid
            # post-tool-use JSON output". Only surface the cleaned value when it is
            # a string; structured outputs are still scrubbed in the persisted audit
            # copy via the redact_value(response) below.
            if isinstance(cleaned, str) and cleaned != raw_tool_output:
                existing = response.get("hookSpecificOutput")
                if not isinstance(existing, dict):
                    existing = {"hookEventName": effective_hook}
                existing["updatedToolOutput"] = cleaned
                response["hookSpecificOutput"] = existing
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
    if hook == "PostToolUse":
        # T44: preserve `updatedToolOutput` when redact stage produced one.
        updated_tool_output = hook_specific.get("updatedToolOutput")
        if additional_context or updated_tool_output is not None:
            out: dict[str, Any] = {"hookEventName": "PostToolUse"}
            if additional_context:
                out["additionalContext"] = str(additional_context)
            if updated_tool_output is not None:
                out["updatedToolOutput"] = updated_tool_output
            return {"hookSpecificOutput": out}
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
        agent = normalize_agent(payload)
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
                    # Restore freshness *before* snapshotting so the resume snapshot
                    # carries real progress instead of frozen, fresh-looking-stale state.
                    try:
                        _auto_milestone_on_stale(root)
                    except Exception:
                        pass
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
            description = str(
                raw_input.get("description")
                or raw_input.get("Reason")
                or raw_input.get("reason")
                or ""
            )[:200]
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
        return

    if hook_name == "SubagentStart":
        agent_id = str(payload.get("agent_id") or payload.get("subagent_id") or "")
        agent_type = str(payload.get("agent_type") or payload.get("subagent_type") or "")
        append_audit(
            root,
            action="subagent.started",
            category="memory",
            payload={"agent_id": agent_id[:64], "agent_type": agent_type[:64]},
        )
        return

    if hook_name == "TaskCreated":
        title = str(payload.get("title") or payload.get("subject") or "").strip()
        if title:
            try:
                from .memory import append_todo
                append_todo(root, title=title[:200], source="task_hook")
            except Exception:
                pass
        return

    if hook_name == "TaskCompleted":
        match = str(payload.get("title") or payload.get("subject") or payload.get("task_id") or "").strip()
        if match:
            try:
                from .memory import close_todo
                close_todo(root, match=match[:200], status="done", reason="task_hook")
            except Exception:
                pass
        return

    if hook_name == "FileChanged":
        file_path = str(payload.get("file_path") or payload.get("path") or "")
        append_audit(
            root,
            action="file.changed",
            category="memory",
            payload={"file_path": file_path[:200]},
        )
        return

    if hook_name == "PostToolUseFailure":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
        error = str(payload.get("error") or payload.get("error_message") or "")[:200]
        append_audit(
            root,
            action="tool.failed",
            category="hook",
            payload={"tool_name": tool_name[:64], "error": error},
        )
        return


def build_context(hook_name: str, payload: dict[str, Any], *, root: Path | None = None) -> str:
    agent = normalize_agent(payload)
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
    sections.append(
        "Code reading: after `code_query` locates a file, prefer MCP `code_read_hashline` "
        "for exact file slices so line+hash anchors are available before edits. "
        "For CLI fallback, use `.ai/bin/ai code read-hashline <path> --start N --end M`; "
        "verify saved anchors with `.ai/bin/ai code verify-hashline <path>` when stale edits matter."
    )
    staleness = _memory_staleness_context(root, hook_name)
    if staleness:
        sections.append(staleness)
    if hook_name == "SessionStart":
        map_context = _codebase_map_summary_context(root)
        if map_context:
            sections.append(map_context)
        try:
            from .autonomous_harness import context_line as _harness_context_line
            sections.append(_harness_context_line(root))
        except Exception:
            pass
    if hook_name == "UserPromptSubmit":
        try:
            from .autonomous_harness import directive as _harness_directive, requested as _harness_requested
            if _harness_requested(payload):
                sections.append(_harness_directive(root, explicit=True))
        except Exception:
            pass
        scope_line = _session_scope_summary(root)
        if scope_line:
            sections.append(scope_line)
    elif hook_name == "SessionStart":
        try:
            from .session_resume import read_latest_snapshot
            current_sid = str(payload.get("session_id") or payload.get("sid") or "")
            prior = read_latest_snapshot(root, exclude_session_id=current_sid or None)
        except Exception:
            prior = None
        if prior:
            lines = [f"Prior session resume (session_id={prior.get('session_id')}, written_at={prior.get('written_at')}):"]
            # P1: lead with the intent-carrying handoff so a resuming session (esp. on
            # the other machine) sees "what we were doing / what to do next" first.
            handoff = prior.get("handoff") if isinstance(prior.get("handoff"), dict) else None
            if handoff:
                if handoff.get("goal"):
                    lines.append(f"  goal: {str(handoff['goal'])[:200]}")
                if handoff.get("next_step"):
                    lines.append(f"  next step: {str(handoff['next_step'])[:200]}")
                for step in (handoff.get("plan") or [])[:6]:
                    lines.append(f"  plan: {str(step)[:160]}")
                for q in (handoff.get("open_questions") or [])[:4]:
                    lines.append(f"  open question: {str(q)[:160]}")
                for b in (handoff.get("blockers") or [])[:4]:
                    lines.append(f"  blocker: {str(b)[:160]}")
            # P2: cross-machine pointer — if the prior thread ran on another machine,
            # its full transcript stays there (all 3 agents are local-only); tell the
            # resuming agent where it is and how to reopen it.
            try:
                from .session_resume import machine_id as _machine_id
                here = _machine_id(root)
            except Exception:
                here = ""
            prior_machine = str(prior.get("machine_id") or "")
            if prior_machine and here and prior_machine != here:
                hint = str(prior.get("resume_hint") or "").strip()
                hint_txt = f" Reopen its full transcript there with `{hint}`." if hint else ""
                lines.append(
                    f"  cross-machine: prior thread ran on '{prior_machine}' via {prior.get('agent') or 'unknown'} "
                    f"(you are on '{here}'). Its full conversation stays on that machine.{hint_txt} "
                    f"Use memory_query/context_pack here for detail."
                )
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
        memory_tier = _memory_tier_summary_context(root)
        if memory_tier:
            sections.append(memory_tier)
    # T35 codegraph hotspot teaser — surfaces the top-3 most-called callees so
    # downstream agents can `code_graph_callers <name>` without prior knowledge.
    if hook_name in SKILL_RECOMMENDATION_HOOKS and not _env_disabled("AI_CODEGRAPH_SUMMARY"):
        try:
            from .codegraph import hotspot_callees
            hot = hotspot_callees(root, limit=3)
            entries = hot.get("hotspots") or []
            if entries:
                top = ", ".join(f"{h['callee']}({h['calls']})" for h in entries)
                sections.append(f"cb-graph: top callees — {top}. MCP: code_graph_callers/callees/symbol/hotspots.")
        except Exception:
            pass
    session_tail = _read_text_tail(root / ".ai" / "memory" / "session-current.md", SESSION_TAIL_LINES)
    if session_tail:
        sections.append("Session-current tail:\n" + session_tail)
    # T37 — cloudflare remote_memory removed (.ai/ git sync handles cross-device).
    composed = "\n\n".join(sections)
    max_bytes = _max_injection_bytes_for(hook_name)
    if len(composed.encode("utf-8")) > max_bytes:
        truncated = composed.encode("utf-8")[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."
        composed = truncated
    return composed


def _memory_staleness_context(root: Path, hook_name: str) -> str:
    """Banner warning that shared memory has fallen behind git progress.

    The navio incident: agents stopped calling record tools, so session-current.md /
    decisions.jsonl froze while git advanced and the resume snapshot kept looking
    fresh. Surfacing the gap at every injection point makes all agents converge on
    git truth instead of diverging from their own native memory. Cached (mtime +
    TTL) so the git calls never threaten the hot-path budget. Opt-out: AI_MEMORY_STALENESS=0.
    """
    if hook_name not in CONTEXT_INJECTION_HOOKS:
        return ""
    if _env_disabled("AI_MEMORY_STALENESS", default="1"):
        return ""

    def compute() -> str:
        banners: list[str] = []
        try:
            from .memory_staleness import remote_sync_banner, staleness_banner

            local = staleness_banner(root)
            if local:
                banners.append(local)
            # P3: remote-ahead (cb-behind) — another machine pushed work we lack. Reads
            # the already-fetched upstream ref only (no fetch here); the fetch runs in
            # the detached sleep-time job. Closes the silent cross-machine divergence gap.
            behind = remote_sync_banner(root)
            if behind:
                banners.append(behind)
        except Exception:
            return ""
        # P4: peer heartbeat summary — "VPS synced 3m ago" — when memory auto-sync runs
        # on another machine. Local file reads only; absent for single-machine users.
        try:
            from .memory_sync import peer_sync_summary

            peers = peer_sync_summary(root)
            if peers:
                banners.append(peers)
        except Exception:
            pass
        return "\n\n".join(banners)

    deps = [
        root / ".ai" / "memory" / "session-current.md",
        root / ".ai" / "memory" / "decisions.jsonl",
        root / ".git" / "HEAD",
        root / ".git" / "index",
        # .git/HEAD only changes on branch switch; a new commit on the *same*
        # branch must still bust the cache. The HEAD reflog is appended on every
        # HEAD movement (commit/checkout/reset), so its mtime is the reliable
        # signal — and it is a cheap stat, no extra git subprocess on the hot path.
        root / ".git" / "logs" / "HEAD",
        # FETCH_HEAD mtime changes whenever the sleep-time job runs `git fetch`, so the
        # cb-behind banner refreshes after new remote commits are fetched.
        root / ".git" / "FETCH_HEAD",
        # peer heartbeats change when another machine's memory sync runs.
        root / ".ai" / "memory" / "sync",
    ]
    return _cached_hook_summary(
        root, cache_name="memory_staleness", deps=deps, compute=compute
    )


def _auto_milestone_on_stale(root: Path) -> bool:
    """At SessionEnd, if git advanced past recorded memory and the agent forgot to
    log it, append a *factual* milestone so the next session (and the resume
    snapshot composed right after) reflect reality instead of frozen state.

    Deliberately records git facts only — commit subjects + dirty count, never an
    LLM summary — so an automated note can never embed a hallucinated "agreed wrong
    answer" into shared memory (the panel's main objection). Deduped by HEAD sha so
    a stale-but-unchanged tree is captured at most once. Opt-out: AI_AUTO_MILESTONE_ON_STALE=0.

    Returns True when a note was written.
    """
    if _env_disabled("AI_AUTO_MILESTONE_ON_STALE", default="1"):
        return False
    try:
        from .memory_staleness import memory_freshness

        info = memory_freshness(root)
    except Exception:
        return False
    if not info.get("stale"):
        return False

    head = str(info.get("head") or "")
    marker = f"[auto:{head}]" if head else "[auto]"
    try:
        tail = _read_text_tail(root / ".ai" / "memory" / "session-current.md", 5)
    except Exception:
        tail = ""
    if marker in tail:
        return False

    commits = info.get("commits") or []
    count = int(info.get("commit_count") or 0)
    dirty = int(info.get("dirty_count") or 0)
    bits = [f"{marker} 에이전트 미기록 자동 캡처(git 사실 기반)"]
    if count:
        subject = str(commits[0].get("subject") or "") if commits else ""
        more = f" 외 {count - 1}개" if count > 1 else ""
        bits.append(f"커밋 {count}개{more} 최근:{subject[:70]}")
    if dirty:
        bits.append(f"dirty {dirty}파일")
    text = " · ".join(bits) + ". 어디까지 진행했는지는 git log/status가 정답."
    try:
        from .memory import append_session_note

        append_session_note(root, text=text)
        return True
    except Exception:
        return False


def _memory_tier_summary_context(root: Path) -> str:
    deps = [
        root / ".ai" / "memory" / "audit-index.jsonl",
        root / ".ai" / "memory" / "todos.jsonl",
        root / ".ai" / "memory" / "decisions.jsonl",
        root / ".ai" / "memory" / "session-current.md",
    ]
    deps.extend(all_audit_files(root))

    def compute() -> str:
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
            return sline
        except Exception:
            return ""

    return _cached_hook_summary(root, cache_name="memory_tier_hot", deps=deps, compute=compute)


def _codebase_map_summary_context(root: Path) -> str:
    deps = [
        root / ".git" / "index",
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "package.json",
        root / "pyproject.toml",
        root / "pubspec.yaml",
    ]

    def compute() -> str:
        try:
            from .codebase_map import build_codebase_map
            map_payload = build_codebase_map(root, max_entries=12, include_untracked=False)
            return str(map_payload.get("additionalContext") or "")
        except Exception:
            return ""

    return _cached_hook_summary(root, cache_name="codebase_map_hot", deps=deps, compute=compute)
