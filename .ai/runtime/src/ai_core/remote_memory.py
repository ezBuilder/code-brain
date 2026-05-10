from __future__ import annotations

import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import load_config
from .memory import append_audit, now_iso
from .redact import redact_value

SCOPES = {"global", "project"}
DEFAULT_TIMEOUT_SECONDS = 10


def _remote_config(root: Path) -> dict[str, Any]:
    config = load_config(root)
    features = config.get("features", {}) if isinstance(config.get("features"), dict) else {}
    remote = config.get("remote_memory", {}) if isinstance(config.get("remote_memory"), dict) else {}
    return {
        "enabled": bool(features.get("remote_memory", False)),
        "provider": str(remote.get("provider") or "cloudflare"),
        "inject_on_session_start": bool(remote.get("inject_on_session_start", False)),
        "default_scope": str(remote.get("default_scope") or "project"),
        "cache_path": str(remote.get("cache_path") or ".ai/cache/remote-memory/summary.json"),
    }


def current_project_id(root: Path) -> str:
    config = load_config(root)
    name = str(config.get("project_name") or "").strip()
    return name or root.name


def repo_url(root: Path) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def cache_path(root: Path) -> Path:
    cfg = _remote_config(root)
    raw = Path(cfg["cache_path"])
    return raw if raw.is_absolute() else root / raw


def _local_env_values(root: Path) -> dict[str, str]:
    path = root / ".ai" / "cache" / "remote-memory" / "cloudflare.env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        return {}
    return values


def _base_url(root: Path) -> str:
    return (os.environ.get("AI_REMOTE_MEMORY_URL") or _local_env_values(root).get("AI_REMOTE_MEMORY_URL", "")).rstrip("/")


def _token(root: Path) -> str:
    return os.environ.get("AI_REMOTE_MEMORY_TOKEN") or _local_env_values(root).get("AI_REMOTE_MEMORY_TOKEN", "")


def _configured(root: Path) -> tuple[bool, str | None]:
    if not _base_url(root):
        return False, "AI_REMOTE_MEMORY_URL is not set"
    if not _token(root):
        return False, "AI_REMOTE_MEMORY_TOKEN is not set"
    return True, None


def status(root: Path) -> dict[str, Any]:
    cfg = _remote_config(root)
    configured, reason = _configured(root)
    return {
        "ok": bool(cfg["enabled"] and configured),
        "enabled": cfg["enabled"],
        "configured": configured,
        "reason": reason,
        "provider": cfg["provider"],
        "project_id": current_project_id(root),
        "inject_on_session_start": cfg["inject_on_session_start"],
        "cache_path": cache_path(root).relative_to(root).as_posix(),
    }


def _require_enabled(root: Path) -> None:
    cfg = _remote_config(root)
    if not cfg["enabled"]:
        raise RuntimeError("remote_memory feature is disabled")
    configured, reason = _configured(root)
    if not configured:
        raise RuntimeError(reason or "remote memory is not configured")


def _http_json(root: Path, path: str, payload: dict[str, Any] | None = None, *, method: str = "POST") -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _base_url(root) + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {_token(root)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "code-brain-remote-memory/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS, context=_ssl_context()) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"remote memory HTTP {exc.code}: {detail[:300]}") from exc
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise RuntimeError("remote memory returned non-object JSON")
    return redact_value(data)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _normalize_scope(scope: str | None, root: Path) -> str:
    chosen = (scope or _remote_config(root)["default_scope"]).strip().lower()
    if chosen not in SCOPES:
        raise ValueError("scope must be global or project")
    return chosen


def _clean_tags(tags: list[str] | None) -> list[str]:
    return sorted({str(tag).strip()[:64] for tag in (tags or []) if str(tag).strip()})


def _redact_or_reject(text: str) -> str:
    raw = str(text).strip()
    redacted = str(redact_value(raw)).strip()
    if not redacted:
        raise ValueError("text is empty")
    if redacted != raw:
        raise ValueError("remote memory rejected secret-like content after redaction")
    return redacted


def remember(
    root: Path,
    *,
    text: str,
    scope: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    source_agent: str = "operator",
    source_surface: str = "cli",
) -> dict[str, Any]:
    _require_enabled(root)
    content = _redact_or_reject(text)
    project_id = str(project or current_project_id(root)).strip()
    chosen_scope = _normalize_scope(scope, root)
    payload = {
        "content": content,
        "scope": chosen_scope,
        "project_id": project_id,
        "repo_url": repo_url(root),
        "tags": _clean_tags(tags),
        "source_agent": source_agent[:64],
        "source_surface": source_surface[:64],
        "sensitivity": "internal",
        "dedupe_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    result = _http_json(root, "/capture", payload)
    append_audit(
        root,
        action="remote_memory.remember",
        category="memory",
        payload={"id": result.get("id"), "scope": chosen_scope, "project_id": project_id},
    )
    return result


def recall(
    root: Path,
    *,
    query: str,
    project: str | None = None,
    top_k: int = 5,
    include_cross_project: bool = False,
    scope: str | None = None,
) -> dict[str, Any]:
    _require_enabled(root)
    query_text = _redact_or_reject(query)
    project_id = str(project or current_project_id(root)).strip()
    payload = {
        "query": query_text,
        "topK": max(1, min(20, int(top_k))),
        "project_id": project_id,
        "include_cross_project": bool(include_cross_project),
    }
    if scope:
        payload["scope"] = _normalize_scope(scope, root)
    result = _http_json(root, "/recall", payload)
    append_audit(
        root,
        action="remote_memory.recall",
        category="memory",
        payload={"project_id": project_id, "include_cross_project": include_cross_project, "count": len(result.get("matches", []))},
    )
    return result


def list_recent(root: Path, *, project: str | None = None, n: int = 10, include_cross_project: bool = False) -> dict[str, Any]:
    _require_enabled(root)
    project_id = str(project or current_project_id(root)).strip()
    path = f"/list?n={max(1, min(50, int(n)))}&project_id={urllib.parse.quote(project_id)}"
    if include_cross_project:
        path += "&include_cross_project=1"
    return _http_json(root, path, None, method="GET")


def forget(root: Path, *, entry_id: str) -> dict[str, Any]:
    _require_enabled(root)
    entry = str(entry_id).strip()
    if not entry:
        raise ValueError("entry_id is required")
    result = _http_json(root, "/forget", {"id": entry})
    append_audit(root, action="remote_memory.forget", category="memory", payload={"id": entry})
    return result


def sync(root: Path, *, direction: str) -> dict[str, Any]:
    _require_enabled(root)
    if direction not in {"pull", "push"}:
        raise ValueError("direction must be pull or push")
    if direction == "push":
        return {"ok": False, "reason": "push sync is intentionally not automatic; use remote-memory remember for explicit writes"}
    recent = list_recent(root, n=10, include_cross_project=False)
    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": True,
        "synced_at": now_iso(),
        "project_id": current_project_id(root),
        "recent": recent.get("entries", recent),
    }
    path.write_text(json.dumps(redact_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "direction": "pull", "cache_path": path.relative_to(root).as_posix(), "count": len(payload["recent"])}
