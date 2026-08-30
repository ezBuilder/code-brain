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
    now_iso,
    read_jsonl_open_todos as _read_jsonl_open_todos,
    read_jsonl_tail as _read_jsonl_tail,
    read_text_tail as _read_text_tail,
)
from .policy import is_ci
from .private_write import (
    atomic_write_private_text,
    private_file_lock,
    read_root_confined_text,
    validate_root_confined_regular_file,
)
from .redact import redact_value

import os as _os


def _read_hook_state_text(
    root: Path,
    path: Path,
    *,
    max_bytes: int = 100_000_000,
) -> str:
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=max_bytes,
            require_private=False,
        )
        return text
    except (OSError, UnicodeDecodeError):
        return ""

HOT_PATH_TARGET_MS = 200
SESSION_START_TARGET_MS = 1500
INJECTION_HOOKS = {"SessionStart", "UserPromptSubmit", "SubagentStart"}
AUTO_REBUILD_HOOKS = {"Stop", "SubagentStop", "FileChanged"}
CONTEXT_INJECTION_HOOKS = {"UserPromptSubmit", "SessionStart", "SubagentStart"}
SKILL_RECOMMENDATION_HOOKS = {"SessionStart"}

try:
    MAX_INJECTION_BYTES = max(256, min(8192, int(_os.environ.get("AI_INJECTION_MAX_BYTES", "2048"))))
except (ValueError, TypeError):
    MAX_INJECTION_BYTES = 2048
try:
    SESSION_START_MAX_INJECTION_BYTES = max(
        MAX_INJECTION_BYTES,
        min(32768, int(_os.environ.get("AI_SESSION_START_MAX_BYTES", "8192"))),
    )
except (ValueError, TypeError):
    SESSION_START_MAX_INJECTION_BYTES = max(MAX_INJECTION_BYTES, 8192)
DECISIONS_TAIL = 3
TODOS_LIMIT = 3
SESSION_TAIL_LINES = 4
PRIOR_SESSION_TAIL_LINES = 4

KNOWN_AGENTS = {"claude", "codex", "antigravity", "kiro"}

# Events whose native host contract can prevent a turn or worker from ending. Keep
# task/teammate quality gates separate from actual turn-end hooks: their wire contract is
# exit-code based on Claude, and a task may finish while the request-level plan continues.
_STOP_LIKE_HOOKS = frozenset({"Stop", "SubagentStop"})
_QUALITY_GATE_HOOKS = frozenset({"TaskCompleted", "TeammateIdle"})
_COMPLETION_GUARD_HOOKS = _STOP_LIKE_HOOKS | _QUALITY_GATE_HOOKS


def normalize_agent(payload: dict[str, Any]) -> str:
    """Map a hook payload's agent identifier to one of the canonical names.

    Returns a canonical host name such as ``claude``, ``codex``, ``antigravity``,
    ``kiro``, or ``unknown``. We
    prefer an explicit ``agent`` (or ``agent_name``) field. Antigravity command
    hooks do not provide one, so their documented camelCase payload signature
    (``conversationId``/``workspacePaths``/``terminationReason``/``fullyIdle``)
    is checked before host environment variables. The result is used both for
    wire projection, inject-context headers and obs/audit breakdown.
    """
    raw = payload.get("agent") or payload.get("agent_name") or ""
    norm = str(raw).strip().lower()
    aliases = {
        "claude": "claude", "claude-code": "claude", "claudecode": "claude",
        "codex": "codex", "codex-cli": "codex",
        "antigravity": "antigravity", "agy": "antigravity", "antigravity-cli": "antigravity",
        "kiro": "kiro", "kiro-cli": "kiro", "kiro-ide": "kiro",
    }
    if norm in aliases:
        return aliases[norm]
    if norm and norm != "unknown":
        return norm
    if any(
        key in payload
        for key in ("conversationId", "workspacePaths", "terminationReason", "fullyIdle")
    ):
        return "antigravity"
    env = _os.environ
    explicit_env = str(env.get("AI_HOOK_AGENT") or "").strip().lower()
    if explicit_env in aliases:
        return aliases[explicit_env]
    if env.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    if env.get("CODEX_HOME") or env.get("CODEX_TURN_ID"):
        return "codex"
    if env.get("ANTIGRAVITY_CLI") or env.get("AGY_HOME"):
        return "antigravity"
    if env.get("KIRO_CLI") or env.get("KIRO_HOME"):
        return "kiro"
    return "unknown"
DELTA_NOTICE_SHORT = "cb-ctx: Δ"
DELTA_NOTICE_VERBOSE = "Code Brain context unchanged since last injection (delta-skipped)."
SKILL_RECOMMENDATION_DISABLE_VALUES = {"0", "false", "no", "off"}
_ENV_ENABLE_VALUES = {"1", "true", "yes", "on"}
_RECOMMENDATION_OPT_IN_ENVS = {
    "AI_SKILL_RECOMMENDATIONS",
    "AI_AGENT_RECOMMENDATIONS",
    "AI_PRECALL_RECOMMENDATIONS",
}


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


def _claim_background_cooldown(
    root: Path,
    marker: Path,
    cooldown_seconds: float,
    *,
    marker_value: str | None = None,
) -> bool:
    """Atomically admit at most one detached job per cooldown window.

    Hook events can arrive concurrently from multiple host processes.  A plain
    ``exists/stat/write`` sequence lets every contender pass before any marker is
    written, multiplying detached children.  Serialize the marker transaction with
    a separate private file lock and fail closed on untrusted marker paths.
    """
    marker = Path(marker)
    guard = marker.with_name(f".{marker.name}.claim.lock")
    try:
        with private_file_lock(guard, root=Path(root)):
            try:
                state = validate_root_confined_regular_file(
                    marker,
                    root=Path(root),
                    require_owner=True,
                    reject_group_other_writable=True,
                )
            except FileNotFoundError:
                state = None
            except OSError:
                return False
            now = time.time()
            if (
                state is not None
                and float(cooldown_seconds) > 0
                and now - float(state.st_mtime) < float(cooldown_seconds)
            ):
                return False
            atomic_write_private_text(
                marker, marker_value if marker_value is not None else str(now), root=Path(root)
            )
            return True
    except (OSError, TypeError, ValueError):
        return False


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


def _spawn_agents_md_refresh(root: Path, agent: str = "") -> None:
    """Refresh the managed AGENTS.md memory block in a DETACHED process.

    Older Antigravity builds could end a slow ``Stop`` command before a synchronous
    refresh (which calls build_context, ~1s) finished. Current 2.0/CLI 1.1.x waits
    and supports continuation, but the Stop decision path must still stay fast.
    Running the refresh detached (own session, like _spawn_background_rebuild)
    keeps side effects out of that decision budget. The refresh is write-on-change, so
    repeated spawns don't churn AGENTS.md. Never raises into the hook hot path.

    Host-aware single-sourcing: Claude Code auto-loads only ``CLAUDE.md``, never
    ``AGENTS.md`` — refreshing the root ``AGENTS.md`` mirror for a Claude turn would be
    pure wasted work (there is no reader on that host). Codex CLI DOES auto-load root
    ``AGENTS.md``, but the mirrored block is DYNAMIC-ONLY and fingerprint-checked (see
    ``ai_core.agents_md``), so a Codex refresh is not duplication — it is what keeps the
    file current for whichever host (Codex again, or Antigravity, which has no hook path
    at all) reads it next, and lets Codex's own next SessionStart see ``is_current() ==
    True`` and skip re-injecting the dynamic body via the hook.
    """
    import os
    import subprocess
    import sys

    from .portable import detached_popen_kwargs

    if _env_disabled("AI_AGENTS_MD_MEMORY", default="1"):
        return
    if agent == "claude":
        return
    # Cooldown: PostToolUse can fire many times per turn. Spawn at most once per
    # window so we don't launch a build_context process on every tool call.
    try:
        cooldown = float(os.environ.get("AI_AGENTS_MD_REFRESH_COOLDOWN", "15"))
    except (TypeError, ValueError):
        cooldown = 15.0
    lock = root / ".ai" / "cache" / "agents_md_refresh.lock"
    if not _claim_background_cooldown(root, lock, cooldown):
        return
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


def _spawn_turn_report(root: Path, agent: str) -> None:
    """Snapshot what this turn changed in a DETACHED child (git facts only).

    Measured cost of the git calls: 36ms on code-brain, 92ms on navio, 687ms on blurivo —
    the last one alone blows the 200ms Stop budget, so this must never run inline. The
    child writes .ai/cache/turn_report.json; the next UserPromptSubmit decides whether the
    change was big enough to ask for a short summary. Never raises into the hot path.
    """
    import os
    import subprocess
    import sys

    from .portable import detached_popen_kwargs

    if _env_disabled("AI_TURN_REPORT", default="1"):
        return
    # PostToolUse/Stop can fire repeatedly; bound the spawn rate like the AGENTS.md refresh.
    try:
        cooldown = float(os.environ.get("AI_TURN_REPORT_COOLDOWN", "10"))
    except (TypeError, ValueError):
        cooldown = 10.0
    lock = root / ".ai" / "cache" / "turn_report.lock"
    if not _claim_background_cooldown(root, lock, cooldown):
        return
    src = str(root / ".ai" / "runtime" / "src")
    code = (
        "import sys;from pathlib import Path;"
        f"sys.path.insert(0,{src!r});"
        "from ai_core.turn_report import write_snapshot;"
        "write_snapshot(Path(sys.argv[1]), agent=sys.argv[2])"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                [sys.executable, "-c", code, str(root), str(agent or "")],
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(root),
                env=env,
                **detached_popen_kwargs(),
            )
    except Exception:
        pass


def _spawn_tokens_cache_refresh(root: Path) -> None:
    """Refresh the prompt_growth output-token total in a DETACHED child.

    `prompt_growth._output_tokens` used to aggregate every agent transcript INLINE inside
    the Stop hook. Measured: 8.06s on blurivo (623,836 JSON lines across 507 codex + 125
    claude session files), 6.5s on code-brain — and `tick` hits it on its growth cooldown,
    so one turn in five froze turn-end for 6-8s. The value is only recorded provenance on a
    rule (`baseline_tokens`); every ratchet decision uses `_recent_output_avg`, which reads
    the small local jsonl. So the hook now reads a TTL cache and this child fills it.

    Hourly cooldown matches the cache TTL: a rule needs RATCHET_WINDOW turns to graduate,
    so hour-scale staleness in a provenance field cannot change an outcome.
    """
    import os
    import subprocess
    import sys

    from .portable import detached_popen_kwargs

    # Explicit opt-in (default off): nothing reads this cache unless prompt_growth
    # itself is enabled, so spawning the multi-second transcript scan by default would
    # be a pure-waste background process on every Stop/SessionEnd.
    if _env_disabled("AI_PROMPT_GROWTH", default="0"):
        return
    try:
        cooldown = float(os.environ.get("AI_PROMPT_GROWTH_TOKENS_COOLDOWN", "3600"))
    except (TypeError, ValueError):
        cooldown = 3600.0
    lock = root / ".ai" / "cache" / "prompt_growth_tokens.lock"
    if not _claim_background_cooldown(root, lock, cooldown):
        return
    src = str(root / ".ai" / "runtime" / "src")
    code = (
        "import sys;from pathlib import Path;"
        f"sys.path.insert(0,{src!r});"
        "from ai_core.prompt_growth import refresh_output_tokens_cache;"
        "refresh_output_tokens_cache(Path(sys.argv[1]))"
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


SLEEP_TIME_HOOKS = {"Stop", "SessionEnd"}
# Turn-start events used only as a fallback when turn-end events never arrive.
SLEEP_TIME_FALLBACK_HOOKS = {"SessionStart", "UserPromptSubmit"}
# Only treat a turn-start as idle-time catch-up once the last spawn is this old.
SLEEP_TIME_FALLBACK_MIN_AGE_SECONDS = 6 * 60 * 60


def _sleep_time_fallback_due(root: Path) -> bool:
    """True when turn-start should stand in for a missing Stop/SessionEnd.

    Uses the same lock file as the spawn cooldown as a last-run marker. Missing
    lock means maintenance has never run here, so allow it. Any error is treated
    as not-due: background maintenance must never break a hook.
    """
    lock_path = root / ".ai" / "cache" / "sleep-time.lock"
    try:
        if not lock_path.exists():
            return True
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age >= SLEEP_TIME_FALLBACK_MIN_AGE_SECONDS


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

    # Cross-process cooldown admission.  The marker update must be one locked
    # transaction or concurrent Stop/SessionEnd events multiply every child job.
    lock_path = root / ".ai" / "cache" / "sleep-time.lock"
    if not _claim_background_cooldown(root, lock_path, 600, marker_value="running"):
        return {"ok": True, "spawned": [], "skipped": True, "reason": "lock_recent"}

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

    # Job 4 (T30 step C): memory page-in — consolidate a compact, salience-ranked
    # HOT cache so the next SessionStart injects a tighter, fewer-token context.
    # Deterministic + offline; ordered AFTER page-out so tiers settle first.
    # Explicit opt-in: the ranked HOT cache is an optional optimization, not required
    # for bounded retention or correctness. Keeping it off avoids an otherwise unused
    # detached process on every sleep-time cycle; page-out below remains always-on for
    # storage rotation/folding.
    if _env_enabled("AI_MEMORY_PAGE_IN", default="0"):
        try:
            from .process_janitor import register_child

            if IS_WINDOWS and ai_bin_ps.exists():
                cmd = ["powershell", "-NoProfile", "-File", str(ai_bin_ps),
                       "memory", "page-in", "--json"]
            elif ai_bin_unix.exists():
                cmd = [str(ai_bin_unix), "memory", "page-in", "--json"]
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
                register_child(root, pid=proc.pid, kind="sleep_time_page_in", command=cmd)
                spawned.append(f"page_in(pid={proc.pid})")
        except Exception:
            pass

    return {"ok": True, "spawned": spawned, "skipped": False, "reason": None}


def _parse_audit_ts_utc(ts: str):
    """Audit 'ts' → aware UTC datetime, or None (fail-soft).

    An offset-less value is read as UTC, matching now_iso(). Every caller compares
    the result against an aware cutoff (or sorts/subtracts it against aware peers),
    so returning a naive datetime here would raise TypeError PAST the callers'
    except-ValueError guards — audit lines are git-synced and hand-editable, so the
    offset-less shape does arrive. Twin of lessons._parse_ts.
    """
    from datetime import datetime, timezone
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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
        content = _read_hook_state_text(root, audit_file)
        if not content:
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
            parsed = _parse_audit_ts_utc(ts)
            if parsed is None or parsed < cutoff:
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
        content = _read_hook_state_text(root, audit_file)
        if not content:
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
            parsed = _parse_audit_ts_utc(ts)
            if parsed is None:
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
        content = _read_hook_state_text(root, audit_file)
        if not content:
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
        content = _read_hook_state_text(root, audit_file)
        if not content:
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
    default_toggle = "0" if env_toggle in _RECOMMENDATION_OPT_IN_ENVS else "1"
    if _env_disabled(env_toggle, default=default_toggle):
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
    try:
        cache_text, cache_state = read_root_confined_text(
            cache_path,
            root=root,
            max_bytes=2_000_000,
            require_private=True,
        )
        cache_mt = cache_state.st_mtime
        age = time.time() - cache_mt
        if age < _HOOK_SUMMARY_CACHE_TTL_SECONDS:
            if all((not p.exists()) or p.stat().st_mtime <= cache_mt for p in deps):
                payload = json.loads(cache_text)
                if isinstance(payload, dict) and tuple(payload.get("extra") or ()) == cache_key_extra:
                    return str(payload.get("text") or "")
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    text = str(compute() or "")
    try:
        atomic_write_private_text(
            cache_path,
            json.dumps({"extra": list(cache_key_extra), "text": text}),
            root=root,
        )
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
    try:
        cache_text, cache_state = read_root_confined_text(
            cache_path,
            root=root,
            max_bytes=2_000_000,
            require_private=True,
        )
        cache_mt = cache_state.st_mtime
        age = time.time() - cache_mt
        if age < _RECOMMEND_CACHE_TTL_SECONDS:
            if all((not p.exists()) or p.stat().st_mtime <= cache_mt for p in deps):
                payload = json.loads(cache_text)
                if (
                    isinstance(payload, dict)
                    and payload.get("min_signal") == min_signal
                    and tuple(payload.get("extra") or ()) == cache_key_extra
                ):
                    return payload.get("result") or {"candidates": []}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    result = compute()
    try:
        atomic_write_private_text(
            cache_path,
            json.dumps(
                {"min_signal": min_signal, "extra": list(cache_key_extra), "result": result}
            ),
            root=root,
        )
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
        header="Skill candidates; install only after explicit approval:",
        approval_line="Approve: `ai recommend skills accept <id>`; reject: `ai recommend skills reject <id>`.",
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
        for line in _read_hook_state_text(root, af).splitlines():
            line = line.strip()
            if not line or "skill.auto_accept" not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(rec.get("ts") or "")
            parsed = _parse_audit_ts_utc(ts)
            if parsed is not None and parsed >= cutoff:
                return  # already auto-accepted recently

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
        header="Agent candidates; install only after explicit approval:",
        approval_line="Approve: `ai agents accept <id>`; reject: `ai agents reject <id>`.",
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
        header="Precall rule candidates; activate only after explicit approval:",
        approval_line="Approve: `ai precall accept <id>` then `ai precall activate <id>`; reject: `ai precall reject <id>`.",
        label_field="kind",
        desc_field=("evidence", "rationale"),
        source_short="cb-precall",
        accept_cmd_compact="`ai precall accept <id>`",
    )


def _federated_summary_context(root: Path, hook_name: str) -> str:
    if hook_name not in SKILL_RECOMMENDATION_HOOKS:
        return ""
    if _env_disabled("AI_FEDERATED_SUMMARY", default="0"):
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
        f"federated patterns({scanned} projects): {' | '.join(parts)}. "
        "Inspect with `ai federated summary`."
    )


def _satisfaction_summary_context(root: Path, hook_name: str) -> str:
    if hook_name not in SKILL_RECOMMENDATION_HOOKS:
        return ""
    if _env_disabled("AI_SATISFACTION_SUMMARY", default="0"):
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
        content = _read_hook_state_text(root, audit_file)
        if not content:
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
                    parsed = _parse_audit_ts_utc(ts)
                    if parsed is not None:
                        surfaced_records.append((parsed, pid))
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
            f"recommend satisfaction: {counts['surfaced']} surfaced, 0 acted{stale_suffix}{adaptive_suffix}. "
            "Inspect: `ai recommend skills|agents|precall`; opt out: AI_*_RECOMMENDATIONS=0."
        )
    sat_pct = int(round(100 * counts["accepted"] / total_acted))
    return (
        f"recommend satisfaction: {sat_pct}% accept ({counts['accepted']}/{total_acted} acted, "
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
    # Do not include the live count: it changes after every hook event and used to defeat
    # UserPromptSubmit's whole-context delta cache, re-injecting ~2 KiB every turn once
    # the threshold was crossed. The threshold is enough actionable information and stays
    # byte-stable until the next SessionStart.
    return (
        f"cb-scope: long session ({threshold}+ audit events) — "
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
            content = _read_hook_state_text(root, audit_file)
            if not content:
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
    host_agent = normalize_agent(payload)
    antigravity_first_invocation = (
        effective_hook == "PreInvocation"
        and str(payload.get("invocationNum", "")).strip() == "0"
    )
    if effective_hook == "UserPromptSubmit" or antigravity_first_invocation:
        # Continuation budgets are per USER REQUEST, not per long-lived host session. Before
        # this reset, one task could consume the cap/30-minute window and silently disable
        # the guard for every later request in the same session.
        sid = str(
            payload.get("session_id")
            or payload.get("sid")
            or payload.get("conversationId")
            or "default"
        )
        try:
            from .completion_guard import begin_request
            from .loop_continuation import reset_counter

            begin_request(root, sid)
            reset_counter(root, sid)
        except Exception:
            pass
    if effective_hook in {"SessionStart", "Stop", "SubagentStop", "StopFailure", "Interrupt"} and not (is_ci() or payload.get("dry") is True):
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
        # Completion proof ledger: metadata-only and lock-bounded. A successful relevant
        # check must follow the latest edit before Stop is allowed to claim completion.
        if effective_hook in {"PostToolUse", "PostToolUseFailure"}:
            try:
                from .completion_guard import observe_tool_event

                observe_tool_event(
                    root,
                    payload,
                    event_succeeded=effective_hook == "PostToolUse",
                )
            except Exception:
                pass
        # Spawn the AGENTS.md memory refresh EARLY and detached. Stop/SessionEnd are
        # the natural triggers; PostToolUse remains a fallback for interrupted turns and
        # older hosts. A cooldown in the helper
        # bounds frequency; the refresh is write-on-change and detached so it finishes
        # even if the host kills the parent hook.
        if effective_hook in {"Stop", "SessionEnd", "StopFailure", "Interrupt", "PostToolUse"}:
            _spawn_agents_md_refresh(root, agent=host_agent)
        # Turn-change snapshot: same trigger set as the AGENTS.md refresh. Detached;
        # git facts only, never part of the synchronous Stop decision.
        if effective_hook in {"Stop", "SessionEnd", "StopFailure", "Interrupt"}:
            _spawn_turn_report(root, host_agent)
            # Same reason, different cost centre: the transcript aggregation that feeds
            # prompt_growth's `baseline_tokens` is a multi-second scan, so it is refreshed
            # out-of-band and the hook only ever reads its TTL cache. Prompt growth is
            # explicit opt-in, so its detached transcript scan must be off too.
            if not _env_disabled("AI_PROMPT_GROWTH", default="0"):
                _spawn_tokens_cache_refresh(root)
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
            # Prompt growth: record this turn and let the deterministic loop grow the
            # project prompt. Background only (Stop hook), never blocks; fail-soft.
            # Explicit opt-in (default off): this writes derived rules and spends a
            # background transcript scan (_spawn_tokens_cache_refresh) on every turn it
            # runs, which is unwanted cost/noise unless a project has deliberately
            # turned the self-growth loop on.
            if not _env_disabled("AI_PROMPT_GROWTH", default="0"):
                try:
                    from . import prompt_growth

                    last_msg = payload.get("last_assistant_message")
                    output_chars = len(last_msg) if isinstance(last_msg, str) else 0
                    grew = prompt_growth.tick(root, output_chars=output_chars,
                                              agent=normalize_agent(payload))
                    # closed self-improvement loop (opt-in): periodically queue a cheap-judge
                    # review for the loopd pool. Enqueue only — no LLM here, never blocks.
                    if _env_enabled("AI_SELF_IMPROVE_AUTO", default="0"):
                        turns = int(grew.get("turns", 0)) if isinstance(grew, dict) else 0
                        if turns and turns % 25 == 0:
                            try:
                                from . import self_improve
                                self_improve.enqueue_review(root, tier="cheap")
                            except Exception:
                                pass
                except Exception:
                    pass
            # Optional inline pressure reaction. Bounded retention does not depend on
            # this path: detached sleep-time page-out always performs rotation/folding.
            # Default-off prevents an audit scan/fold from landing on the Stop hot path.
            if _env_enabled("AI_AUTO_PAGE_OUT", default="0"):
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
        # T6: spawn sleep-time idle jobs (memory page-out, audit fold, index refresh).
        #
        # Originally gated on Stop/SessionEnd only. Some agent hosts never emit
        # those events -- a real workspace went 12 days with an unexpired
        # `sleep-time.lock` and zero background maintenance while still emitting
        # SessionStart/UserPromptSubmit daily. Turn-START events are therefore a
        # fallback trigger, admitted only when the previous run is old enough that
        # this is genuinely idle-time catch-up rather than per-turn work. The
        # 600s spawn cooldown inside _spawn_sleep_time_jobs still applies.
        if effective_hook in SLEEP_TIME_HOOKS or (
            effective_hook in SLEEP_TIME_FALLBACK_HOOKS
            and _sleep_time_fallback_due(root)
        ):
            try:
                _spawn_sleep_time_jobs(root)
            except Exception:
                pass
        # Cross-machine memory auto-sync (git fetch/push) is intentionally NOT spawned
        # from any hook: the project's own contract forbids network I/O on the hooks/MCP
        # hot path, and a background process launched FROM the hook is still the hook
        # causing network I/O even when detached. Use the explicit `ai memory sync`
        # command instead (one-shot or --loop daemon). A lingering
        # memory_sync.enabled: true in .ai/config.yaml is a deprecated no-op, diagnosed
        # once per session by `ai doctor` rather than silently reactivating an automatic
        # spawn.
    target_ms = _target_ms_for(effective_hook)
    response = {
        "ok": True,
        "hook": effective_hook,
        "mode": mode,
        "persisted": persisted,
        "elapsed_ms": 0,
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
    # G9: Read-triggered walk-up directory context (opt-in via AI_DIR_CONTEXT). Surfaces nested
    # AGENTS.md/CLAUDE.md next to the file just read, once per session. Separate branch — does not
    # widen CONTEXT_INJECTION_HOOKS (those inject unconditionally; this is demand-driven on Read).
    if effective_hook == "PostToolUse":
        try:
            from .dir_context import directory_context_for_read
            dir_block = directory_context_for_read(root, payload)
        except Exception:
            dir_block = ""
        if dir_block:
            existing = response.get("hookSpecificOutput")
            if not isinstance(existing, dict):
                existing = {"hookEventName": effective_hook}
            prior_ctx = str(existing.get("additionalContext") or "")
            merged = f"{prior_ctx}\n\n{dir_block}".strip() if prior_ctx else dir_block
            existing["additionalContext"] = merged
            response["hookSpecificOutput"] = existing
            response["additionalContext"] = merged
            response["dir_context"] = True
    # G3: Stop-hook plan continuation (opt-in via AI_LOOP_CONTINUATION, bounded). Only when NOT
    # already blocking for security — a security block must never be downgraded to a continuation.
    if (
        effective_hook in _STOP_LIKE_HOOKS
        and host_agent != "kiro"
        and response.get("decision") != "block"
    ):
        try:
            from .loop_continuation import continuation_directive
            cont = continuation_directive(payload, root)
            if cont:
                response["decision"] = "block"
                response["reason"] = cont
                response["continuation"] = True
        except Exception:
            pass
    # Premature-stop guard: plan continuation only fires when an `ai plan` has unchecked
    # steps, and measured across the installed projects almost nobody keeps one (blurivo 1 of
    # 140 plans, navio 0 of 32, fluxwright 0). So the tree-evidence guard runs as the fallback
    # — it reads conflict markers, broken syntax, self-introduced TODOs and failed acceptance
    # instead of any self-report. Still second in line: an explicit plan names a better next
    # action than a derived signal ever can.
    if (
        effective_hook in _COMPLETION_GUARD_HOOKS
        and not (host_agent == "kiro" and effective_hook in _STOP_LIKE_HOOKS)
        and response.get("decision") != "block"
    ):
        try:
            from .completion_guard import guard_directive
            guard = guard_directive(
                payload,
                root,
                include_plan=effective_hook in _STOP_LIKE_HOOKS,
            )
            if guard:
                response["decision"] = "block"
                response["reason"] = guard
                response["completion_guard"] = True
        except Exception:
            pass
    if effective_hook in _STOP_LIKE_HOOKS and response.get("decision") != "block":
        try:
            from .completion_guard import _session_id, consume_degraded_notice
            from .loop_continuation import consume_limit_notice

            sid = _session_id(payload)
            notices = [consume_limit_notice(root, sid), consume_degraded_notice(root, sid)]
            notices = [notice for notice in notices if notice]
            if notices:
                response["completion_guard_notice"] = " ".join(notices)[:900]
        except Exception:
            pass
    if effective_hook == "TaskCompleted" and persisted:
        if response.get("decision") == "block":
            try:
                from .memory import append_audit

                append_audit(
                    root,
                    action="task.completion_blocked",
                    category="hook",
                    payload={"task_id": str(payload.get("task_id") or "")[:64]},
                )
            except Exception:
                pass
        else:
            _close_task_todo(root, payload)

    # Measure after every synchronous decision and lifecycle side effect. Measuring before
    # completion_guard used to under-report actual Stop/TaskCompleted latency.
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response["elapsed_ms"] = elapsed_ms
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
        if hook in {"UserPromptSubmit", "PostToolUse"} or hook in _STOP_LIKE_HOOKS:
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
    if hook in _STOP_LIKE_HOOKS:
        return {"continue": True}
    return {}


def antigravity_wire_output(response: dict[str, Any]) -> dict[str, Any]:
    """Project CB's internal decision to Antigravity 2.0 / CLI's native schema.

    Antigravity's Stop polarity is the inverse of Claude/Codex: only
    ``decision: \"continue\"`` prevents stopping; any other value allows it. Returning
    Claude's ``decision: \"block\"`` therefore silently DID THE OPPOSITE of what CB meant.
    The official contract also requires a decision on Stop, so ``{\"continue\": true}``
    was invalid rather than a clean allow.

    Current project wiring installs only PostToolUse and Stop for Antigravity. PreToolUse
    projection is still defined defensively for a future/version-gated installer.
    """
    hook = str(response.get("hook") or "")
    blocked = response.get("decision") == "block"
    hook_specific = response.get("hookSpecificOutput")
    hook_specific = hook_specific if isinstance(hook_specific, dict) else {}
    reason = str(
        response.get("reason")
        or hook_specific.get("permissionDecisionReason")
        or "Code Brain detected unfinished work."
    )
    if hook in _STOP_LIKE_HOOKS:
        if blocked:
            return {"decision": "continue", "reason": reason}
        return {"decision": "stop"}
    if hook == "PreToolUse":
        return {"decision": "deny" if blocked else "allow", **({"reason": reason} if blocked else {})}
    if hook == "PreInvocation":
        return {"injectSteps": []}
    # Antigravity's PostToolUse output contract is exactly an empty object. Codex-specific
    # hookSpecificOutput fields are invalid there and must never leak across host schemas.
    return {}


def kiro_wire_output(response: dict[str, Any]) -> str | None:
    """Project to Kiro's command-hook stdout contract.

    Kiro adds successful command stdout to model context verbatim; emitting Codex JSON
    therefore creates noisy context rather than a decision. Blocking PreToolUse and
    UserPromptSubmit is expressed by a non-zero process status (``hook_exit_code``), while
    Stop is observational in the current Kiro contract and cannot force continuation.
    """
    hook = str(response.get("hook") or "")
    specific = response.get("hookSpecificOutput")
    specific = specific if isinstance(specific, dict) else {}
    if hook in {"SessionStart", "UserPromptSubmit"}:
        context = specific.get("additionalContext") or response.get("additionalContext")
        return str(context) if context else None
    if hook == "Stop" and response.get("decision") == "block":
        reason = str(response.get("reason") or "").strip()
        return reason or None
    return None


def hook_wire_output(
    response: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any] | str | None:
    """Select the strict wire schema from the ORIGINAL host hook payload."""
    agent = normalize_agent(request_payload)
    if agent == "antigravity":
        return antigravity_wire_output(response)
    if agent == "kiro":
        return kiro_wire_output(response)
    out = codex_wire_output(response)
    # Claude's official universal `systemMessage` is user-visible without re-entering the model
    # loop. Keep it host-gated: other Claude-compatible runtimes may enforce a narrower schema.
    if agent == "claude" and response.get("completion_guard_notice"):
        out["systemMessage"] = str(response["completion_guard_notice"])
    return out


def hook_exit_code(response: dict[str, Any], request_payload: dict[str, Any]) -> int:
    """Return the host-native command status for decisions JSON cannot express."""
    if response.get("decision") != "block":
        return 0
    hook = str(response.get("hook") or "")
    agent = normalize_agent(request_payload)
    if agent == "claude" and hook in _QUALITY_GATE_HOOKS:
        return 2
    if agent == "kiro" and hook in {"PreToolUse", "UserPromptSubmit", "PreTaskExec"}:
        # Kiro documents exit 2 as the native blocking status for PreToolUse;
        # it is also non-zero for Prompt Submit/PreTaskExec's blocking contract.
        return 2
    return 0


def hook_stderr(response: dict[str, Any], request_payload: dict[str, Any]) -> str:
    """Feedback text for host contracts that block by non-zero command status."""
    if hook_exit_code(response, request_payload) == 0:
        return ""
    return str(response.get("reason") or "Blocked by Code Brain hook.")[:900]


LIFECYCLE_SNAPSHOT_HOOKS = {"PreCompact", "SessionEnd", "StopFailure", "Interrupt"}


def _handle_lifecycle_event(root: Path, hook_name: str, payload: dict[str, Any]) -> None:
    """Side-effect handler for PreCompact / SessionEnd / Notification / PermissionRequest.

    Runs after append_event so audit ordering matches the original event timestamp.
    Errors are swallowed by the caller — never break the hook hot path.
    """
    from .memory import append_audit

    if hook_name in {"StopFailure", "Interrupt"}:
        session_id = str(payload.get("session_id") or payload.get("sid") or "")
        agent = normalize_agent(payload)
        if hook_name == "StopFailure":
            reason = str(payload.get("error") or "unknown")[:64]
            details = str(payload.get("error_details") or "")[:200]
            action = "session.stop_failure"
            snapshot_reason = f"stop_failure_{reason}"
            audit_payload = {"error": reason, "error_details": details, "session_id": session_id}
        else:
            reason = str(payload.get("reason") or payload.get("interrupt_reason") or "unknown")[:64]
            action = "session.interrupt"
            snapshot_reason = f"interrupt_{reason}"
            audit_payload = {"reason": reason, "session_id": session_id}
        if session_id:
            try:
                from .session_resume import write_snapshot

                _auto_milestone_on_stale(root)
                write_snapshot(
                    root,
                    session_id=session_id,
                    agent=agent,
                    force=True,
                    reason=snapshot_reason,
                )
            except Exception:
                pass
        append_audit(root, action=action, category="memory", payload=audit_payload)
        return

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
        prev = str(payload.get("old_cwd") or payload.get("previous_cwd") or "")
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
        title = str(
            payload.get("task_subject") or payload.get("title") or payload.get("subject") or ""
        ).strip()
        if title:
            try:
                from .memory import append_todo
                append_todo(root, title=title[:200], source="task_hook")
            except Exception:
                pass
        return

    if hook_name == "TaskCompleted":
        append_audit(
            root,
            action="task.completion_requested",
            category="memory",
            payload={
                "task_id": str(payload.get("task_id") or "")[:64],
                "task_subject": str(payload.get("task_subject") or "")[:200],
            },
        )
        return

    if hook_name == "FileChanged":
        file_path = str(payload.get("file_path") or payload.get("filePath") or payload.get("path") or "")
        change_event = str(payload.get("event") or payload.get("changeType") or "")[:32]
        append_audit(
            root,
            action="file.changed",
            category="memory",
            payload={"file_path": file_path[:200], "event": change_event},
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


def _close_task_todo(root: Path, payload: dict[str, Any]) -> None:
    """Close a mirrored Claude task only after its completion gate allowed closure."""
    match = str(
        payload.get("task_subject") or payload.get("title") or payload.get("subject") or ""
    ).strip()
    if not match:
        return
    try:
        from .memory import close_todo

        close_todo(root, match=match[:200], status="done", reason="task_hook_verified")
    except Exception:
        pass

_FAILURE_MAX_SURFACE = 3   # bound the AS-OF block so it cannot push other sections off the budget cliff


def _failure_live_versions(root: Path) -> dict[str, str]:
    """Optional on-disk environment snapshot for the version-diff re-test. Fail-soft → {}."""
    path = root / ".ai" / "memory" / "env-versions.json"
    try:
        content = _read_hook_state_text(root, path, max_bytes=64_000)
        if not content:
            return {}
        import json as _json

        from .redact import redact_value

        data = _json.loads(content)
        if not isinstance(data, dict):
            return {}
        return {str(k)[:40]: str(redact_value(str(v)))[:60] for k, v in list(data.items())[:64]}
    except Exception:
        return {}


def _failure_retest_flag(entry: dict[str, Any], live: dict[str, str], today: str) -> str:
    """retest | fresh | unknown — read-time, deterministic, never a false 'still broken'."""
    versions = entry.get("observed_versions")
    if isinstance(versions, dict) and versions:
        if live:
            for k, v in versions.items():
                if str(live.get(str(k), "")) != str(v):
                    return "retest"  # a known version moved → escalate
            return "fresh"
        return "unknown"  # no live snapshot → soft reminder only
    retest_after = str(entry.get("retest_after") or "")
    if retest_after and today >= retest_after[:10]:
        return "retest"
    return "unknown"


def _render_failure_lines(entry: dict[str, Any], live: dict[str, str], today: str) -> list[str]:
    """≤2-line AS-OF block for a live failure. Never a bare prohibition; always re-testable."""
    when = str(entry.get("observed_at") or entry.get("decided_at") or "")[:10]
    head = str(entry.get("decision") or "")[:200]
    confirmed = " [confirmed]" if str(entry.get("status")) == "confirmed" else ""
    out = [f"  - [FAILURE as-of {when}]{confirmed} {head}"]
    versions = entry.get("observed_versions")
    vtail = ""
    if isinstance(versions, dict) and versions:
        vtail = " ".join(f"{k}={v}" for k, v in versions.items())[:120]
    env = str(entry.get("environment") or "")[:80]
    scope = (f"under: {vtail}" if vtail else "") + ((" " + env) if env else "")
    flag = _failure_retest_flag(entry, live, today)
    if flag == "retest":
        tail = "RE-TEST: a version may have changed since this observation — verify before relying; not a permanent rule"
    else:
        tail = "point-in-time observation; re-test before relying; not a permanent ban"
    out.append(f"    {scope.strip() + ' — ' if scope.strip() else ''}{tail}")
    return out


def _lessons_context(root: Path, hook_name: str) -> str:
    """Inject the few strongest distilled lessons (experience replay). Bounded; recall MCP for more."""
    if hook_name not in SKILL_RECOMMENDATION_HOOKS or _env_disabled("AI_LESSONS_INJECT"):
        return ""
    try:
        from .lessons import score_lessons

        items = score_lessons(root, include_stale=False).get("items", [])
    except Exception:
        return ""
    rendered: list[tuple[dict[str, Any], str]] = []
    for item in items:
        if float(item.get("confidence", 0) or 0) < 0.5:
            continue
        # Global prompt injection is reserved for an actionable cause/fix. Bare
        # commands and aggregate counters (for example an acceptance-command count)
        # remain available through query-specific recall but add no durable guidance.
        body = str(item.get("fix") or item.get("cause") or item.get("failure") or "").strip()
        words = body.split()
        looks_like_counter = bool(
            words
            and words[0].isdigit()
            and words[-1].lower() in {"commands", "cases", "tests", "failures", "items"}
        )
        if len(body) < 20 or len(words) < 3 or looks_like_counter:
            continue
        rendered.append((item, body[:120]))
        if len(rendered) == 3:
            break
    if not rendered:
        return ""
    lines = ["Lessons (distilled from past runs — apply; call lessons_recall for query-specific recall):"]
    for it, body in rendered:
        conf = it.get("confidence")
        kind = str(it.get("kind") or "")
        lines.append(f"  - ({conf}) {body}" + (f" [{kind}]" if kind else ""))
    return "\n".join(lines)


def _learned_prompt_context(root: Path) -> str:
    """Inject auto-grown project rules (prompt growth). Empty until the loop has grown one."""
    if _env_disabled("AI_PROMPT_GROWTH", default="0"):
        return ""
    try:
        from . import prompt_growth

        text = prompt_growth.learned_prompt_text(root)
    except Exception:
        return ""
    return text or ""


def _session_harness_context(root: Path) -> str:
    """Return the always-on SessionStart harness hint without repo-wide analysis.

    Detailed mode/source/test/dirty analysis remains available through
    autonomous_harness.analyze/context_line and explicit harness directives.
    SessionStart only needs the invariant operating rule plus active-plan
    progress; counting files and spawning `git status` on every new session was
    pure hint-generation overhead.
    """
    base = (
        "cb-harness: target=95%. "
        "For build/harden: scope, own paths, verify, iterate until done/blocker."
    )
    plans_root = root / ".ai" / "memory" / "plans"
    if not plans_root.is_dir():
        return base
    try:
        from . import plan_state

        active = plan_state.active_summary(root)
    except Exception:
        return base
    if not active:
        return base
    next_label = active.get("next_label")
    tail = f" next: {str(next_label)[:80]}" if next_label else ""
    return (
        base
        + f" | plan {active['plan_id']}: {active['completed']}/{active['total']} done,"
        + f" {active['remaining']} left.{tail}"
    )


def static_rule_sections() -> list[str]:
    """The Response/Search/Read behavioural rules as a list of sections, in the exact
    text ``build_context`` has always injected at SessionStart. Factored out so
    ``ai_core.agents_md.render_block`` can embed the identical text inside the managed
    AGENTS.md block's static sub-section (see agents_md.py): once that block is current,
    Codex's own SessionStart hook skips re-injecting these lines (see build_context) rather
    than only skipping the durable memory body — a single source of truth for the text
    means the two paths can never drift out of sync with each other.
    """
    if _env_enabled("AI_ROUTING_HINT_COMPACT"):
        routing = "Search: MCP `code_query`/`context_pack` before grep."
    else:
        routing = (
            "Search: MCP `code_query`/`context_pack` first; graph tools for call paths; "
            "`ai exec run -- rg/grep` only for exact fallback."
        )
    return [
        "Response: match the user's language; self-output <=10 words; answers concise by default; broad changes end with a brief key-point summary; expand for explicit detail or severe risk. No next-step outro; keep working.",
        routing,
        "Read: before editing existing files, use exact target slices with "
        "`code_read_hashline` or `.ai/bin/ai code read-hashline PATH --start START --end END`.",
    ]


def _build_volatile_sections(hook_name: str, payload: dict[str, Any], root: Path) -> list[str]:
    """Sections that must NEVER be mirrored into the AGENTS.md managed block or judged by
    its durable fingerprint: turn-change nudges, the git/remote-derived staleness banner,
    and the codebase-map summary. All three depend on state (current branch, dirty tree,
    remote-ahead, tracked-file layout) that changes far more often than the file-backed
    memory below, and re-deriving/caching them per turn is exactly the cheap, per-turn
    "volatile delta" the hook should keep doing — mirroring them into a once-per-refresh
    file and skipping re-injection when "current" would silently show a stale branch/dirty
    state whenever nothing else in memory happened to change that turn. Always appended by
    build_context regardless of AGENTS.md currentness (see build_context).
    """
    sections: list[str] = []
    if hook_name == "UserPromptSubmit":
        try:
            from .turn_report import nudge_line

            turn_line = nudge_line(root)
        except Exception:
            turn_line = ""
        if turn_line:
            sections.append(turn_line)
    staleness = _memory_staleness_context(root, hook_name)
    if staleness:
        sections.append(staleness)
    if hook_name == "SessionStart":
        map_context = _codebase_map_summary_context(root)
        if map_context:
            sections.append(map_context)
    return sections


def _build_auxiliary_sections(hook_name: str, payload: dict[str, Any], root: Path) -> list[str]:
    """Runtime-only recommendations and telemetry.

    These sections are intentionally not mirrored into ``AGENTS.md``: their inputs are
    caches, audit streams, global catalogs, or the live code index rather than the bounded
    durable-memory files covered by ``agents_md.fingerprint``. Codex still receives them
    when the mirrored durable block is current; otherwise a current AGENTS.md would
    accidentally disable explicitly opted-in recommendations and telemetry.
    """
    sections: list[str] = []
    if hook_name == "SessionStart":
        try:
            from .episodic_runtime import read_hook_context

            episodic = read_hook_context(root)
        except Exception:
            episodic = ""
        if episodic:
            sections.append(episodic)
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
    # Explicit opt-in (default off): these are operational TELEMETRY (hot/warm/cold
    # audit-event counts, top-callee call counts) — not semantic decisions/todos/
    # evidence the model needs to act correctly.
    if hook_name in SKILL_RECOMMENDATION_HOOKS and not _env_disabled("AI_MEMORY_TIER_SUMMARY", default="0"):
        memory_tier = _memory_tier_summary_context(root)
        if memory_tier:
            sections.append(memory_tier)
    if hook_name in SKILL_RECOMMENDATION_HOOKS and not _env_disabled("AI_CODEGRAPH_SUMMARY", default="0"):
        codegraph_summary = _codegraph_hotspot_context(root)
        if codegraph_summary:
            sections.append(codegraph_summary)
    return sections


def _build_dynamic_sections(
    hook_name: str,
    payload: dict[str, Any],
    root: Path,
    *,
    include_auxiliary: bool = True,
) -> list[str]:
    """DURABLE, file-backed dynamic sections: session-harness plan progress (reads
    ``.ai/memory/plans``), resume snapshot, decisions, failures, todos, session tail,
    learned rules, and lessons — every mirrored section's input is covered by
    ``ai_core.agents_md.FINGERPRINT_DEPENDENCIES``.

    Runtime-only recommendations and telemetry are appended by
    ``_build_auxiliary_sections`` when ``include_auxiliary`` is true. The AGENTS.md mirror
    passes false so its fingerprint never claims freshness for inputs it does not track.

    Deliberately excludes: the header line and the Response/Search/Read static rules
    (live ONLY in build_context's top-level static block, never here — same reasoning as
    below, now folded into the managed block's static sub-section, see agents_md.py); AND
    the git/remote/codebase-map VOLATILE sections (see ``_build_volatile_sections`` — those
    must never be judged by the durable fingerprint, since branch/dirty state changes far
    more often than memory files and silently going stale here would hide real drift).

    This is also exactly the body ai_core.agents_md.render_block() mirrors into the
    git-ignored root AGENTS.md for Antigravity (which has no hook-injection path at all):
    keeping volatile/static content out of this function is what keeps that mirrored file
    from ever duplicating rules or state that (for hosts with a hook) are delivered fresh
    every turn by build_context's own static+volatile sections, and what makes a
    fingerprint of its declared durable dependency files (see ai_core.agents_md.fingerprint)
    meaningful as a currentness signal across hosts/turns.
    """
    sections: list[str] = []
    try:
        from .memory import read_decisions_for_surface

        plain_decisions, live_failures = read_decisions_for_surface(root, limit=DECISIONS_TAIL)
    except Exception:
        plain_decisions, live_failures = (
            _read_jsonl_tail(root / ".ai" / "memory" / "decisions.jsonl", DECISIONS_TAIL), [])
    todos = _read_jsonl_open_todos(root / ".ai" / "memory" / "todos.jsonl", TODOS_LIMIT)
    session_tail = _read_text_tail(root / ".ai" / "memory" / "session-current.md", SESSION_TAIL_LINES)
    current_decision_texts = {
        str(entry.get("decision") or entry.get("summary") or entry.get("text") or "")[:160]
        for entry in plain_decisions
    }
    current_todo_texts = {
        str(entry.get("title") or entry.get("text") or entry.get("summary") or "")[:160]
        for entry in todos
    }
    current_session_lines = {line.strip() for line in session_tail.splitlines() if line.strip()}
    if hook_name == "SessionStart":
        sections.append(_session_harness_context(root))
    if hook_name == "UserPromptSubmit":
        try:
            from .autonomous_harness import directive as _harness_directive, requested as _harness_requested
            if _harness_requested(payload):
                sections.append(_harness_directive(root, explicit=True, request=payload))
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
            lines = [f"resume session_id={prior.get('session_id')} written_at={prior.get('written_at')}:"]
            # P1: lead with the intent-carrying handoff so a resuming session (esp. on
            # the other machine) sees "what we were doing / what to do next" first.
            handoff = prior.get("handoff") if isinstance(prior.get("handoff"), dict) else None
            if handoff:
                if handoff.get("goal"):
                    lines.append(f"  goal: {str(handoff['goal'])[:200]}")
                if handoff.get("next_step"):
                    lines.append(f"  resume task: {str(handoff['next_step'])[:200]}")
                for step in (handoff.get("plan") or [])[:6]:
                    lines.append(f"  plan: {str(step)[:160]}")
                for q in (handoff.get("open_questions") or [])[:4]:
                    lines.append(f"  question: {str(q)[:160]}")
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
                # best-effort: a snapshot can hold a since-retired failure; drop retired,
                # and frame surviving failures as re-testable, never as permanent bans.
                if entry.get("kind") == "failure" and str(entry.get("status", "observed")) in {"stale", "refuted"}:
                    continue
                text = str(entry.get("decision") or entry.get("summary") or entry.get("text") or "")[:160]
                if not text or text in current_decision_texts:
                    continue
                if entry.get("kind") == "failure":
                    lines.append(f"  failure (re-testable, not a permanent ban): {text}")
                else:
                    lines.append(f"  decision: {text}")
            for entry in (prior.get("todos_open") or [])[-3:]:
                text = str(entry.get("title") or entry.get("text") or entry.get("summary") or "")[:160]
                if text and text not in current_todo_texts:
                    lines.append(f"  open todo: {text}")
            actions = prior.get("audit_tail_actions") or []
            if actions:
                lines.append(f"  recent actions: {', '.join(str(a) for a in actions[-5:])}")
            prior_tail = str(prior.get("session_tail") or "")
            tail_lines = [
                line
                for line in prior_tail.splitlines()
                if line.strip() and line.strip() not in current_session_lines
            ][-PRIOR_SESSION_TAIL_LINES:]
            if tail_lines:
                lines.append("  session tail:")
                for line in tail_lines:
                    lines.append(f"    {line[:220]}")
            sections.append("\n".join(lines))
    if plain_decisions:
        lines = ["decisions:"]
        for entry in plain_decisions:
            ts = str(entry.get("decided_at") or entry.get("timestamp") or "")[:19]
            text = str(entry.get("decision") or entry.get("summary") or entry.get("text") or "")[:160]
            if not text:
                continue  # same guard as the resume-tail renderer: never inject an empty bullet
            lines.append(f"  - [{ts}] {text}" if ts else f"  - {text}")
        sections.append("\n".join(lines))
    if live_failures:
        live_versions = _failure_live_versions(root)
        today = now_iso()[:10]
        flines = ["failures (retest; not permanent):"]
        for entry in live_failures[:_FAILURE_MAX_SURFACE]:
            flines.extend(_render_failure_lines(entry, live_versions, today))
        extra = len(live_failures) - _FAILURE_MAX_SURFACE
        if extra > 0:
            flines.append(f"  (+{extra} older re-testable findings — query memory for detail)")
        sections.append("\n".join(flines))
    if todos:
        lines = ["todos:"]
        for entry in todos:
            text = str(entry.get("title") or entry.get("text") or entry.get("summary") or "")[:160]
            owner = str(entry.get("owner") or "")
            lines.append(f"  - {text} [{owner}]" if owner else f"  - {text}")
        sections.append("\n".join(lines))
    if include_auxiliary:
        sections.extend(_build_auxiliary_sections(hook_name, payload, root))
    if session_tail:
        sections.append("session tail:\n" + session_tail)
    learned = _learned_prompt_context(root)
    if learned:
        sections.append(learned)
    lessons = _lessons_context(root, hook_name)
    if lessons:
        sections.append(lessons)
    # T37 — cloudflare remote_memory removed (.ai/ git sync handles cross-device).
    return sections


def build_context(hook_name: str, payload: dict[str, Any], *, root: Path | None = None) -> str:
    agent = normalize_agent(payload)
    writes = "off" if is_ci() or payload.get("dry") is True else "worker-local"
    header = f"Code Brain fast_path: hook={hook_name}, agent={agent}, network=off, writes={writes}."
    if hook_name not in INJECTION_HOOKS or root is None:
        return ""
    sections = [header]
    # Currentness check, Codex + SessionStart only — done ONCE, BEFORE rendering either
    # the static rules or the durable dynamic sections, so a current file skips that work
    # rather than rendering it only to discard it. Runtime-only auxiliary sections remain
    # live and are evaluated below. is_current() is a bounded
    # stat()-only signature over a declared durable-memory-file list (no git subprocess,
    # no body re-render — see ai_core.agents_md module docstring for why a hash of the
    # *regenerated* body, or any git-derived input, was unsafe/slow/worktree-fragile).
    #
    # Antigravity has no SessionStart/UserPromptSubmit hook at all (confirmed: its wire
    # projection never emits additionalContext for those events), so this branch never
    # actually applies to it in practice — its only path to the static+durable body is the
    # mirrored root AGENTS.md file it auto-loads, written by ai_core.agents_md.refresh().
    #
    # Claude Code auto-loads only CLAUDE.md, never AGENTS.md, so it has zero exposure to
    # that mirrored file — it must always get the full static+durable body here,
    # unconditionally, at SessionStart (never repeated at UserPromptSubmit/SubagentStart).
    #
    # Codex CLI DOES auto-load root AGENTS.md. If some agent's turn (its own, or
    # Antigravity's, or a stale one from before this repo existed) already wrote a managed
    # block there whose embedded fingerprint matches the CURRENT durable-memory state
    # (decisions/todos/session-current/resume-snapshots/plans/learned-prompt/lessons/
    # .ai/config.yaml), Codex already has the static rules AND the durable dynamic body via
    # that auto-loaded file — re-sending either via additionalContext would duplicate it.
    # Only when the file is missing, unmanaged, or the fingerprint is stale does Codex fall
    # back to the full static+durable body here. VOLATILE sections (git branch/dirty state,
    # codebase-map, turn nudges — see _build_volatile_sections) are NEVER part of this gate:
    # they are appended unconditionally below regardless of currentness, since that state
    # changes far more often than memory files and going stale here would hide real drift.
    skip_static_and_durable = False
    # Current Codex app hooks may omit both an agent field and CODEX_* environment
    # variables. Treat that otherwise-unidentifiable SessionStart as Codex only when
    # a current managed AGENTS.md block proves the same durable body is already
    # available. Known hosts retain their explicit behavior.
    if agent in {"codex", "unknown"} and hook_name == "SessionStart":
        try:
            from . import agents_md as _agents_md

            skip_static_and_durable = _agents_md.is_current(root)
        except Exception:
            skip_static_and_durable = False  # fail-soft: keep the full body on any error
    if skip_static_and_durable:
        sections.append(
            "cb-agents-md: static rules + durable memory (decisions/todos/resume/"
            "session-tail/learned-prompt/lessons) are current in this repo's "
            "auto-loaded AGENTS.md — not repeated here."
        )
        sections.extend(_build_auxiliary_sections(hook_name, payload, root))
    else:
        # Static behavioural rules (Response/Search/Read). Only SessionStart, never
        # repeated at UserPromptSubmit/SubagentStart (no global repetition).
        if hook_name == "SessionStart":
            sections.extend(static_rule_sections())
        sections.extend(_build_dynamic_sections(hook_name, payload, root))
    # Volatile sections are appended unconditionally, regardless of AGENTS.md currentness
    # (see the comment block above): git-derived staleness/codebase-map state and turn
    # nudges must never be skipped just because durable memory happens to be unchanged.
    sections.extend(_build_volatile_sections(hook_name, payload, root))
    return _fit_sections(sections, _max_injection_bytes_for(hook_name))


# Sections that must survive the budget even when earlier sections are large. These are
# behavioural DIRECTIVES (how to answer, what rules were learned); the sections above them
# are evidence (decisions/todos/history) that degrades gracefully when shortened. Naive
# tail truncation dropped these entirely: measured 57% of the composed context discarded on
# code-brain, 76% on navio, and on every project the auto-grown `learned_prompt` rules and
# `session tail` were cut off completely — i.e. prompt_growth was writing rules that never
# reached the model.
_PROTECTED_SECTION_PREFIXES = (
    "Code Brain fast_path:",
    "Response:",
    "Search:",
    "Read:",
    "cb-turn:",
    "cb-stale:",
    "cb-life:",
    # prompt_growth.LEARNED_HEADER, inlined to keep this module import-cycle free and
    # usable at import time. Guarded by test_hook_context_budget.
    "# Learned project rules (auto-grown by Code Brain; do not edit by hand)",
)
_ELLIPSIS = "..."


def _is_protected_section(section: str) -> bool:
    return section.startswith(_PROTECTED_SECTION_PREFIXES)


def _clip_section(section: str, budget: int) -> str:
    """Shorten one section to `budget` bytes on a line boundary when possible."""
    if budget <= len(_ELLIPSIS):
        return ""
    encoded = section.encode("utf-8")
    if len(encoded) <= budget:
        return section
    head = encoded[: budget - len(_ELLIPSIS)].decode("utf-8", errors="ignore")
    # Prefer cutting at the last newline so a section never ends mid-entry.
    newline = head.rfind("\n")
    if newline > len(section) // 4:
        head = head[:newline]
    return head.rstrip() + _ELLIPSIS


def _fit_sections(sections: list[str], max_bytes: int) -> str:
    """Compose sections within `max_bytes`, keeping directives and shrinking evidence.

    Replaces a single tail truncation of the joined string. Protected sections are
    reserved first; the remaining budget is spent on the evidence sections in order, and
    the first evidence section that does not fit is clipped instead of dropping every
    section after it. Deterministic: same input, same output.
    """
    sections = [s for s in sections if s]
    if not sections:
        return ""
    joined = "\n\n".join(sections)
    if len(joined.encode("utf-8")) <= max_bytes:
        return joined

    sep = 2  # "\n\n" between sections
    protected_idx = [i for i, sec in enumerate(sections) if _is_protected_section(sec)]
    protected_bytes = sum(len(sections[i].encode("utf-8")) + sep for i in protected_idx)
    # A pathological protected set (huge learned rules) must not starve everything else,
    # so protection is honoured only while it fits in most of the budget.
    if protected_bytes > max_bytes:
        protected_idx = []
        protected_bytes = 0

    keep: dict[int, str] = {i: sections[i] for i in protected_idx}
    remaining = max_bytes - protected_bytes
    for i, sec in enumerate(sections):
        if i in keep:
            continue
        need = len(sec.encode("utf-8")) + sep
        if need <= remaining:
            keep[i] = sec
            remaining -= need
            continue
        clipped = _clip_section(sec, max(0, remaining - sep))
        if clipped:
            keep[i] = clipped
            remaining = 0
        break

    composed = "\n\n".join(keep[i] for i in sorted(keep))
    encoded = composed.encode("utf-8")
    if len(encoded) > max_bytes:  # belt-and-braces; must never exceed the host budget
        composed = encoded[: max_bytes - len(_ELLIPSIS)].decode("utf-8", errors="ignore") + _ELLIPSIS
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
            # an already-fetched upstream ref only (no fetch here). Refresh refs with an
            # explicit user-run `git fetch` or `ai memory sync`; hooks never cause network.
            behind = remote_sync_banner(root)
            if behind:
                banners.append(behind)
        except Exception:
            return ""
        # P4: peer heartbeat summary from an explicitly run memory sync process.
        # Local file reads only; absent for single-machine users.
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
    bits = [f"{marker} auto capture: git advanced without agent note"]
    if count:
        subject = str(commits[0].get("subject") or "") if commits else ""
        more = f"+{count - 1}" if count > 1 else ""
        bits.append(f"commits={count}{more} latest:{subject[:70]}")
    if dirty:
        bits.append(f"dirty={dirty}")
    text = " · ".join(bits) + ". Use git log/status as source of truth."
    try:
        from .memory import append_session_note

        append_session_note(root, text=text)
        return True
    except Exception:
        return False


def _hot_cache_line(root: Path) -> str:
    """Build a compact ranked HOT line from the precomputed page-in cache.

    Read-only and fail-soft: returns "" when the cache is missing, stale, or
    empty so SessionStart degrades to the live classify() histogram. Bounded by
    the cache's own byte budget plus a hard cap here, so it never grows the
    injection beyond the histogram it replaces.
    """
    # Opt-in (AI_MEMORY_HOT_LINE=1). The page-in cache is always built by the
    # sleep-time path, but injecting its ranked line is OFF by default until the
    # refs carry readable titles and the line measures smaller than the cb-mem
    # histogram it would replace — otherwise it both grows tokens and loses info.
    # cb-simplify: histogram-only by default; revisit when refs are human-readable
    # and the ranked line is provably shorter than the histogram.
    import os as _os
    if _os.environ.get("AI_MEMORY_HOT_LINE", "").strip().lower() not in ("1", "true", "on", "yes"):
        return ""
    try:
        from .memory_hot import read_hot_cache
        from .memory_tier import _env_float as _ef

        max_age = _ef("AI_MEMORY_HOT_CACHE_MAX_AGE", 1800.0)
        cache = read_hot_cache(root, max_age_seconds=max_age if max_age > 0 else None)
        if not cache:
            return ""
        items = cache.get("items") or []
        if not items:
            return ""
        parts: list[str] = []
        for it in items[:8]:
            kind = str(it.get("kind") or "")[:12]
            ref = str(it.get("ref") or "")[:48]
            if ref:
                parts.append(f"{kind}:{ref}")
        if not parts:
            return ""
        return f"cb-hot({len(items)}): " + " · ".join(parts)
    except Exception:
        return ""


def _memory_tier_summary_context(root: Path) -> str:
    deps = [
        root / ".ai" / "memory" / "audit-index.jsonl",
        root / ".ai" / "memory" / "todos.jsonl",
        root / ".ai" / "memory" / "decisions.jsonl",
        root / ".ai" / "memory" / "session-current.md",
        root / ".ai" / "cache" / "memory-hot.json",
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
            # Opt-in ranked HOT line from the page-in cache (T30 step C);
            # default keeps the cb-mem histogram. Read-only; "" when off/missing.
            ranked = _hot_cache_line(root)
            if ranked:
                sline = ranked
            else:
                sline = f"cb-mem: hot={hot} warm={warm} cold={cold}"
            sline += f" | session={int(pres['session_md_ratio']*100)}%"
            if pres.get("page_out_recommended"):
                sline += " ⚠page-out"
            return sline
        except Exception:
            return ""

    return _cached_hook_summary(root, cache_name="memory_tier_hot", deps=deps, compute=compute)


def _codegraph_hotspot_context(root: Path) -> str:
    """Cache the top-callee teaser until the code index changes.

    The full codegraph/search import graph is useful on an index rebuild, but
    importing and querying it on every SessionStart was pure repeated work.
    The SQLite DB and WAL mtimes are authoritative invalidation dependencies.
    """
    db = root / ".ai" / "cache" / "code.sqlite"
    try:
        db_state = db.lstat()
    except OSError:
        return ""
    import stat as _stat

    if not _stat.S_ISREG(db_state.st_mode) or _stat.S_ISLNK(db_state.st_mode):
        return ""
    generation = root / ".ai" / "cache" / "code-index-generation"
    if not generation.exists() and db.exists():
        try:
            atomic_write_private_text(
                generation,
                str(db_state.st_mtime_ns) + "\n",
                root=root,
            )
        except OSError:
            pass
    deps = [generation]

    def compute() -> str:
        try:
            from .codegraph import hotspot_callees

            hot = hotspot_callees(root, limit=3)
            entries = hot.get("hotspots") or []
            if not entries:
                return ""
            top = ", ".join(f"{item['callee']}({item['calls']})" for item in entries)
            return f"cb-graph: top callees — {top}. MCP: code_graph_callers/callees/symbol/hotspots."
        except Exception:
            return ""

    return _cached_hook_summary(
        root,
        cache_name="codegraph_hotspots",
        deps=deps,
        compute=compute,
    )


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
