"""MCP config dialect conversions across Claude Code, Codex CLI, and Antigravity.

Each agent reads MCP server definitions from a different file with subtly
different field names:

  - Claude Code           : ``.mcp.json``                    ``mcpServers`` + ``url``
  - Codex CLI             : ``.codex/config.toml``           ``[mcp_servers.<name>]``
  - Antigravity (Gemini)  : ``.agents/mcp_config.json``      ``mcpServers`` + ``serverUrl``

The functions here are pure (no I/O) except for ``merge_antigravity_mcp_json``
and ``merge_into_target`` which write to disk. They preserve unrelated user
entries and only overwrite the Code Brain managed server, so re-running
install-into is idempotent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CODE_BRAIN_SERVER_NAME = "code-brain"


def code_brain_stdio_entry() -> dict[str, Any]:
    """Standard Code Brain MCP server entry for stdio transport."""
    return {"command": ".ai/bin/ai-mcp", "args": [], "env": {}}


def _normalize_remote_url_keys(server: dict[str, Any], *, target_key: str) -> dict[str, Any]:
    """Move ``url`` ↔ ``serverUrl`` so the result uses ``target_key`` only.

    Antigravity rejects ``url`` and requires ``serverUrl``; Claude/Cursor use
    ``url``. We only touch remote-style entries — stdio entries (with
    ``command``) are returned unchanged.
    """
    if not isinstance(server, dict) or "command" in server:
        return server
    out = dict(server)
    has_url = "url" in out
    has_server_url = "serverUrl" in out
    if target_key == "serverUrl" and has_url and not has_server_url:
        out["serverUrl"] = out.pop("url")
    elif target_key == "url" and has_server_url and not has_url:
        out["url"] = out.pop("serverUrl")
    return out


def to_antigravity(claude_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a ``.mcp.json``-shaped payload to ``mcp_config.json`` shape.

    Antigravity uses the same top-level ``mcpServers`` key but requires
    ``serverUrl`` instead of ``url`` for remote servers.
    """
    if not isinstance(claude_payload, dict):
        return {"mcpServers": {}}
    servers = claude_payload.get("mcpServers")
    if not isinstance(servers, dict):
        return {"mcpServers": {}}
    out: dict[str, Any] = {}
    for name, entry in servers.items():
        if isinstance(entry, dict):
            out[name] = _normalize_remote_url_keys(entry, target_key="serverUrl")
        else:
            out[name] = entry
    return {"mcpServers": out}


def from_antigravity(antigravity_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert an Antigravity ``mcp_config.json`` payload back to ``.mcp.json`` shape."""
    if not isinstance(antigravity_payload, dict):
        return {"mcpServers": {}}
    servers = antigravity_payload.get("mcpServers")
    if not isinstance(servers, dict):
        return {"mcpServers": {}}
    out: dict[str, Any] = {}
    for name, entry in servers.items():
        if isinstance(entry, dict):
            out[name] = _normalize_remote_url_keys(entry, target_key="url")
        else:
            out[name] = entry
    return {"mcpServers": out}


def merge_into_target(target: Path, dialect: str, server_name: str, server_entry: dict[str, Any]) -> None:
    """Idempotently merge a single server entry into a target JSON file.

    Used for ``.mcp.json`` (dialect=``claude``) and
    ``.agents/mcp_config.json`` (dialect=``antigravity``). For Antigravity,
    remote-style ``url`` fields in ``server_entry`` are rewritten to
    ``serverUrl`` automatically.
    """
    if dialect not in {"claude", "antigravity"}:
        raise ValueError(f"unsupported dialect: {dialect!r}")
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"existing {target} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"existing {target} is not a JSON object")
    else:
        payload = {}
    servers = payload.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"existing {target}.mcpServers must be a JSON object")
    entry = dict(server_entry)
    if dialect == "antigravity":
        entry = _normalize_remote_url_keys(entry, target_key="serverUrl")
    servers[server_name] = entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def merge_antigravity_mcp_json(target: Path, server_entry: dict[str, Any] | None = None) -> None:
    """Merge the Code Brain stdio server entry into ``.agents/mcp_config.json``."""
    merge_into_target(
        target=target,
        dialect="antigravity",
        server_name=CODE_BRAIN_SERVER_NAME,
        server_entry=server_entry if server_entry is not None else code_brain_stdio_entry(),
    )


def antigravity_global_mcp_path(home: Path | None = None) -> Path:
    """Resolve the canonical global ``mcp_config.json`` path for Antigravity CLI.

    Antigravity 1.0.x persists user-global MCP servers at
    ``~/.gemini/antigravity/mcp_config.json``; this helper centralizes that
    location so install-into.sh, setup helpers, and tests agree.
    """
    base = home if home is not None else Path.home()
    return base / ".gemini" / "antigravity" / "mcp_config.json"


def install_global_antigravity_mcp(
    wrapper_path: Path,
    *,
    home: Path | None = None,
) -> Path:
    """Register the Code Brain MCP wrapper in the user-global Antigravity config.

    Antigravity 1.0.x does NOT yet read ``.agents/mcp_config.json`` from each
    workspace, so multi-project MCP only works when the entry lives in the
    user-global file *and* points at a wrapper that walks up from the spawning
    cwd to find that workspace's ``.ai/bin/ai-mcp``.

    Returns the resolved global config path so callers can log / chown it.
    """
    target = antigravity_global_mcp_path(home=home)
    merge_into_target(
        target=target,
        dialect="antigravity",
        server_name=CODE_BRAIN_SERVER_NAME,
        server_entry={"command": str(wrapper_path), "args": [], "env": {}},
    )
    return target
