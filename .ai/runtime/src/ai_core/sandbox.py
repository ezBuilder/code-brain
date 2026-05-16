from __future__ import annotations

import json
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import is_ci
from .redact import redact_value

_SANDBOX_DIR = (".ai", "cache", "sandbox")
_SUMMARY_BUDGET_BYTES = 4096
_FIRST_LINES_DEFAULT = 30
_LAST_LINES_DEFAULT = 5
_STDERR_TAIL_DEFAULT = 10
_FETCH_LINE_CAP = 200
_FETCH_DEFAULT_WINDOW = 100
_FETCH_GREP_CAP = 200
_COMPACT_MAX_LINES = 20
_COMPACT_MAX_BYTES = 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sandbox_dir(root: Path) -> Path:
    return root.joinpath(*_SANDBOX_DIR)


def _ensure_dir(root: Path) -> Path:
    sandbox = _sandbox_dir(root)
    sandbox.mkdir(parents=True, exist_ok=True)
    return sandbox


def _write_secure(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _trim_first_lines(first_lines: list[str], last_lines: list[str]) -> list[str]:
    """Cap first_lines so first+last combined stays under the summary budget."""
    last_len = sum(len(line) + 1 for line in last_lines)
    budget = max(0, _SUMMARY_BUDGET_BYTES - last_len)
    trimmed: list[str] = []
    used = 0
    for line in first_lines:
        cost = len(line) + 1
        if used + cost > budget:
            break
        trimmed.append(line)
        used += cost
    return trimmed


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _maybe_audit(root: Path, payload: dict[str, Any]) -> None:
    if is_ci():
        return
    try:
        from .memory import append_event

        append_event(root, payload)
    except Exception:
        # Audit append must never break execute path.
        pass


def execute(
    root: Path,
    *,
    command: list[str] | str,
    cwd: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    # Accept either an argv list or a shell string. String form runs under `bash -lc`
    # so that heredocs, quoting and pipes work without re-escaping into a JSON array.
    if isinstance(command, str):
        if not command.strip():
            return redact_value({"ok": False, "reason": "empty_command"})
        cmd_argv = ["bash", "-lc", command]
    else:
        if not command:
            return redact_value({"ok": False, "reason": "empty_command"})
        cmd_argv = list(command)

    exec_id = secrets.token_hex(8)
    work_cwd = cwd if cwd is not None else str(root)

    try:
        completed = subprocess.run(
            cmd_argv,
            cwd=work_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return redact_value({"ok": False, "reason": "timeout", "exec_id": exec_id})
    except FileNotFoundError:
        return redact_value({"ok": False, "reason": "command_not_found", "exec_id": exec_id})

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = stdout
    if stderr:
        combined = f"{stdout}{stderr}" if stdout.endswith("\n") or not stdout else f"{stdout}\n{stderr}"

    redacted_combined = redact_value(combined)
    redacted_stderr = redact_value(stderr)

    sandbox_dir = _ensure_dir(root)
    out_path = sandbox_dir / f"{exec_id}.txt"
    _write_secure(out_path, redacted_combined)

    all_lines = redacted_combined.splitlines()
    total_lines = len(all_lines)
    total_bytes = len(redacted_combined.encode("utf-8"))
    stderr_bytes = len(redacted_stderr.encode("utf-8"))

    first_lines = all_lines[:_FIRST_LINES_DEFAULT]
    last_lines = all_lines[-_LAST_LINES_DEFAULT:] if total_lines > _FIRST_LINES_DEFAULT else []

    stderr_tail: list[str] = []
    if redacted_stderr:
        stderr_lines = redacted_stderr.splitlines()
        stderr_tail = stderr_lines[-_STDERR_TAIL_DEFAULT:]

    first_lines = _trim_first_lines(first_lines, last_lines)

    created_at = _now_iso()

    meta = {
        "exec_id": exec_id,
        "command": list(cmd_argv),
        "cwd": work_cwd,
        "exit_code": completed.returncode,
        "total_bytes": total_bytes,
        "total_lines": total_lines,
        "created_at": created_at,
        "stderr_bytes": stderr_bytes,
    }
    meta_path = sandbox_dir / f"{exec_id}.meta.json"
    _write_secure(meta_path, json.dumps(meta, ensure_ascii=False, sort_keys=True))

    # Compact mode: when the full output is short, return it raw and skip the
    # first_lines/last_lines split. Saves model-side tokens for small results.
    is_compact = total_lines <= _COMPACT_MAX_LINES and total_bytes <= _COMPACT_MAX_BYTES
    summary: dict[str, Any] = {
        "ok": True,
        "exec_id": exec_id,
        "exit_code": completed.returncode,
        "total_bytes": total_bytes,
        "total_lines": total_lines,
        "stderr_tail": stderr_tail,
        "created_at": created_at,
    }
    if is_compact:
        summary["output"] = redacted_combined
    else:
        summary["first_lines"] = first_lines
        summary["last_lines"] = last_lines

    _maybe_audit(
        root,
        {
            "hook": "sandbox.execute",
            "exec_id": exec_id,
            "exit_code": completed.returncode,
            "total_bytes": total_bytes,
        },
    )

    return redact_value(summary)


def fetch(
    root: Path,
    *,
    exec_id: str,
    line_start: int = 1,
    line_end: int | None = None,
    grep_pattern: str | None = None,
) -> dict[str, Any]:
    sandbox_dir = _sandbox_dir(root)
    out_path = sandbox_dir / f"{exec_id}.txt"
    if not out_path.exists():
        return redact_value({"ok": False, "reason": "not_found", "exec_id": exec_id})

    text = out_path.read_text(encoding="utf-8")
    all_lines = text.splitlines()
    total_lines = len(all_lines)

    if grep_pattern is not None:
        needle = grep_pattern.lower()
        matched: list[dict[str, Any]] = []
        for idx, line in enumerate(all_lines, start=1):
            if needle in line.lower():
                matched.append({"lineno": idx, "text": line})
                if len(matched) >= _FETCH_GREP_CAP:
                    break
        result = {
            "ok": True,
            "exec_id": exec_id,
            "total_lines": total_lines,
            "matched_lines": matched,
            "pattern": grep_pattern,
        }
        return redact_value(result)

    if line_start < 1:
        line_start = 1
    if line_end is None:
        line_end = line_start + _FETCH_DEFAULT_WINDOW - 1
    if line_end < line_start:
        line_end = line_start

    start_idx = line_start - 1
    end_idx = line_end
    sliced = all_lines[start_idx:end_idx]
    if len(sliced) > _FETCH_LINE_CAP:
        sliced = sliced[:_FETCH_LINE_CAP]
        line_end = line_start + len(sliced) - 1

    lines_payload = [{"lineno": line_start + offset, "text": line} for offset, line in enumerate(sliced)]

    result = {
        "ok": True,
        "exec_id": exec_id,
        "total_lines": total_lines,
        "line_start": line_start,
        "line_end": line_end,
        "lines": lines_payload,
    }
    return redact_value(result)


def list_executions(root: Path, *, limit: int = 20) -> dict[str, Any]:
    sandbox_dir = _sandbox_dir(root)
    if not sandbox_dir.exists():
        return redact_value({"ok": True, "count": 0, "items": []})

    metas: list[dict[str, Any]] = []
    for meta_path in sandbox_dir.glob("*.meta.json"):
        meta = _read_meta(meta_path)
        if meta is None:
            continue
        metas.append(meta)

    metas.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    metas = metas[: max(0, int(limit))]

    items = []
    for meta in metas:
        command = meta.get("command") or []
        if isinstance(command, list):
            command_summary = " ".join(str(part) for part in command)[:80]
        else:
            command_summary = str(command)[:80]
        items.append(
            {
                "exec_id": meta.get("exec_id"),
                "exit_code": meta.get("exit_code"),
                "total_bytes": meta.get("total_bytes"),
                "total_lines": meta.get("total_lines"),
                "created_at": meta.get("created_at"),
                "command_summary": command_summary,
            }
        )

    return redact_value({"ok": True, "count": len(items), "items": items})


def prune(root: Path, *, older_than_seconds: int = 86400) -> dict[str, Any]:
    sandbox_dir = _sandbox_dir(root)
    if not sandbox_dir.exists():
        return redact_value({"ok": True, "removed_count": 0, "kept_count": 0})

    now = datetime.now(timezone.utc)
    threshold = float(older_than_seconds)
    removed = 0
    kept = 0

    for meta_path in list(sandbox_dir.glob("*.meta.json")):
        meta = _read_meta(meta_path)
        if meta is None:
            kept += 1
            continue
        created_at = meta.get("created_at")
        age_seconds: float | None = None
        if isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_seconds = (now - created_dt).total_seconds()
            except ValueError:
                age_seconds = None
        if age_seconds is None:
            try:
                mtime = meta_path.stat().st_mtime
                age_seconds = now.timestamp() - mtime
            except OSError:
                age_seconds = None

        if age_seconds is not None and age_seconds >= threshold:
            exec_id = meta.get("exec_id") or meta_path.stem.replace(".meta", "")
            txt_path = sandbox_dir / f"{exec_id}.txt"
            try:
                meta_path.unlink()
            except OSError:
                pass
            try:
                if txt_path.exists():
                    txt_path.unlink()
            except OSError:
                pass
            removed += 1
        else:
            kept += 1

    return redact_value({"ok": True, "removed_count": removed, "kept_count": kept})
