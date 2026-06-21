"""Tests for cAST-style AST-aware chunking (Python pilot, opt-in)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.ast_chunker import chunk_python, maybe_ast_chunks  # noqa: E402


# ---------------------------------------------------------------------------
# chunk_python — core behavior
# ---------------------------------------------------------------------------


def _multi_function_source() -> str:
    return (
        "import os\n"
        "\n"
        "\n"
        "def alpha(x):\n"
        "    # alpha does a thing\n"
        "    total = 0\n"
        "    for i in range(x):\n"
        "        total += i * 2\n"
        "        total -= 1\n"
        "    return total\n"
        "\n"
        "\n"
        "def beta(y):\n"
        "    # beta does another thing\n"
        "    acc = []\n"
        "    for j in range(y):\n"
        "        acc.append(j)\n"
        "    return acc\n"
        "\n"
        "\n"
        "class Gamma:\n"
        "    def method_one(self):\n"
        "        return 1\n"
        "\n"
        "    def method_two(self):\n"
        "        return 2\n"
    )


def test_multi_function_yields_multiple_coherent_chunks() -> None:
    source = _multi_function_source()
    # Small max_chars to force per-function chunking instead of one big merge.
    chunks = chunk_python(source, max_chars=120, min_chars=20)
    assert len(chunks) >= 2
    lines = source.split("\n")
    for ch in chunks:
        assert set(ch) == {"text", "start_line", "end_line"}
        assert 1 <= ch["start_line"] <= ch["end_line"] <= len(lines)
        # text must equal the exact source slice (coherent, no corruption).
        expected = "\n".join(lines[ch["start_line"] - 1 : ch["end_line"]])
        assert ch["text"] == expected


def test_chunks_within_size_bounds_when_splittable() -> None:
    source = _multi_function_source()
    max_chars = 120
    chunks = chunk_python(source, max_chars=max_chars, min_chars=20)
    # Each individual function here is small; with a tight budget no chunk
    # should be a giant blob far over the budget (allow the single-node floor:
    # an indivisible node may exceed, but our functions are all small).
    for ch in chunks:
        assert len(ch["text"]) <= max_chars + 80  # small slack for headers


def test_tiny_siblings_are_merged() -> None:
    # Several tiny top-level statements should merge into far fewer chunks
    # rather than one chunk per line.
    source = "\n".join(f"x{i} = {i}" for i in range(20)) + "\n"
    chunks = chunk_python(source, max_chars=1500, min_chars=200)
    assert len(chunks) == 1  # all tiny siblings merge under the budget
    assert chunks[0]["start_line"] == 1
    assert chunks[0]["end_line"] == 20


def test_oversized_function_is_split() -> None:
    # A class containing several methods, each large enough that the whole
    # class exceeds max_chars, should be split into multiple chunks.
    method_body = "\n".join(f"        v{i} = {i} * {i} + {i}" for i in range(10))
    methods = []
    for name in ("a", "b", "c", "d"):
        methods.append(f"    def {name}(self):\n{method_body}\n        return v0\n")
    source = "class Big:\n" + "\n".join(methods) + "\n"
    whole = len(source)
    max_chars = whole // 3
    chunks = chunk_python(source, max_chars=max_chars, min_chars=20)
    assert len(chunks) >= 2
    # Splitting an oversized container must reduce per-chunk size below whole.
    assert all(len(ch["text"]) < whole for ch in chunks)


def test_syntax_error_returns_empty() -> None:
    bad = "def broken(:\n    return 1\n"
    assert chunk_python(bad) == []


def test_empty_source_returns_empty() -> None:
    assert chunk_python("") == []
    assert chunk_python("   \n  \n") == []


def test_deterministic() -> None:
    source = _multi_function_source()
    first = chunk_python(source, max_chars=120, min_chars=20)
    second = chunk_python(source, max_chars=120, min_chars=20)
    assert first == second


def test_chunks_cover_in_source_order() -> None:
    source = _multi_function_source()
    chunks = chunk_python(source, max_chars=120, min_chars=20)
    starts = [c["start_line"] for c in chunks]
    assert starts == sorted(starts)


# ---------------------------------------------------------------------------
# maybe_ast_chunks — opt-in env gate (default OFF)
# ---------------------------------------------------------------------------


def test_maybe_ast_chunks_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AI_AST_CHUNK", raising=False)
    assert maybe_ast_chunks("x.py", _multi_function_source()) is None


def test_maybe_ast_chunks_off_when_falsy(monkeypatch) -> None:
    for val in ("0", "off", "false", "no", ""):
        monkeypatch.setenv("AI_AST_CHUNK", val)
        assert maybe_ast_chunks("x.py", _multi_function_source()) is None


def test_maybe_ast_chunks_on_for_python(monkeypatch) -> None:
    monkeypatch.setenv("AI_AST_CHUNK", "1")
    out = maybe_ast_chunks("x.py", _multi_function_source())
    assert out is not None
    assert isinstance(out, list)
    assert len(out) >= 1
    assert set(out[0]) == {"text", "start_line", "end_line"}


def test_maybe_ast_chunks_none_for_non_python(monkeypatch) -> None:
    # Even enabled, non-.py files defer to the existing chunker (return None).
    monkeypatch.setenv("AI_AST_CHUNK", "1")
    assert maybe_ast_chunks("x.ts", "const a = 1;") is None
    assert maybe_ast_chunks("x.go", "package main") is None


def test_maybe_ast_chunks_enabled_syntax_error_returns_empty(monkeypatch) -> None:
    # Enabled + .py but unparseable: returns [] (caller then falls back).
    monkeypatch.setenv("AI_AST_CHUNK", "1")
    assert maybe_ast_chunks("x.py", "def broken(:\n    pass\n") == []
