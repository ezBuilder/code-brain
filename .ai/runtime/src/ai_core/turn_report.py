"""Deterministic turn-change snapshot + a bounded "summarize this" nudge (no LLM, no network).

Why not enforce a summary from the Stop hook directly: Claude can attach Stop
``additionalContext`` and block, while Antigravity can return ``decision:"continue"``.
Both mechanisms re-open the model turn and consume the same bounded continuation budget
as correctness/security guards; Codex portability is also host-version dependent. Spending
that limited channel to polish prose would add tokens and dilute real blocks. So this module
uses a standing response rule plus two hooks that do not create a prose-only continuation:

  * ``Stop``   → measure what the turn actually changed and persist a tiny snapshot.
  * ``UserPromptSubmit`` → inject one line, and only when the change was large enough
    that a human would want a summary.

The measurement is git facts only (files / insertions / deletions / HEAD), never an LLM
summary of the model's own answer — same rule ``_auto_milestone_on_stale`` follows, so an
automated line can never launder a hallucination into the next turn's context. It also
cannot depend on the model's answer text: ``last_assistant_message`` is absent from most
hosts' Stop payload (measured: Codex populates it on a minority of turns, Antigravity
never), so anything derived from it would silently no-op.

Costs are measured, not assumed: ``git diff --shortstat`` was 36ms on code-brain, 92ms on
navio, 687ms on blurivo — above the 200ms Stop budget on a big tree, hence the snapshot is
written by a DETACHED child like ``_spawn_background_rebuild``, never inline.

stdlib only. Fail-soft everywhere: any error yields no snapshot and no nudge.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .private_write import atomic_write_private_text, private_file_lock

# Files changed in one turn at/above which a human wants a short summary instead of prose.
DEFAULT_SUMMARY_MIN_FILES = 8
# Total churned lines (insertions + deletions) that also warrants a summary on few files.
DEFAULT_SUMMARY_MIN_LINES = 200
# A snapshot older than this is stale context for the *next* turn; drop it silently.
SNAPSHOT_MAX_AGE_SECONDS = 6 * 60 * 60
_GIT_TIMEOUT = 5
STATE_PARTS = (".ai", "cache", "turn_report.json")


def state_path(root: Path) -> Path:
    return Path(root).joinpath(*STATE_PARTS)


def _env_disabled(name: str, default: str = "1") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in ("0", "false", "no")


def _int_env(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _git(root: Path, *args: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if proc.returncode != 0:
        return False, ""
    return True, proc.stdout


def _parse_shortstat(text: str) -> tuple[int, int, int]:
    """Parse ``N files changed, N insertions(+), N deletions(-)`` → (files, ins, del)."""
    files = insertions = deletions = 0
    for chunk in text.strip().split(","):
        chunk = chunk.strip()
        head = chunk.split(" ", 1)[0]
        if not head.isdigit():
            continue
        value = int(head)
        if "file" in chunk:
            files = value
        elif "insertion" in chunk:
            insertions = value
        elif "deletion" in chunk:
            deletions = value
    return files, insertions, deletions


# Code Brain writes to .ai/ on virtually every hook (audit jsonl, caches, this very
# snapshot file), so counting it would attribute Code Brain's own bookkeeping to the
# user's turn. Measured on code-brain: including .ai reported 16 files/+943/-370 and 10
# untracked, excluding it reported the real 5 files/+39/-15 and 0 untracked.
_EXCLUDE_PATHSPEC = (".", ":(exclude).ai")


def measure(root: Path) -> dict[str, Any]:
    """Return deterministic git facts for the current working tree. ``git=False`` off-repo."""
    root = Path(root)
    ok_head, head_out = _git(root, "rev-parse", "--short", "HEAD")
    if not ok_head:
        return {"git": False, "files": 0, "insertions": 0, "deletions": 0, "head": "", "untracked": 0}
    # Unstaged + staged, so a turn that staged its work is still counted.
    files = insertions = deletions = 0
    for args in (("diff", "--shortstat"), ("diff", "--shortstat", "--cached")):
        ok, out = _git(root, *args, "--", *_EXCLUDE_PATHSPEC)
        if ok:
            f, i, d = _parse_shortstat(out)
            files += f
            insertions += i
            deletions += d
    ok_untracked, untracked_out = _git(
        root, "ls-files", "--others", "--exclude-standard", "--", *_EXCLUDE_PATHSPEC
    )
    untracked = len([ln for ln in untracked_out.splitlines() if ln.strip()]) if ok_untracked else 0
    return {
        "git": True,
        "files": files,
        "insertions": insertions,
        "deletions": deletions,
        "head": str(head_out).strip(),
        "untracked": untracked,
    }


def _delta(prev: dict[str, Any], current: dict[str, Any]) -> dict[str, int]:
    """Per-turn change = |now - previous| on each counter.

    Absolute difference, not signed: a turn that *reverts* 200 lines changed just as much
    as one that adds them, and a turn that commits its work (shrinking the dirty diff) is
    still a large turn. A no-op turn leaves every counter equal and therefore yields 0.
    """
    base = prev.get("measured") if isinstance(prev.get("measured"), dict) else {}
    out: dict[str, int] = {}
    for key in ("files", "insertions", "deletions", "untracked"):
        try:
            before = int(base.get(key) or 0)
        except (TypeError, ValueError):
            before = 0
        out[key] = abs(int(current.get(key) or 0) - before)
    return out


def write_snapshot(root: Path, *, agent: str = "", now: float | None = None) -> dict[str, Any]:
    """Measure the tree and persist the delta against the previous snapshot.

    Called from a DETACHED child at turn end, so the git cost never lands on the hook
    budget. Returns the written snapshot (or ``{}`` when disabled / off-repo / unwritable).
    """
    if _env_disabled("AI_TURN_REPORT", default="1"):
        return {}
    root = Path(root)
    current = measure(root)
    if not current.get("git"):
        return {}
    path = state_path(root)
    try:
        with private_file_lock(path.with_suffix(".lock"), root=root):
            prev = _read_state(root)
            prev_head = str(prev.get("head") or "")
            head = str(current.get("head") or "")
            # A repo can be dirty for reasons that predate this turn (measured: blurivo
            # carries hundreds of modified files at rest). Compare only adjacent turn-end
            # snapshots; the first write is a baseline that never nudges.
            have_baseline = bool(prev.get("measured"))
            delta = _delta(prev, current) if have_baseline else {
                "files": 0, "insertions": 0, "deletions": 0, "untracked": 0,
            }
            snapshot = {
                "ts": now if now is not None else time.time(),
                "agent": str(agent or "")[:32],
                "files": delta["files"],
                "insertions": delta["insertions"],
                "deletions": delta["deletions"],
                "untracked": delta["untracked"],
                "measured": {
                    "files": int(current["files"]),
                    "insertions": int(current["insertions"]),
                    "deletions": int(current["deletions"]),
                    "untracked": int(current["untracked"]),
                },
                "baseline": not have_baseline,
                "head": head,
                "head_moved": bool(prev_head and head and prev_head != head),
                "reported": False,
            }
            atomic_write_private_text(
                path,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                root=root,
            )
    except OSError:
        return {}
    return snapshot


def _read_state(root: Path) -> dict[str, Any]:
    try:
        raw = state_path(root).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _mark_reported(root: Path, snapshot: dict[str, Any]) -> None:
    """Flag the snapshot consumed so the same turn is never nudged about twice."""
    path = state_path(root)
    try:
        with private_file_lock(path.with_suffix(".lock"), root=Path(root)):
            current = _read_state(root)
            # A newer detached Stop snapshot won the race. Never overwrite it with the stale
            # copy that UserPromptSubmit just consumed.
            if current.get("ts") != snapshot.get("ts"):
                return
            current["reported"] = True
            atomic_write_private_text(
                path,
                json.dumps(current, ensure_ascii=False, sort_keys=True),
                root=Path(root),
            )
    except OSError:
        pass


def is_large(snapshot: dict[str, Any]) -> bool:
    if snapshot.get("baseline"):
        return False  # first measurement has nothing to diff against
    min_files = _int_env("AI_TURN_REPORT_MIN_FILES", DEFAULT_SUMMARY_MIN_FILES, minimum=1)
    min_lines = _int_env("AI_TURN_REPORT_MIN_LINES", DEFAULT_SUMMARY_MIN_LINES, minimum=1)
    files = int(snapshot.get("files") or 0) + int(snapshot.get("untracked") or 0)
    churn = int(snapshot.get("insertions") or 0) + int(snapshot.get("deletions") or 0)
    return files >= min_files or churn >= min_lines


def nudge_line(root: Path, *, now: float | None = None) -> str:
    """One line for UserPromptSubmit when the previous turn changed a lot, else "".

    Consumes the snapshot (single-shot) so a quiet follow-up turn is not nagged again.
    """
    if _env_disabled("AI_TURN_REPORT", default="1"):
        return ""
    snapshot = _read_state(root)
    if not snapshot or snapshot.get("reported"):
        return ""
    ts = float(snapshot.get("ts") or 0.0)
    current = now if now is not None else time.time()
    if ts <= 0 or (current - ts) > SNAPSHOT_MAX_AGE_SECONDS:
        return ""
    if not is_large(snapshot):
        # Small turn: consume it so it cannot accumulate into a late, misleading nudge.
        _mark_reported(root, snapshot)
        return ""
    _mark_reported(root, snapshot)
    files = int(snapshot.get("files") or 0)
    untracked = int(snapshot.get("untracked") or 0)
    bits = [f"{files}c"] if files else []
    if untracked:
        bits.append(f"{untracked}new")
    churn = f"+{int(snapshot.get('insertions') or 0)}/-{int(snapshot.get('deletions') or 0)}"
    state = "committed" if snapshot.get("head_moved") else "dirty"
    # Terse on purpose. The UserPromptSubmit budget is only MAX_INJECTION_BYTES (2048 by
    # default) and build_context truncates from the TAIL, so a verbose line here silently
    # evicts the todos/lessons sections that follow it. Measured on this repo: the context
    # already fills the budget exactly, so every byte spent here costs a byte of memory.
    return (
        f"cb-turn: prev turn {' '.join(bits) or 'edits'} {churn} {state}; "
        "summarize key points, don't narrate."
    )
