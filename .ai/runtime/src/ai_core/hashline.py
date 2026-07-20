from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .private_write import read_root_confined_bytes


HASH_LEN = 12
MAX_READ_BYTES = 512 * 1024
MAX_PATH_CHARS = 4096
MAX_RANGE_LINES = 10_000
MAX_ANCHORS = 1_000
MAX_ANCHOR_CONTENT_CHARS = 4096
MAX_LINE_NUMBER = 10_000_000
_HASH_RE = re.compile(r"^[0-9a-f]{12}$")
DENIED_NAMES = {
    ".env",
    "auth.json",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def line_hash(line_number: int, content: str) -> str:
    payload = f"{line_number}\0{content}".encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(payload).hexdigest()[:HASH_LEN]


def format_anchor(line_number: int, content: str) -> str:
    return f"{line_number}+{line_hash(line_number, content)}|{content}"


def _is_denied_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    if parts & DENIED_NAMES:
        return True
    name = path.name.lower()
    return name.endswith(DENIED_SUFFIXES)


def _resolve_under_root(root: Path, target: str) -> tuple[Path, Path]:
    raw_text = str(target or "").strip()
    if not raw_text or "\x00" in raw_text or len(raw_text) > MAX_PATH_CHARS:
        raise ValueError("invalid repo-relative path")
    root_absolute = Path(os.path.abspath(root))
    raw = Path(raw_text).expanduser()
    path = raw if raw.is_absolute() else root_absolute / raw
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ValueError("path must stay under repo root") from exc
    if not relative.parts:
        raise ValueError("path must identify a file under repo root")
    if _is_denied_path(relative):
        raise PermissionError("refusing to hashline-read credential-like path")
    return absolute, relative


def _coerce_bound(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("invalid line bound")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid line bound") from exc
    return max(minimum, min(maximum, parsed))


def _trusted_file_text(root: Path, target: str, *, max_bytes: int) -> tuple[str, Path]:
    path, relative = _resolve_under_root(root, target)
    byte_cap = _coerce_bound(
        max_bytes,
        default=MAX_READ_BYTES,
        minimum=1,
        maximum=MAX_READ_BYTES,
    )
    data, _state = read_root_confined_bytes(
        path,
        root=Path(os.path.abspath(root)),
        max_bytes=byte_cap,
        require_private=False,
        require_owner=True,
        reject_group_other_writable=True,
    )
    return data.decode("utf-8", errors="replace"), relative


def read_hashline(
    root: Path,
    target: str,
    *,
    start: int | None = None,
    end: int | None = None,
    max_bytes: int = MAX_READ_BYTES,
) -> dict[str, Any]:
    text, relative = _trusted_file_text(root, target, max_bytes=max_bytes)
    lines = text.splitlines()
    first = _coerce_bound(start, default=1, minimum=1, maximum=MAX_LINE_NUMBER)
    requested_end = _coerce_bound(
        end,
        default=min(len(lines), first + MAX_RANGE_LINES - 1),
        minimum=1,
        maximum=MAX_LINE_NUMBER,
    )
    last = min(len(lines), requested_end, first + MAX_RANGE_LINES - 1)
    if first > last and lines:
        selected: list[tuple[int, str]] = []
    else:
        selected = [(idx, lines[idx - 1]) for idx in range(first, last + 1)]
    rendered = "\n".join(format_anchor(idx, line) for idx, line in selected)
    return {
        "ok": True,
        "path": relative.as_posix(),
        "start": first,
        "end": last,
        "line_count": len(selected),
        "truncated": bool(lines) and (
            requested_end > last or (end is None and last < len(lines))
        ),
        "hash_format": "line+sha12|content",
        "content": rendered,
    }


def verify_anchors(root: Path, target: str, anchors: list[dict[str, Any]]) -> dict[str, Any]:
    text, relative = _trusted_file_text(root, target, max_bytes=MAX_READ_BYTES)
    lines = text.splitlines()
    if not isinstance(anchors, list) or len(anchors) > MAX_ANCHORS:
        raise ValueError("invalid anchor list")
    results: list[dict[str, Any]] = []
    ok = True
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError("invalid anchor")
        line = _coerce_bound(
            anchor.get("line"),
            default=0,
            minimum=0,
            maximum=MAX_LINE_NUMBER,
        )
        expected = str(anchor.get("hash") or "").strip().lower()
        if not _HASH_RE.fullmatch(expected):
            raise ValueError("invalid anchor hash")
        if line <= 0 or line > len(lines):
            ok = False
            results.append({"line": line, "ok": False, "reason": "line_out_of_range"})
            continue
        actual = line_hash(line, lines[line - 1])
        match = actual == expected
        ok = ok and match
        item = {"line": line, "ok": match, "expected": expected, "actual": actual}
        if "content" in anchor:
            supplied_content = str(anchor.get("content") or "")
            if len(supplied_content) > MAX_ANCHOR_CONTENT_CHARS:
                raise ValueError("anchor content too long")
            item["content_matches"] = supplied_content == lines[line - 1]
        results.append(item)
    return {
        "ok": ok,
        "path": relative.as_posix(),
        "checked": len(results),
        "results": results,
    }
