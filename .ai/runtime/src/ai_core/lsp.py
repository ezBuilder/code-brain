"""LSP-grade symbol navigation (PoC).

Wraps `multilspy` (Microsoft, MIT) to expose precise reference resolution,
goto-definition, and workspace symbol lookup as a graceful, optional layer
on top of the existing heuristic codegraph.

Design constraints:
  - `multilspy` is an OPTIONAL dependency. Import is wrapped in try/except so
    this module always loads even when the extra isn't installed.
  - All public functions return a `dict` shape with at least `ok: bool` and
    a `reason: str` when unavailable. They never raise.
  - A small TTL memory cache (5s) is kept for `find_references` keyed by
    (root, file_path, line, column). Larger persistent caches are out of
    scope for this PoC.

The actual `multilspy.SyncLanguageServer` usage is intentionally NOT
implemented in this PoC — we only ship the detection layer, the API
contract, the cache scaffold, and shape-stable responses. A follow-up
round wires the real LSP calls behind the same surface.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from threading import RLock
from typing import Any

# Optional dep: multilspy. We import lazily and never raise on absence.
try:  # pragma: no cover - exercised by absence test
    import multilspy  # type: ignore[import-not-found]

    _MULTILSPY_AVAILABLE = True
except Exception:  # noqa: BLE001 - any failure means "not usable"
    multilspy = None  # type: ignore[assignment]
    _MULTILSPY_AVAILABLE = False


# Known language server binaries we probe via PATH.
# Order is significant only for the returned `servers_detected` list.
_LANGUAGE_SERVERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("pyright-langserver", "pyright", "pylsp")),
    ("go", ("gopls",)),
    ("typescript", ("typescript-language-server",)),
    ("rust", ("rust-analyzer",)),
    ("c_cpp", ("clangd",)),
)


# ---------------------------------------------------------------------------
# Cache scaffold
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 5.0
_cache_lock = RLock()
_references_cache: dict[tuple[str, str, int, int], tuple[float, dict[str, Any]]] = {}


def _cache_get(key: tuple[str, str, int, int]) -> dict[str, Any] | None:
    with _cache_lock:
        item = _references_cache.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            _references_cache.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[str, str, int, int], value: dict[str, Any]) -> None:
    with _cache_lock:
        _references_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)


def _cache_clear() -> None:
    """Test helper — drop all cached entries."""
    with _cache_lock:
        _references_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_servers() -> list[str]:
    """Return the list of LSP server binaries currently visible on PATH."""
    found: list[str] = []
    for _lang, candidates in _LANGUAGE_SERVERS:
        for binary in candidates:
            if shutil.which(binary):
                found.append(binary)
                break  # one per language is enough
    return found


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "reason": reason}
    payload.update(extra)
    return payload


def _normalise_root(root: Path) -> Path:
    try:
        return root.resolve()
    except OSError:
        return root


# ---------------------------------------------------------------------------
# Real backend (multilspy, per-call, Python only). Wired behind lsp_available so
# the unavailable contract is unchanged. No daemon, no hooks — explicit calls only.
# OmO ships a TS unix-socket LSP daemon; that design is reimplemented here per-call,
# not ported. A warm daemon is intentionally out of scope (cold-start would flake the
# hot-path SLO if ever wired into a hook).
# ---------------------------------------------------------------------------


def _line_preview(root: Path, rel_or_abs_path: str, line: int) -> str:
    """Best-effort source line at `line` (0-indexed) for a result location. Never raises."""
    try:
        p = Path(rel_or_abs_path)
        if not p.is_absolute():
            p = Path(root) / p
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if 0 <= line < len(text):
            return text[line].strip()[:200]
    except OSError:
        pass
    return ""


def _map_location(loc: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Map a multilspy Location dict → {path, line, column, preview}. Pure; None if unusable."""
    if not isinstance(loc, dict):
        return None
    path = loc.get("relativePath") or loc.get("absolutePath") or ""
    if not path and isinstance(loc.get("uri"), str):
        path = loc["uri"].removeprefix("file://")
    rng = loc.get("range") if isinstance(loc.get("range"), dict) else {}
    start = rng.get("start") if isinstance(rng.get("start"), dict) else {}
    line = int(start.get("line", 0) or 0)
    column = int(start.get("character", 0) or 0)
    if not path:
        return None
    return {"path": str(path), "line": line, "column": column,
            "preview": _line_preview(root, str(path), line)}


def _lsp_call(root: Path, file_path: str, line: int, column: int, *, kind: str) -> list[dict[str, Any]] | None:
    """Per-call multilspy query (Python/pyright). Returns raw locations, or None on any failure."""
    try:
        from multilspy import SyncLanguageServer
        from multilspy.multilspy_config import MultilspyConfig
        from multilspy.multilspy_logger import MultilspyLogger

        config = MultilspyConfig.from_dict({"code_language": "python"})
        server = SyncLanguageServer.create(config, MultilspyLogger(), str(_normalise_root(root)))
        with server.start_server():
            if kind == "references":
                raw = server.request_references(file_path, line, column)
            else:
                raw = server.request_definition(file_path, line, column)
        return list(raw) if raw else []
    except Exception:  # noqa: BLE001 — any backend failure degrades to fallback, never raises
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lsp_available(root: Path) -> dict[str, Any]:
    """Probe whether LSP-grade navigation is usable from `root`.

    Returns a dict with the keys:
      ok                : True iff multilspy is importable AND at least one
                          known language server binary is on PATH.
      reason            : Short machine-readable cause when ok=False.
                          One of: 'multilspy_not_installed',
                          'no_language_server_on_path'.
      servers_detected  : List of binaries found on PATH (possibly empty).
    """
    servers = _detect_servers()
    if not _MULTILSPY_AVAILABLE:
        return {
            "ok": False,
            "reason": "multilspy_not_installed",
            "servers_detected": servers,
        }
    if not servers:
        return {
            "ok": False,
            "reason": "no_language_server_on_path",
            "servers_detected": servers,
        }
    return {
        "ok": True,
        "reason": "",
        "servers_detected": servers,
        "root": _normalise_root(root).as_posix(),
    }


def find_references(
    root: Path,
    file_path: str,
    line: int,
    column: int,
) -> dict[str, Any]:
    """Find all references to the symbol at `(line, column)` in `file_path`.

    `file_path` is interpreted relative to `root`. `line` and `column` follow
    the LSP convention (0-indexed).

    Response shape (always):
      {
        "ok": bool,
        "references": [
            {"path": str, "line": int, "column": int, "preview": str},
            ...
        ],
        "reason"?: str,
      }

    When the LSP layer is unavailable the function returns ok=False with a
    `reason` field; `references` is always present (empty list).
    """
    avail = lsp_available(root)
    if not avail["ok"]:
        return {
            "ok": False,
            "reason": avail["reason"],
            "references": [],
        }

    cache_key = (_normalise_root(root).as_posix(), file_path, int(line), int(column))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    raw = _lsp_call(root, file_path, int(line), int(column), kind="references")
    if raw is None:
        return {"ok": False, "reason": "lsp_query_failed", "references": []}
    refs = [m for loc in raw if (m := _map_location(loc, root)) is not None]
    result: dict[str, Any] = {"ok": True, "references": refs}
    _cache_put(cache_key, result)
    return result


def goto_definition(
    root: Path,
    file_path: str,
    line: int,
    column: int,
) -> dict[str, Any]:
    """Locate the definition for the symbol at `(line, column)`.

    Response shape (always):
      {
        "ok": bool,
        "definition": {"path": str, "line": int, "column": int, "preview": str} | None,
        "reason"?: str,
      }
    """
    avail = lsp_available(root)
    if not avail["ok"]:
        return {
            "ok": False,
            "reason": avail["reason"],
            "definition": None,
        }
    raw = _lsp_call(root, file_path, int(line), int(column), kind="definition")
    if raw is None:
        return {"ok": False, "reason": "lsp_query_failed", "definition": None}
    definition: dict[str, Any] | None = None
    for loc in raw:
        definition = _map_location(loc, root)
        if definition is not None:
            break
    return {"ok": True, "definition": definition}


def workspace_symbols(
    root: Path,
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Fuzzy lookup of workspace-wide symbols matching `query`.

    Response shape (always):
      {
        "ok": bool,
        "symbols": [
            {"name": str, "kind": str, "path": str, "line": int,
             "container"?: str},
            ...
        ],
        "reason"?: str,
      }

    `limit` caps the returned list. It is honoured regardless of whether the
    LSP backend is wired up.
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 20
    if cap < 0:
        cap = 0

    avail = lsp_available(root)
    if not avail["ok"]:
        return {
            "ok": False,
            "reason": avail["reason"],
            "symbols": [],
        }

    # PoC: backend not wired. Return an empty list (already within `cap`).
    symbols: list[dict[str, Any]] = []
    return {
        "ok": True,
        "symbols": symbols[:cap],
        "reason": "lsp_backend_not_wired",
    }


__all__ = [
    "lsp_available",
    "find_references",
    "goto_definition",
    "workspace_symbols",
]
