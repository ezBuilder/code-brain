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
  * Pushes ONLY when every commit ahead of upstream is memory-only. If the user has
    unpushed code/infra commits, the push is held (``skipped_push``) so the sync never
    publishes their in-flight work or invites an amend/rebase divergence.
  * Holds a per-machine lock per cycle so the SessionEnd daemon and a manual ``ai memory
    sync`` can't race (double commit/push); a stale lock is stolen after a TTL.
  * Writes a per-machine heartbeat so other machines can show "VPS synced 3m ago".
  * Raw audit is local-private by default. Syncing it requires an explicit private-remote
    confirmation, stages the complete physical audit set (including ignored immutable
    segments), and fails closed on a missing segment or invalid lineage. Rebase conflicts
    are aborted; audit history is never union-merged or silently re-chained.

Hard rule: this does NETWORK I/O (fetch/push) and MUST NOT run on the hooks/MCP hot
path. It is invoked ONLY explicitly by ``ai memory sync`` (one-shot or ``--loop``
daemon) — never spawned from a hook, even detached, because a background process
launched FROM a hook is still the hook causing network I/O. ``memory_sync.enabled``
in ``.ai/config.yaml`` is a deprecated no-op kept for one release for backward
compatibility; ``ai doctor`` reports it once per invocation rather than silently
reactivating an automatic spawn.
"""
from __future__ import annotations

import json
import os
import stat
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
_MEMORY_STAGE_PATHS = (
    *MEMORY_PATHS,
    ":(exclude,glob).ai/memory/*.lock",
    ":(exclude,glob).ai/memory/**/*.lock",
    ":(exclude).ai/memory/audit-index.jsonl",
    ":(exclude).ai/memory/audit-rollups",
    ":(exclude).ai/memory/episodic",
    ":(exclude).ai/memory/events",
    ":(exclude).ai/memory/inbox",
    ":(exclude).ai/memory/outbox",
    ":(exclude).ai/memory/queue",
    ":(exclude).ai/memory/loop",
)
_HEARTBEAT_DIR = (".ai", "memory", "sync")
# Per-machine, git-ignored lock so the detached SessionEnd daemon and a manual
# `ai memory sync` never run a cycle concurrently (double commit/push races). A lock
# older than _LOCK_TTL is treated as stale and stolen (crash-safe).
_LOCK_PATH = (".ai", "cache", "memory-sync.lock")
_LOCK_TTL = 120


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
    """memory_sync.enabled in .ai/config.yaml (default False).

    No hook spawns from this flag anymore (network I/O is banned on the hot path even
    when detached); the explicit `ai memory sync` command and `--loop` daemon run
    regardless of this setting. The flag is read only for the `ai doctor` deprecation
    diagnostic and by any external tooling that wants to know the configured intent."""
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


def _is_memory_path(path: str) -> bool:
    """A repo-relative path the sync is allowed to own (stage/commit/push)."""
    return path == ".ai/memory" or path.startswith(".ai/memory/")


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
        if _is_memory_path(path) or path == "AGENTS.md":
            continue
        return True
    return False


def _audit_sync_preflight(
    root: Path,
    *,
    private_remote_confirmed: bool,
    transaction_locked: bool = False,
) -> tuple[bool, str]:
    """Prove that raw audit can be committed as one complete private snapshot."""

    from .doctor import _check_audit_chain_snapshot, check_audit_chain, check_gitattributes
    from .memory import all_audit_files

    physical = {path.relative_to(root).as_posix() for path in all_audit_files(root)}
    tracked_ok, tracked_out, _ = _git(
        root, "ls-files", "-z", "--", ".ai/memory/audit"
    )
    if not tracked_ok:
        return False, "audit-tracked-set-unavailable"
    tracked = {path for path in tracked_out.split("\0") if path}
    missing = sorted(tracked - physical)
    if missing:
        return False, "audit-segment-missing:" + ",".join(missing[:4])
    if not physical:
        return True, ""
    if not private_remote_confirmed:
        return False, "private-remote-confirmation-required"
    attributes = check_gitattributes(root)
    if not attributes.ok:
        return False, "audit-no-merge-policy-missing"
    chain = (
        _check_audit_chain_snapshot(root)
        if transaction_locked
        else check_audit_chain(root)
    )
    if not chain.ok:
        return False, "audit-integrity-invalid:" + chain.detail[:160]
    return True, ""


def _stage_memory_snapshot(root: Path, *, private_remote_confirmed: bool) -> tuple[bool, str]:
    """Stage memory, force-including private data only after explicit confirmation."""

    if private_remote_confirmed:
        ok, _out, err = _git(root, "add", "-f", "--", *_MEMORY_STAGE_PATHS)
    else:
        ok, _out, err = _git(root, "add", "--", *_MEMORY_STAGE_PATHS)
    return ok, err.strip()[:160]


def _ahead_has_non_memory_commit(root: Path, upstream: str) -> bool:
    """True if any commit in ``upstream..HEAD`` touches a path OUTSIDE ``.ai/memory`` — i.e.
    the user has unpushed code/infra commits. The sync must NOT push those for them: doing so
    silently publishes in-flight work and invites amend/rebase divergence. Unknown git state
    → True (conservative: hold the push)."""
    ok, out, _ = _git(root, "rev-list", f"{upstream}..HEAD")
    if not ok:
        return True
    for sha in (s for s in out.split() if s):
        fok, fout, _ = _git(root, "show", "--name-only", "--pretty=format:", sha)
        if not fok:
            return True
        for f in fout.splitlines():
            f = f.strip().strip('"')
            if f and not _is_memory_path(f):
                return True
    return False


def _lock_owner_alive(text: str) -> bool:
    try:
        pid = int(text.split(maxsplit=1)[0])
    except (IndexError, TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_sync_lock(root: Path) -> tuple[str, str | None]:
    """Acquire a fail-closed non-blocking lock and return ``(state, token)``.

    ``state`` is ``acquired``, ``busy``, or ``error``. A stale lock is stolen only
    when its recorded process is no longer alive; infrastructure failures never
    permit an unlocked network/commit cycle.
    """
    p = Path(root).joinpath(*_LOCK_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.parent.resolve(strict=True).relative_to(Path(root).resolve(strict=True))
    except (OSError, ValueError):
        return "error", None
    try:
        state = p.lstat()
        if not stat.S_ISREG(state.st_mode) or stat.S_ISLNK(state.st_mode) or state.st_nlink != 1:
            return "error", None
        if (time.time() - state.st_mtime) <= _LOCK_TTL:
            return "busy", None
        try:
            owner = p.read_text(encoding="utf-8")[:256]
        except (OSError, UnicodeDecodeError):
            return "error", None
        if _lock_owner_alive(owner):
            return "busy", None
        if p.lstat().st_ino != state.st_ino:
            return "busy", None
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return "error", None
    token = f"{os.getpid()} {time.monotonic_ns()}-{_utc()}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(p), flags, 0o600)
    except FileExistsError:
        return "busy", None
    except OSError:
        return "error", None
    try:
        os.write(fd, token.encode("utf-8"))
        os.fsync(fd)
    except OSError:
        try:
            p.unlink()
        except OSError:
            pass
        return "error", None
    finally:
        os.close(fd)
    return "acquired", token


def _release_sync_lock(root: Path, token: str) -> None:
    p = Path(root).joinpath(*_LOCK_PATH)
    try:
        state = p.lstat()
        if not stat.S_ISREG(state.st_mode) or stat.S_ISLNK(state.st_mode) or state.st_nlink != 1:
            return
        if p.read_text(encoding="utf-8") != token:
            return
        p.unlink()
    except (OSError, UnicodeDecodeError):
        pass


def sync_once(
    root: Path,
    *,
    agent: str = "agent",
    push: bool = True,
    private_remote_confirmed: bool = False,
) -> dict[str, Any]:
    """One sync cycle. Safe to call repeatedly; no-op when nothing changed and in sync.

    Holds a per-machine lock for the cycle so the detached SessionEnd daemon and a manual
    ``ai memory sync`` cannot race (double commit/push). If a live cycle holds the lock this
    returns immediately with ``skipped_lock=True``."""
    root = Path(root)
    lock_state, lock_token = _acquire_sync_lock(root)
    if lock_state != "acquired" or lock_token is None:
        lock_error = None if lock_state == "busy" else "sync-lock-unavailable"
        return {
            "ok": lock_error is None, "machine_id": machine_id(root), "committed": False, "pushed": False,
            "rebased": False, "behind_before": 0, "ahead_before": 0, "skipped_rebase": False,
            "conflict": False, "skipped_push": False, "skipped_lock": True,
            "errors": [] if lock_error is None else [lock_error],
        }
    try:
        return _sync_once_locked(
            root,
            agent=agent,
            push=push,
            private_remote_confirmed=private_remote_confirmed,
        )
    finally:
        _release_sync_lock(root, lock_token)


def _sync_once_locked(
    root: Path,
    *,
    agent: str = "agent",
    push: bool = True,
    private_remote_confirmed: bool = False,
) -> dict[str, Any]:
    """One sync cycle (run while holding the per-machine lock). Safe to call repeatedly;
    no-op when nothing changed and in sync."""
    root = Path(root)
    mid = machine_id(root)
    res: dict[str, Any] = {
        "ok": True, "machine_id": mid, "committed": False, "pushed": False,
        "rebased": False, "behind_before": 0, "ahead_before": 0,
        "skipped_rebase": False, "conflict": False, "skipped_push": False,
        "skipped_lock": False, "errors": [],
    }
    if not _git(root, "rev-parse", "--is-inside-work-tree")[0]:
        res["ok"] = False
        res["errors"].append("not-a-git-repo")
        return res

    audit_ok, audit_error = _audit_sync_preflight(
        root, private_remote_confirmed=private_remote_confirmed
    )
    if not audit_ok:
        res["ok"] = False
        res["errors"].append(audit_error)
        return res

    _write_heartbeat(root, mid, agent)

    # Commit ONLY the memory paths that actually exist. Stage them first so NEW files
    # (e.g. first handoff.json / new session dir) are included, then pathspec-commit just
    # those paths — which leaves any code the user has staged untouched (never committed).
    paths = [p for p in MEMORY_PATHS if (root / p).exists()]
    if paths:
        from .memory import audit_transaction_lock_path
        from .private_write import private_file_lock

        try:
            with private_file_lock(audit_transaction_lock_path(root), root=root):
                audit_ok, audit_error = _audit_sync_preflight(
                    root,
                    private_remote_confirmed=private_remote_confirmed,
                    transaction_locked=True,
                )
                if not audit_ok:
                    res["ok"] = False
                    res["errors"].append(audit_error)
                    return res
                staged_ok, stage_error = _stage_memory_snapshot(
                    root, private_remote_confirmed=private_remote_confirmed
                )
                if not staged_ok:
                    res["ok"] = False
                    res["errors"].append("stage-failed: " + stage_error)
                    return res
                _staged_ok, staged_out, _ = _git(
                    root, "diff", "--cached", "--name-only", "--", *_MEMORY_STAGE_PATHS
                )
                if staged_out.strip():
                    msg = f"chore(memory): sync {mid} via {agent} {_utc()}"
                    ok_c, _, err = _git(
                        root,
                        "-c",
                        "commit.gpgsign=false",
                        "commit",
                        "-m",
                        msg,
                        "--",
                        *_MEMORY_STAGE_PATHS,
                    )
                    res["committed"] = ok_c
                    if not ok_c:
                        res["ok"] = False
                        res["errors"].append("commit-failed: " + err.strip()[:160])
                        return res
        except OSError as exc:
            res["ok"] = False
            res["errors"].append(f"audit-lock-failed: {type(exc).__name__}")
            return res

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
            audit_ok, audit_error = _audit_sync_preflight(
                root, private_remote_confirmed=private_remote_confirmed
            )
            if not audit_ok:
                res["ok"] = False
                res["errors"].append("post-rebase-" + audit_error)
                return res
        else:
            _git(root, "rebase", "--abort")
            res["conflict"] = True
            res["errors"].append("rebase-conflict-aborted: " + rerr.strip()[:160])
            return res

    if push:
        ahead_ok, ahead_out, _ = _git(root, "rev-list", "--count", f"{upstream}..HEAD")
        ahead_now = int(ahead_out.strip()) if ahead_ok and ahead_out.strip().isdigit() else 0
        if ahead_now > 0 and _ahead_has_non_memory_commit(root, upstream):
            # The user has unpushed code/infra commits ahead. Pushing here would silently
            # publish their in-flight work and set up amend/rebase divergence — leave the
            # push to them; our memory commit rides along on their next push.
            res["skipped_push"] = True
            res["errors"].append("unpushed-non-memory-commits: left push to you")
        elif ahead_now > 0:
            pok, _, perr = _git(root, "push", "--quiet")
            res["pushed"] = pok
            if not pok:
                res["errors"].append("push-failed: " + perr.strip()[:160])
    return res


def sync_loop(
    root: Path,
    *,
    agent: str = "agent",
    interval: int = 180,
    private_remote_confirmed: bool = False,
) -> None:
    """Daemon mode: sync every `interval` seconds. Run under systemd/launchd on the VPS.
    Errors per cycle are swallowed so the loop survives transient offline/auth issues."""
    interval = max(30, int(interval))
    while True:
        try:
            sync_once(
                root,
                agent=agent,
                private_remote_confirmed=private_remote_confirmed,
            )
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
