"""Worker launch wrapper (PRD §10.2) — bring up warm, isolated CLI workers in tmux.

The wrapper is the truth owner (PRD §3.3): it resolves a per-profile isolated env (HOME/XDG
paths only — NEVER secrets), opens a tmux window running the agent CLI under that env, injects
the boot prompt, and records the pane in the registry. Codex/AGY get separate profiles so their
auth caches live in distinct HOMEs and never collide; the user logs in once per profile.

Building the launch plan is pure and testable (build_launch_plan). Executing it spawns a real
CLI, so launch() supports dry_run and refuses unknown agents / non-fixed commands. stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import worker_profiles as wp
from . import worker_registry as wr
from .memory import append_audit
from .tmux_adapter import TmuxAdapterBase, get_adapter

# Fixed agent → CLI binary. Only these may be launched (no arbitrary command strings).
AGENT_COMMANDS = {"codex": "codex", "claude": "claude", "agy": "agy"}
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BOOT = (
    "You are Code Brain worker {wid}. Do not poll the queue yourself. Wait for explicit task "
    "injection from code-brain-loopd. For any assigned request, claim/heartbeat/result/complete "
    "using the provided commands. Respect approval gates for secrets/auth/billing/prod/destructive."
)


def session_name(project_root: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", Path(project_root).name)[:48] or "repo"
    return f"cb-{slug}"


def build_launch_plan(root: Path, *, worker_id: str, agent: str, profile: str,
                      session: str, window: str, inherit_auth: bool = False) -> dict[str, Any]:
    """Pure: resolve the env + the exact (validated) tmux launch for a worker.

    inherit_auth=True does NOT override HOME/XDG — the worker uses the inherited (default-login)
    environment. Use it for a single already-logged-in account; use a profile (isolated HOME) for
    multiple accounts of the same agent so their auth caches never collide.
    """
    agent = str(agent).strip().lower()
    if agent not in AGENT_COMMANDS:
        return {"ok": False, "reason": f"unknown agent: {agent}"}
    if not _SESSION_RE.fullmatch(session):
        return {"ok": False, "reason": "invalid session name"}
    env: dict[str, str] = {} if inherit_auth else wp.resolve_profile_env(root, profile)
    env["CODE_BRAIN_PROJECT_ROOT"] = str(root)
    env["CODE_BRAIN_WORKER_ID"] = str(worker_id)[:64]
    env["CODE_BRAIN_HEARTBEAT"] = str(wr.heartbeat_path(root, worker_id))
    return {"ok": True, "worker_id": worker_id, "agent": agent, "profile": profile,
            "session": session, "window": window, "command": AGENT_COMMANDS[agent], "env": env}


def launch_worker(root: Path, *, worker_id: str, agent: str, profile: str,
                  session: str | None = None, window: str | None = None,
                  adapter: TmuxAdapterBase | None = None, dry_run: bool = False,
                  inherit_auth: bool = False) -> dict[str, Any]:
    session = session or session_name(root)
    window = window or worker_id
    plan = build_launch_plan(root, worker_id=worker_id, agent=agent, profile=profile,
                             session=session, window=window, inherit_auth=inherit_auth)
    if not plan.get("ok"):
        return plan
    if not inherit_auth:
        wp.ensure_profile_dirs(root, profile)
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}
    adapter = adapter or get_adapter()
    pane = adapter.new_window(session, window, plan["env"], plan["command"])  # type: ignore[attr-defined]
    if not pane:
        return {"ok": False, "reason": "tmux launch failed", "plan": plan}
    wr.register_worker(root, worker_id=worker_id, agent=agent, profile=profile,
                       project_root=str(root), cwd=str(root), pane_id=pane,
                       session=session, window=window, state="booting")
    adapter.inject(pane, _BOOT.format(wid=worker_id))
    wr.set_state(root, worker_id=worker_id, state="idle")
    wr.write_heartbeat(root, worker_id=worker_id, state="idle", pane_id=pane)
    append_audit(root, action="worker.launch", category="loopd",
                 payload={"worker_id": worker_id, "agent": agent, "profile": profile, "pane_id": pane})
    return {"ok": True, "worker_id": worker_id, "pane_id": pane, "session": session}


def launch_pool(root: Path, *, adapter: TmuxAdapterBase | None = None,
                dry_run: bool = False) -> dict[str, Any]:
    """Launch a worker for every registered profile that is not already up (idempotent)."""
    session = session_name(root)
    existing = {w["worker_id"] for w in wr.list_workers(root) if w.get("state") not in ("stopped", "lost")}
    results: list[dict[str, Any]] = []
    for prof in wp.list_profiles(root):
        wid = str(prof.get("worker_id") or f"{prof.get('agent')}-{prof.get('profile')}")
        if wid in existing:
            results.append({"worker_id": wid, "skipped": "already up"})
            continue
        results.append(launch_worker(root, worker_id=wid, agent=str(prof.get("agent")),
                                     profile=str(prof.get("profile")), session=session,
                                     adapter=adapter, dry_run=dry_run))
    return {"ok": True, "session": session, "dry_run": dry_run, "launched": results}
