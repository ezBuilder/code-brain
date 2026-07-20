from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .private_write import read_root_confined_text


CONFIG_MAX_BYTES = 256 * 1024
CONFIG_MAX_LINES = 5_000
CONFIG_MAX_DEPTH = 16
CONFIG_MAX_KEYS = 2_000
CONFIG_MAX_KEY_CHARS = 128
CONFIG_MAX_VALUE_CHARS = 4096
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


def _strip_comment(raw: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote is not None:
            escaped = True
            continue
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None:
            return raw[:index]
    if quote is not None:
        raise ValueError("unterminated quoted config value")
    return raw


def load_config(root: Path) -> dict[str, Any]:
    root = Path(root)
    path = root / ".ai" / "config.yaml"
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=CONFIG_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        raise ValueError(".ai/config.yaml is missing")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(".ai/config.yaml is unavailable or untrusted") from exc
    data: dict[str, Any] = {}
    # (mapping key indentation, mapping, expected child indentation)
    stack: list[list[Any]] = [[-1, data, 0]]
    key_count = 0
    raw_lines = text.splitlines()
    if len(raw_lines) > CONFIG_MAX_LINES:
        raise ValueError("config exceeds line limit")
    for line_number, raw in enumerate(raw_lines, start=1):
        if "\t" in raw:
            raise ValueError(f"invalid config indentation at line {line_number}")
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, sep, value = line.strip().partition(":")
        if not sep or not _KEY_RE.fullmatch(key) or len(key) > CONFIG_MAX_KEY_CHARS:
            raise ValueError(f"invalid config syntax at line {line_number}")
        while len(stack) > 1 and indent <= int(stack[-1][0]):
            stack.pop()
        parent_indent, parent, expected_indent = stack[-1]
        if indent <= int(parent_indent):
            raise ValueError(f"invalid config indentation at line {line_number}")
        if expected_indent is None:
            stack[-1][2] = indent
        elif indent != int(expected_indent):
            raise ValueError(f"inconsistent config indentation at line {line_number}")
        if key in parent:
            raise ValueError(f"duplicate config key at line {line_number}")
        key_count += 1
        if key_count > CONFIG_MAX_KEYS:
            raise ValueError("config exceeds key limit")
        if value.strip() == "":
            if len(stack) - 1 >= CONFIG_MAX_DEPTH:
                raise ValueError("config exceeds nesting depth")
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append([indent, child, None])
        else:
            scalar = value.strip()
            if len(scalar) > CONFIG_MAX_VALUE_CHARS or "\x00" in scalar:
                raise ValueError(f"invalid config value at line {line_number}")
            parent[key] = parse_scalar(scalar)
    return data


def parse_scalar(value: str) -> Any:
    if len(value) > CONFIG_MAX_VALUE_CHARS or "\x00" in value:
        raise ValueError("invalid config scalar")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered.isdigit():
        return int(lowered)
    if value[:1] in ("'", '"'):
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError("unterminated quoted config value")
        return value[1:-1]
    return value

