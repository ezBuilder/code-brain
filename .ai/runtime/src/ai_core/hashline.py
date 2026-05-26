from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


HASH_LEN = 12
MAX_READ_BYTES = 512 * 1024
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


def _resolve_under_root(root: Path, target: str) -> Path:
    raw = Path(target).expanduser()
    path = raw if raw.is_absolute() else root / raw
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("path must stay under repo root") from exc
    if _is_denied_path(resolved):
        raise PermissionError("refusing to hashline-read credential-like path")
    return resolved


def read_hashline(
    root: Path,
    target: str,
    *,
    start: int | None = None,
    end: int | None = None,
    max_bytes: int = MAX_READ_BYTES,
) -> dict[str, Any]:
    path = _resolve_under_root(root, target)
    if not path.is_file():
        raise FileNotFoundError(target)
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file too large for hashline read: {size} bytes > {max_bytes}")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    first = max(1, start or 1)
    last = min(len(lines), end or len(lines))
    if first > last and lines:
        selected: list[tuple[int, str]] = []
    else:
        selected = [(idx, lines[idx - 1]) for idx in range(first, last + 1)]
    rendered = "\n".join(format_anchor(idx, line) for idx, line in selected)
    return {
        "ok": True,
        "path": path.relative_to(root.resolve()).as_posix(),
        "start": first,
        "end": last,
        "line_count": len(selected),
        "hash_format": "line+sha12|content",
        "content": rendered,
    }


def verify_anchors(root: Path, target: str, anchors: list[dict[str, Any]]) -> dict[str, Any]:
    path = _resolve_under_root(root, target)
    if not path.is_file():
        raise FileNotFoundError(target)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    results: list[dict[str, Any]] = []
    ok = True
    for anchor in anchors:
        line = int(anchor.get("line") or 0)
        expected = str(anchor.get("hash") or "")
        if line <= 0 or line > len(lines):
            ok = False
            results.append({"line": line, "ok": False, "reason": "line_out_of_range"})
            continue
        actual = line_hash(line, lines[line - 1])
        match = actual == expected
        ok = ok and match
        item = {"line": line, "ok": match, "expected": expected, "actual": actual}
        if "content" in anchor:
            item["content_matches"] = str(anchor.get("content")) == lines[line - 1]
        results.append(item)
    return {
        "ok": ok,
        "path": path.relative_to(root.resolve()).as_posix(),
        "checked": len(results),
        "results": results,
    }
