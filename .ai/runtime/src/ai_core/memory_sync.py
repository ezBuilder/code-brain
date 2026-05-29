"""Opt-in, self-hosted memory auto-sync (P4) — git as transport, no central service.

Lets work bounce between machines (Mac↔VPS) without manual ``git push``/``pull``.
It is deliberately conservative and NEVER touches the user's code:

  * Commits ONLY the memory paths (``.ai/memory/``) using a pathspec commit, so a staged
    code change in the user's index is left untouched. ``AGENTS.md`` is deliberately NOT
    synced — it is a git-ignored, per-machine mirror regenerated from ``.ai/memory``.
  * Fetches, then integrates remote memory by rebasing the local memory commit onto the
    upstream — but ONLY when the rest of the working tree is clean. If other (code)
    changes are in flight, the rebase is skipped (the cb-behind banner already nags to
    pull). On any rebase conflict it aborts cleanly and reports — never a half-merged tree.
  * Writes a per-machine heartbeat so other machines can show "VPS synced 3m ago".

Hard rule: this does NETWORK I/O (fetch/push) and MUST NOT run on the hooks/MCP hot
path. It is invoked explicitly by ``ai memory sync`` (one-shot or ``--loop`` daemon) and,
when ``memory_sync.enabled`` is set, spawned detached at SessionEnd.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .session_resume import machine_id

_GIT_TIMEOUT = 30
# The ONLY paths this sync is allowed to commit. Code is never staged/committed here.
# AGENTS.md is intentionally excluded: it is git-ignored (a per-machine memory mirror
# regenerated from .ai/memory). Listing it made `git add` abort on the ignored path,
# which left .ai/memory unstaged so the sync silently committed nothing.
MEMORY_PATHS = (".ai/memory",)
_HEARTBEAT_DIR = (".ai", "memory", "sync")


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(root: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "", "git-exec-failed"
    return proc.returncode == 0, proc.stdout, proc.stderr


def sync_enabled(root: Path) -> bool:
    """memory_sync.enabled in .ai/config.yaml (default False). Off by default so the
    automatic SessionEnd spawn and the daemon stay opt-in; the explicit `ai memory sync`
    command runs regardless."""
    try:
        from .config import load_config

        cfg = load_config(Path(root))
    except Exception:
        return False
    block = cfg.get("memory_sync") if isinstance(cfg, dict) else None
    return bool(isinstance(block, dict) and block.get("enabled"))


def _write_heartbeat(root: Path, mid: str, agent: str) -> None:
    d = Path(root).joinpath(*_HEARTBEAT_DIR)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"heartbeat-{mid}.json").write_text(
            json.dumps({"machine_id": mid, "agent": agent, "synced_at": _utc()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _other_paths_dirty(root: Path) -> bool:
    """True if the working tree has TRACKED-file changes OUTSIDE the memory paths (user
    code in flight) — rebase is then unsafe and skipped. Untracked files (``??``) are
    ignored: rebase does not touch them, and they are often gitignored cache anyway.
    Unknown status → treated dirty (conservative)."""
    ok, out, _ = _git(root, "status", "--porcelain")
    if not ok:
        return True
    for line in out.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):  # untracked — does not block a rebase
            continue
        path = line[3:].strip().strip('"')
        if " -> " in line:  # rename: "R  old -> new"
            path = line.split(" -> ", 1)[1].strip().strip('"')
        if path.startswith(".ai/memory/") or path == "AGENTS.md":
            continue
        return True
    return False


def _maybe_repair_audit_chain(root: Path) -> None:
    """After a memory merge/rebase the union-merged audit jsonl can have a broken
    prev_sha chain. Re-chain it deterministically via the existing ai command."""
    ai_bin = Path(root) / ".ai" / "bin" / "ai"
    if not ai_bin.exists():
        return
    try:
        subprocess.run(
            [str(ai_bin), "audit", "repair-chain", "--json"],
            cwd=str(root), capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def sync_once(root: Path, *, agent: str = "agent", push: bool = True) -> dict[str, Any]:
    """One sync cycle. Safe to call repeatedly; no-op when nothing changed and in sync."""
    root = Path(root)
    mid = machine_id(root)
    res: dict[str, Any] = {
        "ok": True, "machine_id": mid, "committed": False, "pushed": False,
        "rebased": False, "behind_before": 0, "ahead_before": 0,
        "skipped_rebase": False, "conflict": False, "errors": [],
    }
    if not _git(root, "rev-parse", "--is-inside-work-tree")[0]:
        res["ok"] = False
        res["errors"].append("not-a-git-repo")
        return res

    _write_heartbeat(root, mid, agent)

    # Commit ONLY the memory paths that actually exist. Stage them first so NEW files
    # (e.g. first handoff.json / new session dir) are included, then pathspec-commit just
    # those paths — which leaves any code the user has staged untouched (never committed).
    paths = [p for p in MEMORY_PATHS if (root / p).exists()]
    if paths:
        _git(root, "add", "--", *paths)
        _staged_ok, staged_out, _ = _git(root, "diff", "--cached", "--name-only", "--", *paths)
        if staged_out.strip():
            msg = f"chore(memory): sync {mid} via {agent} {_utc()}"
            ok_c, _, err = _git(root, "-c", "commit.gpgsign=false", "commit", "-m", msg, "--", *paths)
            res["committed"] = ok_c
            if not ok_c:
                res["errors"].append("commit-failed: " + err.strip()[:160])

    up_ok, up_out, _ = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    upstream = up_out.strip() if up_ok else ""
    if not upstream:
        res["errors"].append("no-upstream")
        return res

    _git(root, "fetch", "--quiet", "--no-tags")

    cnt_ok, cnt_out, _ = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    if cnt_ok and len(cnt_out.split()) == 2:
        try:
            res["ahead_before"], res["behind_before"] = int(cnt_out.split()[0]), int(cnt_out.split()[1])
        except ValueError:
            pass

    if res["behind_before"] > 0:
        if _other_paths_dirty(root):
            res["skipped_rebase"] = True
            res["errors"].append("remote-ahead-but-worktree-dirty: pull manually")
            return res  # never push a diverged branch
        rok, _, rerr = _git(root, "-c", "commit.gpgsign=false", "rebase", upstream)
        if rok:
            res["rebased"] = True
            _maybe_repair_audit_chain(root)
        else:
            _git(root, "rebase", "--abort")
            res["conflict"] = True
            res["errors"].append("rebase-conflict-aborted: " + rerr.strip()[:160])
            return res

    if push:
        ahead_ok, ahead_out, _ = _git(root, "rev-list", "--count", f"{upstream}..HEAD")
        ahead_now = int(ahead_out.strip()) if ahead_ok and ahead_out.strip().isdigit() else 0
        if ahead_now > 0:
            pok, _, perr = _git(root, "push", "--quiet")
            res["pushed"] = pok
            if not pok:
                res["errors"].append("push-failed: " + perr.strip()[:160])
    return res


def sync_loop(root: Path, *, agent: str = "agent", interval: int = 180) -> None:
    """Daemon mode: sync every `interval` seconds. Run under systemd/launchd on the VPS.
    Errors per cycle are swallowed so the loop survives transient offline/auth issues."""
    interval = max(30, int(interval))
    while True:
        try:
            sync_once(root, agent=agent)
        except Exception:
            pass
        time.sleep(interval)


def peer_sync_summary(root: Path) -> str:
    """One-line summary of OTHER machines' last sync (from committed heartbeats), e.g.
    'cb-sync: peers — vps-ab12 synced 2026-05-29T13:00Z'. '' when no peers."""
    d = Path(root).joinpath(*_HEARTBEAT_DIR)
    if not d.is_dir():
        return ""
    here = machine_id(root)
    peers: list[str] = []
    try:
        for f in sorted(d.glob("heartbeat-*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            mid = str(obj.get("machine_id") or "")
            if not mid or mid == here:
                continue
            peers.append(f"{mid} synced {str(obj.get('synced_at') or '')[:16]}")
    except OSError:
        return ""
    if not peers:
        return ""
    return "cb-sync: peers — " + "; ".join(peers[:4])


__all__ = ["sync_once", "sync_loop", "sync_enabled", "peer_sync_summary", "MEMORY_PATHS"]
