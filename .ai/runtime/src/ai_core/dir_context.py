"""Read-triggered walk-up directory context (G9, OmO agents-md-core inspired) — opt-in.

Hand-written nested AGENTS.md/CLAUDE.md only reach the agent at SessionStart as the cwd-bucket
nearest file; when the agent edits deep in a subtree, the guidance living next to *that* code is
invisible. This walks up from the file just Read, collecting the AGENTS.md/CLAUDE.md chain between
the file and the repo root, and surfaces any not seen yet this session.

Language-agnostic, offline, demand-driven, realpath-sealed (never escapes the repo root), and
deduped per session so the same directory is injected once. skipRoot=True because the root file is
already surfaced at SessionStart. stdlib only; no LLM, no network. Default ON (read-only/advisory;
disable with AI_DIR_CONTEXT=0).

Caveat (pilot): this relies on the host actually consuming PostToolUse `additionalContext`. Verify
empirically per host before trusting it as load-bearing.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .private_write import atomic_write_private_text, read_root_confined_text

CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")
MAX_DEPTH = 12
DEFAULT_CHAR_CAP = 4000
_PER_FILE_CAP = 1500
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


def enabled() -> bool:
    """Read-triggered directory-context gate. Default ON (read-only/advisory); disable
    with AI_DIR_CONTEXT=0. UNSET/empty = ON; only an explicit 0/false/no turns it off."""
    return str(os.environ.get("AI_DIR_CONTEXT", "")).strip().lower() not in ("0", "false", "no")


def _realpath(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def find_context_files(root: Path, file_path: str, *, max_depth: int = MAX_DEPTH) -> list[Path]:
    """AGENTS.md/CLAUDE.md between the Read file's dir and the repo root (nearest-first).

    Realpath-sealed: only dirs inside root are walked; the root dir itself is skipped (skipRoot)
    because its context is already injected at SessionStart. Deduped by realpath.
    """
    fp = str(file_path or "").strip()
    if not fp:
        return []
    root_rp = _realpath(Path(root))
    target = Path(fp)
    if not target.is_absolute():
        target = Path(root) / target
    cur = _realpath(target.parent if not target.is_dir() else target)
    found: list[Path] = []
    seen_dirs: set[str] = set()
    for _ in range(max_depth):
        # containment: stop once we leave the repo root, and skip the root dir itself (skipRoot)
        if cur == root_rp:
            break
        try:
            cur.relative_to(root_rp)
        except ValueError:
            break
        key = str(cur)
        if key in seen_dirs:
            break
        seen_dirs.add(key)
        for name in CONTEXT_FILENAMES:
            candidate = cur / name
            try:
                candidate_state = candidate.lstat()
            except OSError:
                continue
            if not (stat.S_ISREG(candidate_state.st_mode) or stat.S_ISLNK(candidate_state.st_mode)):
                continue
            rp = _realpath(candidate)
            try:
                rp.relative_to(root_rp)
                resolved_state = rp.lstat()
            except (OSError, ValueError):
                continue
            if not stat.S_ISREG(resolved_state.st_mode) or stat.S_ISLNK(resolved_state.st_mode):
                continue
            if rp not in found:
                found.append(rp)
            break  # one root-confined context file per directory
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return found


def _seen_path(root: Path, sid: str) -> Path:
    safe = _SID_RE.sub("_", sid)[:64] or "default"
    return Path(root) / ".ai" / "cache" / "dir_context" / f"{safe}.json"


def _load_seen(root: Path, sid: str) -> set[str]:
    try:
        text, _state = read_root_confined_text(
            _seen_path(root, sid),
            root=root,
            max_bytes=1_000_000,
            require_private=True,
        )
        data = json.loads(text)
        return set(data) if isinstance(data, list) else set()
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return set()


def _save_seen(root: Path, sid: str, seen: set[str]) -> None:
    path = _seen_path(root, sid)
    try:
        atomic_write_private_text(
            path,
            json.dumps(sorted(seen)[-500:]),
            root=root,
        )
    except OSError:
        pass


def directory_context_for_read(root: Path, payload: dict[str, Any], *,
                               char_cap: int = DEFAULT_CHAR_CAP) -> str:
    """Block of not-yet-seen directory context for the file in a Read PostToolUse payload.

    Returns "" when disabled, not a Read, no nested context, or everything already surfaced this
    session. Per-session dedup is persisted so each directory is injected at most once. Fail-soft.
    """
    try:
        if not enabled() or not isinstance(payload, dict):
            return ""
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
        if tool_name and tool_name.lower() != "read":
            return ""
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        file_path = str(tool_input.get("file_path") or tool_input.get("path")
                        or payload.get("file_path") or payload.get("path") or "")
        if not file_path:
            return ""
        files = find_context_files(root, file_path)
        if not files:
            return ""
        sid = str(payload.get("session_id") or payload.get("sid") or "default")
        seen = _load_seen(root, sid)
        fresh = [f for f in files if str(f) not in seen]
        if not fresh:
            return ""
        from .redact import redact_value
        parts: list[str] = []
        total = 0
        for f in fresh:
            try:
                body, _state = read_root_confined_text(
                    f,
                    root=root,
                    max_bytes=1_000_000,
                    require_private=False,
                )
                body = body.strip()
            except (OSError, UnicodeDecodeError):
                continue
            body = str(redact_value(body))[:_PER_FILE_CAP]
            try:
                rel = f.parent.relative_to(_realpath(Path(root))).as_posix() or "."
            except ValueError:
                rel = f.parent.name
            block = f"[Directory Context: {rel}/{f.name}]\n{body}"
            if total + len(block) > char_cap and parts:
                break
            parts.append(block)
            total += len(block)
            seen.add(str(f))
        if not parts:
            return ""
        _save_seen(root, sid, seen)
        return "\n\n".join(parts)
    except Exception:
        return ""
