from __future__ import annotations

from pathlib import Path
from typing import Any

from .redact import redact_value
from .worker.scheduler import enqueue

ALLOWED_CHANNELS = {"slack", "telegram", "smtp", "pagerduty", "webhook", "stdout"}


def enqueue_notification(root: Path, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
    if channel not in ALLOWED_CHANNELS:
        raise ValueError(f"channel must be one of: {', '.join(sorted(ALLOWED_CHANNELS))}")
    sanitized = {
        "channel": channel,
        "category": payload.get("category", "notification"),
        "summary": payload.get("summary", ""),
        "body": redact_value(payload.get("body", "")),
    }
    result = enqueue(root, "P3", "notify", sanitized)
    return {"ok": True, "channel": channel, "queued": result["job"]}

