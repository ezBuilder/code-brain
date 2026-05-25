"""TRAJEVAL-style trajectory diagnostics (PoC).

Pure-stdlib trajectory extraction and fine-grained analysis over the
``.ai/memory/audit/<year>.jsonl`` event stream.

The module is intentionally side-effect free: it never writes to memory,
never modifies obs/cli/memory, and uses streaming line-by-line reads so it
is safe to run over multi-MB audit logs.

Public API:
    extract_trajectories(root, *, session_id=None, limit=50) -> dict
    analyze_efficiency(traj) -> dict
    analyze_failures(traj) -> dict
    summarize(root, *, limit=10) -> dict
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# When no session_id is present in the payload, we group events into
# "anonymous" trajectories whenever a gap larger than this threshold appears
# between two consecutive events.
IDLE_GAP_SECONDS = 5 * 60  # 5 minutes

# Bucket size for anonymous-session naming (5 minutes).
ANON_BUCKET_SECONDS = 5 * 60

# Failure heuristic thresholds.
LOOP_MIN_REPEATS = 3
SHALLOW_MIN_EVENTS = 5
SHALLOW_MAX_DISTINCT_TOOLS = 3
OVER_EXPLORATION_UNIQUE_FILES = 22  # From the TRAJEVAL "22x review" finding.
OVER_EXPLORATION_MAX_EDITS = 3


# ---------------------------------------------------------------------------
# Audit log discovery & parsing
# ---------------------------------------------------------------------------


def _audit_dir(root: Path) -> Path:
    return root / ".ai" / "memory" / "audit"


def _audit_files(root: Path) -> list[Path]:
    """Return all ``<year>.jsonl`` audit files, oldest first."""
    audit_dir = _audit_dir(root)
    if not audit_dir.exists():
        return []
    files = sorted(
        p for p in audit_dir.iterdir() if p.is_file() and re.fullmatch(r"\d{4}\.jsonl", p.name)
    )
    return files


def _iter_audit_events(root: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed audit events line-by-line from all year files (chronological)."""
    for path in _audit_files(root):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # Skip malformed lines silently — audit may be partial.
                        continue
        except OSError:
            continue


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # Accept trailing 'Z' as UTC.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _tool_of(event: dict[str, Any]) -> str:
    """Best-effort tool/action name for an event.

    Order:
      1. ``payload.tool_name`` (string, non-empty, not "unknown")
      2. ``tool_name`` at the top level
      3. ``payload.kind`` (e.g. "PreToolUse")
      4. ``action`` (e.g. "memory.todo_add", "hook.slow")
    """
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        tn = payload.get("tool_name")
        if isinstance(tn, str) and tn and tn != "unknown":
            return tn
        kind = payload.get("kind")
        if isinstance(kind, str) and kind:
            return kind
    tn = event.get("tool_name")
    if isinstance(tn, str) and tn and tn != "unknown":
        return tn
    action = event.get("action")
    if isinstance(action, str) and action:
        return action
    return "unknown"


def _session_id_of(event: dict[str, Any]) -> str | None:
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        sid = payload.get("session_id") or payload.get("sid")
        if isinstance(sid, str) and sid:
            return sid
    sid = event.get("session_id")
    if isinstance(sid, str) and sid:
        return sid
    return None


def _payload_summary(payload: Any) -> dict[str, Any]:
    """Compact summary of an event payload (for human inspection)."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("kind", "tool_name", "agent", "path", "file_path", "reason"):
        if key in payload:
            value = payload[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
    return out


def _anon_session_id(ts: datetime) -> str:
    bucket = int(ts.replace(tzinfo=timezone.utc).timestamp() // ANON_BUCKET_SECONDS)
    return f"anon-{bucket}"


# ---------------------------------------------------------------------------
# Trajectory extraction
# ---------------------------------------------------------------------------


def _new_traj(sid: str) -> dict[str, Any]:
    return {
        "session_id": sid,
        "start_ts": "",
        "end_ts": "",
        "events": [],
        "total_events": 0,
        "duration_seconds": 0.0,
    }


def _finalize_traj(traj: dict[str, Any]) -> None:
    events = traj["events"]
    traj["total_events"] = len(events)
    if not events:
        return
    traj["start_ts"] = events[0]["ts"]
    traj["end_ts"] = events[-1]["ts"]
    start = _parse_ts(traj["start_ts"])
    end = _parse_ts(traj["end_ts"])
    if start and end:
        traj["duration_seconds"] = max(0.0, (end - start).total_seconds())


def extract_trajectories(
    root: Path,
    *,
    session_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Walk audit logs and group events into trajectories.

    Grouping rules:
      * Consecutive events with the same ``session_id`` (from payload) form
        one trajectory.
      * When ``session_id`` is missing, events are grouped by 5-minute idle
        gaps — a gap larger than ``IDLE_GAP_SECONDS`` starts a new anonymous
        trajectory named ``anon-<bucket>``.

    Returns a dict ``{ok, trajectories, scanned_events}`` where
    ``trajectories`` is sorted by ``start_ts`` descending and truncated to
    ``limit`` entries. When ``session_id`` is given, only that trajectory is
    returned (still inside a list).
    """
    if limit <= 0:
        limit = 1
    if not _audit_dir(root).exists():
        return {"ok": True, "trajectories": [], "scanned_events": 0}

    open_trajs: dict[str, dict[str, Any]] = {}
    finished: list[dict[str, Any]] = []
    last_anon_ts: datetime | None = None
    current_anon_sid: str | None = None
    scanned = 0

    for event in _iter_audit_events(root):
        scanned += 1
        ts_str = event.get("ts")
        ts = _parse_ts(ts_str)
        if ts is None:
            continue

        sid = _session_id_of(event)
        if sid is None:
            # Anonymous: use idle-gap bucketing.
            if (
                last_anon_ts is None
                or current_anon_sid is None
                or (ts - last_anon_ts).total_seconds() > IDLE_GAP_SECONDS
            ):
                # Finish the previous anonymous run if any.
                if current_anon_sid is not None and current_anon_sid in open_trajs:
                    finished.append(open_trajs.pop(current_anon_sid))
                current_anon_sid = _anon_session_id(ts)
                # Disambiguate consecutive anon runs that land in the same bucket.
                base = current_anon_sid
                bump = 0
                while current_anon_sid in open_trajs or any(
                    t["session_id"] == current_anon_sid for t in finished
                ):
                    bump += 1
                    current_anon_sid = f"{base}-{bump}"
                open_trajs[current_anon_sid] = _new_traj(current_anon_sid)
            sid_to_use = current_anon_sid
            last_anon_ts = ts
        else:
            sid_to_use = sid

        if session_id is not None and sid_to_use != session_id:
            continue

        if sid_to_use not in open_trajs:
            open_trajs[sid_to_use] = _new_traj(sid_to_use)
        traj = open_trajs[sid_to_use]
        traj["events"].append(
            {
                "ts": ts_str,
                "action": event.get("action", ""),
                "tool": _tool_of(event),
                "payload_summary": _payload_summary(event.get("payload")),
            }
        )

    # All remaining open trajectories are finished.
    for traj in open_trajs.values():
        finished.append(traj)

    for traj in finished:
        _finalize_traj(traj)

    # Sort by start_ts desc.
    finished = [t for t in finished if t["total_events"] > 0]
    finished.sort(key=lambda t: t["start_ts"], reverse=True)
    if session_id is not None:
        finished = [t for t in finished if t["session_id"] == session_id]
    finished = finished[:limit]

    return {"ok": True, "trajectories": finished, "scanned_events": scanned}


# ---------------------------------------------------------------------------
# Fine-grained analyses
# ---------------------------------------------------------------------------


def analyze_efficiency(traj: dict[str, Any]) -> dict[str, Any]:
    """Tool-use efficiency metrics for a single trajectory."""
    events = traj.get("events") or []
    total = len(events)
    if total == 0:
        return {
            "total_events": 0,
            "unique_tools": 0,
            "tool_repeat_rate": 0.0,
            "tools_per_minute": 0.0,
            "dominant_tool": "",
            "dominant_tool_share": 0.0,
        }

    tools = [str(e.get("tool") or "") for e in events]
    counter = Counter(tools)
    unique_tools = len([t for t in counter if t])

    # repeated_tool_calls: number of positions i>=1 where tools[i] == tools[i-1].
    repeated = sum(1 for i in range(1, total) if tools[i] == tools[i - 1])
    repeat_rate = repeated / total if total else 0.0

    dominant_tool, dominant_count = counter.most_common(1)[0]
    dominant_share = dominant_count / total if total else 0.0

    duration = float(traj.get("duration_seconds") or 0.0)
    if duration > 0:
        tools_per_minute = total / (duration / 60.0)
    else:
        tools_per_minute = float(total)  # All events in <1s — treat as burst.

    return {
        "total_events": total,
        "unique_tools": unique_tools,
        "tool_repeat_rate": round(repeat_rate, 4),
        "tools_per_minute": round(tools_per_minute, 2),
        "dominant_tool": dominant_tool,
        "dominant_tool_share": round(dominant_share, 4),
    }


def _detect_loop(tools: list[str]) -> bool:
    """True if a sub-sequence of length 2 or 3 repeats >= LOOP_MIN_REPEATS times."""
    n = len(tools)
    if n < 2 * LOOP_MIN_REPEATS:
        return False
    for window in (2, 3):
        if n < window * LOOP_MIN_REPEATS:
            continue
        for start in range(0, n - window * LOOP_MIN_REPEATS + 1):
            pattern = tuple(tools[start : start + window])
            if not all(pattern):
                continue
            repeats = 1
            cursor = start + window
            while cursor + window <= n and tuple(tools[cursor : cursor + window]) == pattern:
                repeats += 1
                cursor += window
            if repeats >= LOOP_MIN_REPEATS:
                return True
    return False


def _files_touched(events: Iterable[dict[str, Any]]) -> set[str]:
    files: set[str] = set()
    for event in events:
        payload = event.get("payload_summary") or {}
        if not isinstance(payload, dict):
            continue
        for key in ("path", "file_path"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                files.add(value)
    return files


def analyze_failures(traj: dict[str, Any]) -> dict[str, Any]:
    """Heuristic failure-mode detection (TRAJEVAL-inspired)."""
    events = traj.get("events") or []
    tools = [str(e.get("tool") or "") for e in events]
    total = len(tools)
    details: list[str] = []

    # Loop detection.
    loop_suspected = _detect_loop(tools)
    if loop_suspected:
        details.append("Repeated short tool sequence detected (>=3 cycles).")

    # Shallow exploration.
    distinct = len({t for t in tools if t})
    shallow = total >= SHALLOW_MIN_EVENTS and distinct < SHALLOW_MAX_DISTINCT_TOOLS
    if shallow:
        details.append(
            f"Shallow exploration: only {distinct} distinct tools across {total} events."
        )

    # Backtrack evidence: a Read (or read-like) event hits a path that was
    # already touched by an Edit/Write event earlier in the trajectory.
    backtrack = False
    edited_paths: set[str] = set()
    for event in events:
        tool = (event.get("tool") or "").lower()
        payload = event.get("payload_summary") or {}
        path = ""
        if isinstance(payload, dict):
            path = str(payload.get("path") or payload.get("file_path") or "")
        if not path:
            continue
        if "edit" in tool or "write" in tool:
            edited_paths.add(path)
        elif "read" in tool and path in edited_paths:
            backtrack = True
    if backtrack:
        details.append("Read-after-edit revisit on the same path.")

    # Over-exploration: many distinct files touched, few edits committed.
    files = _files_touched(events)
    edit_events = sum(1 for t in tools if "edit" in t.lower() or "write" in t.lower())
    over_exploration = (
        len(files) > OVER_EXPLORATION_UNIQUE_FILES and edit_events <= OVER_EXPLORATION_MAX_EDITS
    )
    if over_exploration:
        details.append(
            f"Over-exploration: {len(files)} files touched, only {edit_events} edits."
        )

    return {
        "loop_suspected": loop_suspected,
        "shallow_exploration": shallow,
        "backtrack_evidence": backtrack,
        "over_exploration": over_exploration,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Summary entry point
# ---------------------------------------------------------------------------


def summarize(root: Path, *, limit: int = 10) -> dict[str, Any]:
    """Return per-session efficiency+failure analysis for recent trajectories."""
    extracted = extract_trajectories(root, limit=limit)
    trajectories = extracted.get("trajectories") or []
    summary = []
    for traj in trajectories:
        summary.append(
            {
                "session_id": traj["session_id"],
                "efficiency": analyze_efficiency(traj),
                "failures": analyze_failures(traj),
            }
        )
    return {
        "ok": True,
        "summary": summary,
        "total_sessions": len(summary),
    }


__all__ = [
    "extract_trajectories",
    "analyze_efficiency",
    "analyze_failures",
    "summarize",
]
