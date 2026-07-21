from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 900


def registry_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "child-processes.jsonl"


def register_child(root: Path, *, pid: int, kind: str, command: list[str]) -> None:
    if pid <= 0:
        return
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pid": int(pid),
        "kind": str(kind)[:64],
        "command": [str(part)[:240] for part in command[:12]],
        "created_at": time.time(),
    }
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        return


def cleanup_children(root: Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    path = registry_path(root)
    if not path.exists():
        return {"ok": True, "checked": 0, "killed": 0, "alive": 0}
    now = time.time()
    checked = killed = alive = 0
    kept: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "reason": "registry_unreadable"}
    for line in lines[-200:]:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        pid = int(record.get("pid") or 0)
        if pid <= 0:
            continue
        checked += 1
        if not _pid_alive(pid):
            continue
        age = now - float(record.get("created_at") or now)
        if age >= ttl_seconds:
            if _terminate(pid):
                killed += 1
                continue
        alive += 1
        kept.append(record)
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in kept[-100:]),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass
    return {"ok": True, "checked": checked, "killed": killed, "alive": alive}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
