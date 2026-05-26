"""Detect when Code Brain's recorded memory has fallen behind real git progress.

Agents are supposed to call ``record_decision`` / ``append_session_note``, but when
they forget, the shared memory (``session-current.md``, ``decisions.jsonl``) silently
freezes while git keeps advancing. A resume snapshot then *looks* fresh (its
``written_at`` is new) yet carries stale content, so different agents answer
"how far did we get" from their own native memory and diverge.

``memory_freshness`` compares the most recent *recorded* memory timestamp against the
git HEAD history and working tree so the hook layer can surface a visible staleness
banner and converge every agent on git truth.

All git access is guarded: a non-git directory, missing git binary, or any failure
yields ``stale=False`` — staleness is a hint, never a hard error.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

# Matches the leading ``- [2026-05-26T11:43:18.640870Z] ...`` timestamp that
# append_session_note / append_event prepend to each milestone line.
_ISO_LINE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\]]*)\]")

_GIT_TIMEOUT = 5
# Dirty-tree size that on its own implies meaningful unrecorded progress. A few
# stray edits are normal; a large working tree means real work is in flight.
DIRTY_STALE_THRESHOLD = 5
# Cap commit enumeration so a long-frozen project does not produce a huge banner.
MAX_COMMITS = 20


def _last_session_note_iso(root: Path) -> str:
    path = root / ".ai" / "memory" / "session-current.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    last = ""
    for line in text.splitlines():
        match = _ISO_LINE_RE.search(line)
        if match:
            last = match.group(1)
    return last


def _last_decision_iso(root: Path) -> str:
    path = root / ".ai" / "memory" / "decisions.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    last = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            ts = str(obj.get("decided_at") or obj.get("timestamp") or "")
            if ts > last:
                last = ts
    return last


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


def memory_freshness(root: Path) -> dict[str, Any]:
    """Compare recorded-memory recency against git HEAD/working tree.

    Returns a dict with ``stale`` plus the evidence used to decide it. Safe to call
    in any directory: ``git=False`` (and ``stale=False``) when not a git repo.
    """
    root = Path(root)
    last_recorded = max(_last_session_note_iso(root), _last_decision_iso(root))

    ok_head, head_out = _git(root, "rev-parse", "--short", "HEAD")
    if not ok_head:
        return {
            "ok": True,
            "git": False,
            "stale": False,
            "last_recorded": last_recorded,
            "head": "",
            "commit_count": 0,
            "commits": [],
            "dirty_count": 0,
        }

    log_args = ["log", "-n", str(MAX_COMMITS), "--pretty=%h\t%s"]
    if last_recorded:
        # --since bounds by commit date; without a recorded timestamp every commit
        # up to the cap counts, which is correct — empty memory is itself stale.
        log_args.insert(1, f"--since={last_recorded}")
    ok_log, log_out = _git(root, *log_args)
    commits: list[dict[str, str]] = []
    if ok_log:
        for line in log_out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            sha, _, subject = line.partition("\t")
            commits.append({"sha": sha, "subject": subject[:80]})

    ok_status, status_out = _git(root, "status", "--porcelain")
    dirty_count = len([ln for ln in status_out.splitlines() if ln.strip()]) if ok_status else 0

    commit_count = len(commits)
    stale = bool(commit_count > 0 or dirty_count >= DIRTY_STALE_THRESHOLD)
    return {
        "ok": True,
        "git": True,
        "stale": stale,
        "last_recorded": last_recorded,
        "head": head_out.strip(),
        "commit_count": commit_count,
        "commits": commits,
        "dirty_count": dirty_count,
    }


def staleness_banner(root: Path) -> str:
    """One-line operator-facing banner, or ``""`` when shared memory is fresh."""
    info = memory_freshness(root)
    if not info.get("stale"):
        return ""

    last = (info.get("last_recorded") or "기록 없음")[:19]
    parts = [f"cb-stale: 공유 메모리 마지막 기록={last} 이후 실제 진행이 반영되지 않았다."]

    count = int(info.get("commit_count") or 0)
    if count:
        shown = info["commits"][:2]
        subjects = "; ".join(c["subject"][:60] for c in shown)
        extra = f" 외 {count - len(shown)}개" if count > len(shown) else ""
        parts.append(f" git 커밋 {count}개 미기록(최근: {subjects}{extra}).")

    dirty = int(info.get("dirty_count") or 0)
    if dirty:
        parts.append(f" 작업트리 dirty {dirty}파일.")

    parts.append(
        " → '어디까지 진행했나'의 정답은 git log/status다. 종료 전 "
        "`ai memory session append`(또는 record_decision)로 기록해 다음 세션·다른 에이전트와 동기화하라."
    )
    return "".join(parts)


__all__ = ["memory_freshness", "staleness_banner", "DIRTY_STALE_THRESHOLD"]
