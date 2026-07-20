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
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import unquote, urlparse

from .private_write import (
    read_root_confined_text,
    validate_root_confined_regular_file,
)

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
_CACHE_MAX_ENTRIES = 256
LSP_PATH_MAX_CHARS = 1024
LSP_QUERY_MAX_CHARS = 512
LSP_FILE_MAX_BYTES = 2 * 1024 * 1024
LSP_LINE_MAX = 10_000_000
LSP_COLUMN_MAX = 1_000_000
LSP_RESULT_MAX = 100
_cache_lock = RLock()
_CacheKey = tuple[Any, ...]
_SourceSignature = tuple[int, int, int, int, int, int]
_references_cache: dict[
    _CacheKey,
    tuple[float, dict[str, Any], dict[str, _SourceSignature]],
] = {}


def _cache_prune_expired(now: float) -> None:
    expired = [
        key
        for key, (expires_at, _value, _dependencies) in _references_cache.items()
        if expires_at < now
    ]
    for key in expired:
        _references_cache.pop(key, None)


def _source_signature(state: os.stat_result) -> _SourceSignature:
    return (
        int(state.st_dev),
        int(state.st_ino),
        int(state.st_size),
        int(state.st_mtime_ns),
        int(getattr(state, "st_ctime_ns", int(state.st_ctime * 1_000_000_000))),
        int(state.st_mode),
    )


def _trusted_source_stat(root: Path, rel_path: str) -> os.stat_result:
    return validate_root_confined_regular_file(
        Path(os.path.abspath(root)) / rel_path,
        root=Path(os.path.abspath(root)),
        min_bytes=0,
        max_bytes=LSP_FILE_MAX_BYTES,
        require_owner=True,
        reject_group_other_writable=True,
    )


def _cache_get(
    key: _CacheKey,
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    with _cache_lock:
        now = time.monotonic()
        _cache_prune_expired(now)
        item = _references_cache.pop(key, None)
        if item is None:
            return None
        expires_at, value, dependencies = item
        if expires_at < now:
            return None
        if root is not None:
            for rel_path, expected in dependencies.items():
                try:
                    current = _source_signature(_trusted_source_stat(root, rel_path))
                except OSError:
                    return None
                if current != expected:
                    return None
        _references_cache[key] = item
        return deepcopy(value)


def _cache_put(
    key: _CacheKey,
    value: dict[str, Any],
    *,
    dependencies: dict[str, _SourceSignature] | None = None,
) -> None:
    with _cache_lock:
        now = time.monotonic()
        _cache_prune_expired(now)
        _references_cache.pop(key, None)
        while len(_references_cache) >= _CACHE_MAX_ENTRIES:
            _references_cache.pop(next(iter(_references_cache)), None)
        _references_cache[key] = (
            now + _CACHE_TTL_SECONDS,
            deepcopy(value),
            dict(dependencies or {}),
        )


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


def _request_path_shape(file_path: object) -> tuple[str | None, str | None]:
    raw = str(file_path or "").strip()
    if not raw:
        return None, "empty_file_path"
    if "\x00" in raw:
        return None, "invalid_file_path_control_character"
    if len(raw) > LSP_PATH_MAX_CHARS:
        return None, "file_path_too_long"
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None, "file_path_outside_project"
    if path.suffix.casefold() != ".py":
        return None, "unsupported_language"
    return path.as_posix(), None


def _coerce_position(line: object, column: object) -> tuple[int | None, int | None, str | None]:
    if isinstance(line, bool) or isinstance(column, bool):
        return None, None, "invalid_position"
    try:
        line_value = int(line)
        column_value = int(column)
    except (TypeError, ValueError, OverflowError):
        return None, None, "invalid_position"
    if (
        line_value < 0
        or column_value < 0
        or line_value > LSP_LINE_MAX
        or column_value > LSP_COLUMN_MAX
    ):
        return None, None, "invalid_position"
    return line_value, column_value, None


def _trusted_project_source(
    root: Path,
    raw_path: str,
    *,
    allow_absolute: bool,
) -> tuple[str | None, str | None, os.stat_result | None, str | None]:
    raw_path = str(raw_path or "").strip()
    if not raw_path:
        return None, None, None, "empty_file_path"
    if "\x00" in raw_path:
        return None, None, None, "invalid_file_path_control_character"
    if len(raw_path) > LSP_PATH_MAX_CHARS:
        return None, None, None, "file_path_too_long"
    root_abs = Path(os.path.abspath(root))
    candidate = Path(raw_path)
    if candidate.is_absolute():
        if not allow_absolute:
            return None, None, None, "file_path_outside_project"
        candidate_abs = Path(os.path.abspath(candidate))
    else:
        if ".." in candidate.parts or not candidate.parts:
            return None, None, None, "file_path_outside_project"
        candidate_abs = Path(os.path.abspath(root_abs / candidate))
    try:
        rel = candidate_abs.relative_to(root_abs)
    except ValueError:
        return None, None, None, "file_path_outside_project"
    if not rel.parts:
        return None, None, None, "file_path_outside_project"
    try:
        text, state = read_root_confined_text(
            candidate_abs,
            root=root_abs,
            max_bytes=LSP_FILE_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except (OSError, UnicodeDecodeError):
        return None, None, None, "source_unavailable"
    return rel.as_posix(), text, state, None


def _trusted_project_text(
    root: Path,
    raw_path: str,
    *,
    allow_absolute: bool,
) -> tuple[str | None, str | None, str | None]:
    rel, text, _state, reason = _trusted_project_source(
        root,
        raw_path,
        allow_absolute=allow_absolute,
    )
    return rel, text, reason


def _validate_position_in_text(text: str, line: int, column: int) -> str | None:
    lines = text.splitlines()
    if not lines and text == "":
        lines = [""]
    if line >= len(lines):
        return "invalid_position"
    utf16_columns = len(lines[line].encode("utf-16-le")) // 2
    if column > utf16_columns:
        return "invalid_position"
    return None


def _location_raw_path(loc: dict[str, Any]) -> str:
    direct = loc.get("relativePath") or loc.get("absolutePath") or ""
    if direct:
        return str(direct)
    uri = loc.get("uri")
    if not isinstance(uri, str):
        return ""
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme.casefold() != "file":
        return ""
    return unquote(parsed.path)


# ---------------------------------------------------------------------------
# Real backend (multilspy, per-call, Python only). Wired behind lsp_available so
# the unavailable contract is unchanged. No daemon, no hooks — explicit calls only.
# OmO ships a TS unix-socket LSP daemon; that design is reimplemented here per-call,
# not ported. A warm daemon is intentionally out of scope (cold-start would flake the
# hot-path SLO if ever wired into a hook).
# ---------------------------------------------------------------------------


def _line_preview(root: Path, rel_or_abs_path: str, line: int) -> str:
    """Best-effort source line at `line` (0-indexed) for a result location. Never raises."""
    _rel, content, _reason = _trusted_project_text(
        root,
        rel_or_abs_path,
        allow_absolute=True,
    )
    if content is not None:
        lines = content.splitlines()
        if 0 <= line < len(lines):
            return lines[line].strip()[:200]
    return ""


def _map_location(loc: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Map a multilspy Location dict → {path, line, column, preview}. Pure; None if unusable."""
    if not isinstance(loc, dict):
        return None
    path = _location_raw_path(loc)
    rng = loc.get("range") if isinstance(loc.get("range"), dict) else {}
    start = rng.get("start") if isinstance(rng.get("start"), dict) else {}
    line, column, position_reason = _coerce_position(
        start.get("line", 0),
        start.get("character", 0),
    )
    if not path:
        return None
    if position_reason or line is None or column is None:
        return None
    rel, content, path_reason = _trusted_project_text(
        root,
        str(path),
        allow_absolute=True,
    )
    if path_reason or rel is None or content is None:
        return None
    if _validate_position_in_text(content, line, column):
        return None
    lines = content.splitlines()
    preview = lines[line].strip()[:200] if line < len(lines) else ""
    return {"path": rel, "line": line, "column": column, "preview": preview}


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
    normalized_path, path_reason = _request_path_shape(file_path)
    normalized_line, normalized_column, position_reason = _coerce_position(line, column)
    if path_reason or position_reason:
        return {
            "ok": False,
            "reason": path_reason or position_reason,
            "references": [],
        }
    avail = lsp_available(root)
    if not avail["ok"]:
        return {
            "ok": False,
            "reason": avail["reason"],
            "references": [],
        }
    trusted_path, content, source_state, trust_reason = _trusted_project_source(
        root,
        normalized_path or "",
        allow_absolute=False,
    )
    if (
        trust_reason
        or trusted_path is None
        or content is None
        or source_state is None
    ):
        return {"ok": False, "reason": trust_reason, "references": []}
    if (
        normalized_line is None
        or normalized_column is None
        or (position_reason := _validate_position_in_text(content, normalized_line, normalized_column))
    ):
        return {"ok": False, "reason": position_reason or "invalid_position", "references": []}

    cache_key = (
        _normalise_root(root).as_posix(),
        trusted_path,
        normalized_line,
        normalized_column,
        _source_signature(source_state),
    )
    cached = _cache_get(cache_key, root=root)
    if cached is not None:
        return cached

    raw = _lsp_call(
        root,
        trusted_path,
        normalized_line,
        normalized_column,
        kind="references",
    )
    if raw is None:
        return {"ok": False, "reason": "lsp_query_failed", "references": []}
    refs: list[dict[str, Any]] = []
    for loc in raw:
        mapped = _map_location(loc, root)
        if mapped is None:
            continue
        refs.append(mapped)
        if len(refs) >= LSP_RESULT_MAX:
            break
    result: dict[str, Any] = {"ok": True, "references": refs}
    dependencies: dict[str, _SourceSignature] = {}
    for ref in refs:
        rel_path = ref.get("path")
        if not isinstance(rel_path, str) or rel_path in dependencies:
            continue
        try:
            dependencies[rel_path] = _source_signature(
                _trusted_source_stat(root, rel_path)
            )
        except OSError:
            continue
    _cache_put(cache_key, result, dependencies=dependencies)
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
    normalized_path, path_reason = _request_path_shape(file_path)
    normalized_line, normalized_column, position_reason = _coerce_position(line, column)
    if path_reason or position_reason:
        return {
            "ok": False,
            "reason": path_reason or position_reason,
            "definition": None,
        }
    avail = lsp_available(root)
    if not avail["ok"]:
        return {
            "ok": False,
            "reason": avail["reason"],
            "definition": None,
        }
    trusted_path, content, trust_reason = _trusted_project_text(
        root,
        normalized_path or "",
        allow_absolute=False,
    )
    if trust_reason or trusted_path is None or content is None:
        return {"ok": False, "reason": trust_reason, "definition": None}
    if (
        normalized_line is None
        or normalized_column is None
        or (position_reason := _validate_position_in_text(content, normalized_line, normalized_column))
    ):
        return {"ok": False, "reason": position_reason or "invalid_position", "definition": None}
    raw = _lsp_call(
        root,
        trusted_path,
        normalized_line,
        normalized_column,
        kind="definition",
    )
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
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "reason": "empty_query", "symbols": []}
    if "\x00" in query:
        return {"ok": False, "reason": "invalid_query_control_character", "symbols": []}
    if len(query) > LSP_QUERY_MAX_CHARS:
        return {"ok": False, "reason": "query_too_long", "symbols": []}
    try:
        cap = int(limit)
    except (TypeError, ValueError, OverflowError):
        cap = 20
    cap = max(0, min(LSP_RESULT_MAX, cap))

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
