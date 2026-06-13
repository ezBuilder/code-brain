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

import shutil

# Fixed agent → CLI binary. Only these may be launched (no arbitrary command strings).
AGENT_COMMANDS = {"codex": "codex", "claude": "claude", "agy": "agy"}


def tmux_available() -> bool:
    return bool(shutil.which("tmux"))


def agent_available(agent: str) -> bool:
    """True iff this agent's CLI binary is installed on PATH (so a worker can actually run)."""
    binary = AGENT_COMMANDS.get(str(agent).strip().lower())
    return bool(binary and shutil.which(binary))


def available_agents() -> list[str]:
    return [a for a in AGENT_COMMANDS if agent_available(a)]


def capabilities() -> dict[str, Any]:
    """What the pool can actually run here — auto-detected, no config needed."""
    return {
        "ok": True,
        "tmux": tmux_available(),
        "agents": {a: agent_available(a) for a in AGENT_COMMANDS},
        "available_agents": available_agents(),
    }
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
                      session: str, window: str, inherit_auth: bool = False,
                      autonomous: bool = False, tier: str | None = None) -> dict[str, Any]:
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
    import shlex

    from . import worker_models as wm
    model = wm.resolve_model(root, agent, tier=tier)
    parts = [AGENT_COMMANDS[agent], *model.get("flags", [])]
    if autonomous:
        parts += wm.autonomy_flags(agent)  # opt-in: skip prompts; loopd dispatch-gate is the boundary
    # flags come from operator config / safe defaults only (never request input). shlex-quote so a
    # spaced/parenthesized model name (e.g. agy "Gemini 3.1 Pro (High)") is one safe argv element.
    command = " ".join(shlex.quote(part) for part in parts).strip()
    return {"ok": True, "worker_id": worker_id, "agent": agent, "profile": profile, "autonomous": autonomous,
            "session": session, "window": window, "command": command, "model": model, "env": env}


def launch_worker(root: Path, *, worker_id: str, agent: str, profile: str,
                  session: str | None = None, window: str | None = None,
                  adapter: TmuxAdapterBase | None = None, dry_run: bool = False,
                  inherit_auth: bool = False, autonomous: bool = False,
                  tier: str | None = None) -> dict[str, Any]:
    session = session or session_name(root)
    window = window or worker_id
    plan = build_launch_plan(root, worker_id=worker_id, agent=agent, profile=profile,
                             session=session, window=window, inherit_auth=inherit_auth,
                             autonomous=autonomous, tier=tier)
    if not plan.get("ok"):
        return plan
    if dry_run:
        plan["agent_installed"] = agent_available(agent)
        plan["tmux"] = tmux_available()
        return {"ok": True, "dry_run": True, "plan": plan}
    # auto-detect: if this agent's CLI (or tmux) isn't installed, skip gracefully — never crash.
    if not tmux_available():
        return {"ok": False, "skipped": True, "reason": "tmux not installed", "worker_id": worker_id}
    if not agent_available(agent):
        return {"ok": False, "skipped": True, "reason": f"{agent} CLI not installed", "worker_id": worker_id}
    if not inherit_auth:
        wp.ensure_profile_dirs(root, profile)
    adapter = adapter or get_adapter()
    pane = adapter.new_window(session, window, plan["env"], plan["command"])  # type: ignore[attr-defined]
    if not pane:
        return {"ok": False, "reason": "tmux launch failed", "plan": plan}
    dismiss_onboarding(adapter, pane, agent)  # clear first-run trust gate so boot isn't swallowed
    wr.register_worker(root, worker_id=worker_id, agent=agent, profile=profile,
                       project_root=str(root), cwd=str(root), pane_id=pane,
                       session=session, window=window, state="booting",
                       model={**plan.get("model", {}), "command": plan["command"]})
    adapter.inject(pane, _BOOT.format(wid=worker_id))
    wr.set_state(root, worker_id=worker_id, state="idle")
    wr.write_heartbeat(root, worker_id=worker_id, state="idle", pane_id=pane)
    append_audit(root, action="worker.launch", category="loopd",
                 payload={"worker_id": worker_id, "agent": agent, "profile": profile,
                          "pane_id": pane, "autonomous": bool(autonomous)})
    return {"ok": True, "worker_id": worker_id, "pane_id": pane, "session": session}


# Known first-run onboarding gates: capture-pane pattern → safe dismissal keys (best-effort).
# codex shows a hook-trust dialog; "3" = continue without trusting (hooks won't run).
_ONBOARDING = {
    "codex": [("Trust all and continue", ["3", "Enter"]), ("Review hooks", ["3", "Enter"])],
    "claude": [("Do you trust", ["1", "Enter"]), ("trust the files", ["1", "Enter"])],
    "agy": [],
}


def dismiss_onboarding(adapter: TmuxAdapterBase, pane: str, agent: str, *, rounds: int = 8) -> bool:
    """Best-effort: clear a CLI's first-run trust/onboarding gate so the boot prompt is not swallowed.

    No-op on the fake adapter (capture returns ""). Real adapter polls and sends the safe choice.
    """
    import time

    gates = _ONBOARDING.get(str(agent).strip().lower(), [])
    if not gates or not hasattr(adapter, "capture"):
        return False
    for _ in range(max(1, rounds)):
        screen = adapter.capture(pane) or ""
        for pattern, keys in gates:
            if pattern in screen:
                for k in keys:
                    if hasattr(adapter, "send_key"):
                        adapter.send_key(pane, k)  # type: ignore[attr-defined]
                time.sleep(1.0)
                return True
        time.sleep(1.5)
    return False


def account_login(root: Path, *, agent: str, account: str,
                  adapter: TmuxAdapterBase | None = None) -> dict[str, Any]:
    """Open a tmux window under the account's isolated HOME running the CLI so the user can log in.

    The browser OAuth the user completes is stored under the isolated HOME — separate per account.
    Code Brain never reads the credentials.
    """
    prof = wp.account_profile(agent, account)
    wp.ensure_profile_dirs(root, prof)
    env = wp.resolve_profile_env(root, prof)
    session = session_name(root) + "-login"
    window = prof
    command = AGENT_COMMANDS.get(str(agent).strip().lower())
    if not command:
        return {"ok": False, "reason": f"unknown agent: {agent}"}
    adapter = adapter or get_adapter()
    pane = adapter.new_window(session, window, env, command)  # type: ignore[attr-defined]
    if not pane:
        return {"ok": False, "reason": "tmux launch failed"}
    return {"ok": True, "agent": agent, "account": account, "profile": prof,
            "session": session, "pane_id": pane,
            "note": f"Attach with `tmux attach -t {session}` and complete the browser login; "
                    f"credentials are stored isolated under {env['HOME']}."}


def launch_pool(root: Path, *, adapter: TmuxAdapterBase | None = None,
                dry_run: bool = False, autonomous: bool = False, tier: str | None = None) -> dict[str, Any]:
    """Launch a worker for every registered profile that is not already up (idempotent)."""
    session = session_name(root)
    existing = {w["worker_id"] for w in wr.list_workers(root) if w.get("state") not in ("stopped", "lost")}
    results: list[dict[str, Any]] = []
    for prof in wp.list_profiles(root):
        wid = str(prof.get("worker_id") or f"{prof.get('agent')}-{prof.get('profile')}")
        ag = str(prof.get("agent"))
        if wid in existing:
            results.append({"worker_id": wid, "skipped": "already up"})
            continue
        if not agent_available(ag):  # auto-skip an agent whose CLI is not installed
            results.append({"worker_id": wid, "skipped": f"{ag} CLI not installed"})
            continue
        results.append(launch_worker(root, worker_id=wid, agent=ag,
                                     profile=str(prof.get("profile")), session=session,
                                     adapter=adapter, dry_run=dry_run, autonomous=autonomous, tier=tier))
    return {"ok": True, "session": session, "dry_run": dry_run, "autonomous": autonomous,
            "available_agents": available_agents(), "tmux": tmux_available(), "launched": results}
