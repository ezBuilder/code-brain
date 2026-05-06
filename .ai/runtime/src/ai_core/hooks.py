from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .memory import append_event
from .policy import is_ci
from .redact import redact_value

HOT_PATH_TARGET_MS = 200


def read_payload(stdin: str | None = None) -> dict[str, Any]:
    raw = stdin if stdin is not None else sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def handle_hook(root: Path, hook_name: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    effective_hook = hook_name or payload.get("hook") or payload.get("event") or "unknown"
    event = {"hook": effective_hook, **payload}
    if is_ci():
        mode = "ci-fast-path"
        persisted = False
    else:
        append_event(root, event)
        mode = "local-append"
        persisted = True
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response = {
        "ok": True,
        "hook": effective_hook,
        "mode": mode,
        "persisted": persisted,
        "elapsed_ms": elapsed_ms,
        "target_ms": HOT_PATH_TARGET_MS,
        "additionalContext": build_context(effective_hook, payload),
    }
    return redact_value(response)


def build_context(hook_name: str, payload: dict[str, Any]) -> str:
    agent = payload.get("agent", "unknown")
    return f"Code Brain fast_path: hook={hook_name}, agent={agent}, network=off, writes={'off' if is_ci() else 'worker-local'}."

