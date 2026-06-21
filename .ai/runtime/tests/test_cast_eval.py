"""Tests for the cAST self-validation eval + ratchet (cast_eval) and the
search.py integration that honors a passing verdict."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import cast_eval  # noqa: E402
from ai_core import search as search_mod  # noqa: E402
from ai_core.search import connect, rebuild  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("project_name: t\n", encoding="utf-8")
    return repo


def _write(repo: Path, rel: str, content: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _seed_documented_py(repo: Path) -> None:
    """Seed several Python files where each symbol has a distinctive docstring
    whose first line works as a search query for that symbol's chunk."""
    _write(
        repo,
        "src/alpha.py",
        '''"""module alpha"""


def harvest_widgets(n):
    """harvest widgets from the orchard quickly"""
    total = 0
    for i in range(n):
        total += i
    return total


def polish_gizmos(items):
    """polish gizmos until shiny"""
    return [str(x) for x in items]
''',
    )
    _write(
        repo,
        "src/beta.py",
        '''"""module beta"""


class Refinery:
    """refinery transforms ore into ingots"""

    def smelt_ore(self, ore):
        """smelt ore into molten metal"""
        return ore * 2

    def cast_ingot(self, metal):
        """cast ingot from molten metal"""
        return metal + 1
''',
    )


# ---------------------------------------------------------------------------
# build_query_set
# ---------------------------------------------------------------------------


def test_build_query_set_from_docstrings(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_documented_py(repo)
    qs = cast_eval.build_query_set(repo)
    queries = {q["query"] for q in qs}
    assert "harvest widgets from the orchard quickly" in queries
    assert "smelt ore into molten metal" in queries
    # Each entry carries a path + line span used as the relevance target.
    for q in qs:
        assert q["path"].endswith(".py")
        assert 1 <= q["start_line"] <= q["end_line"]


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def test_evaluate_returns_dict_and_writes_verdict(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_documented_py(repo)
    out = cast_eval.evaluate(repo, k=5, margin=0.02, min_queries=1)
    assert out["ok"] is True
    assert "recall_default" in out and "recall_cast" in out and "n" in out
    assert isinstance(out["recall_default"], float)
    assert isinstance(out["recall_cast"], float)
    assert out["n"] >= 1
    assert out["k"] == 5
    # Verdict file persisted at the documented location.
    vp = cast_eval.verdict_path(repo)
    assert vp.is_file()
    data = json.loads(vp.read_text(encoding="utf-8"))
    assert data["n"] == out["n"]
    assert data["recall_default"] == out["recall_default"]
    assert data["enabled"] == out["enabled"]


def test_evaluate_min_queries_gate_blocks_enable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_documented_py(repo)
    # Demand far more queries than the tiny repo has → never enabled.
    out = cast_eval.evaluate(repo, k=5, margin=0.02, min_queries=10_000)
    assert out["ok"] is True
    assert out["enabled"] is False


def test_evaluate_fail_soft_on_bad_root(tmp_path: Path) -> None:
    # A path with no .ai/config.yaml: build_query_set + indexing yield nothing,
    # but evaluate must never raise.
    missing = tmp_path / "nope"
    out = cast_eval.evaluate(missing)
    assert out["ok"] in (True, False)
    assert out["enabled"] is False


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def test_verdict_false_when_absent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert cast_eval.verdict(repo) is False


def test_verdict_true_when_enabled_written(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    vp = cast_eval.verdict_path(repo)
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert cast_eval.verdict(repo) is True


def test_verdict_false_when_disabled_written(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    vp = cast_eval.verdict_path(repo)
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert cast_eval.verdict(repo) is False


def test_verdict_fail_soft_on_corrupt_json(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    vp = cast_eval.verdict_path(repo)
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text("{not json", encoding="utf-8")
    assert cast_eval.verdict(repo) is False


# ---------------------------------------------------------------------------
# metric / hit logic (unit, no index build)
# ---------------------------------------------------------------------------


def test_is_hit_file_level_and_overlap() -> None:
    target = {"path": "src/a.py", "start_line": 10, "end_line": 20}
    # File-level result for the same file contains the symbol.
    assert cast_eval._is_hit([{"path": "src/a.py", "start_line": 1, "end_line": 99}], target)
    # Function chunk overlapping the span hits.
    assert cast_eval._is_hit(
        [{"path": "src/a.py:foo", "start_line": 15, "end_line": 25}], target
    )
    # Non-overlapping function chunk in same file misses.
    assert not cast_eval._is_hit(
        [{"path": "src/a.py:bar", "start_line": 30, "end_line": 40}], target
    )
    # Different file misses.
    assert not cast_eval._is_hit(
        [{"path": "src/b.py", "start_line": 1, "end_line": 99}], target
    )


def test_recall_at_k_with_inmemory_index(tmp_path: Path) -> None:
    db = tmp_path / "idx.sqlite"
    files = [(
        "src/a.py",
        'def find_treasure():\n    """locate the buried treasure"""\n    return 42\n',
    )]
    cast_eval._build_eval_index(db, files, use_cast=False)
    queries = [{
        "query": "locate the buried treasure",
        "path": "src/a.py",
        "start_line": 1,
        "end_line": 3,
    }]
    r = cast_eval._recall_at_k(db, queries, k=5)
    assert r == 1.0
    # A query that matches nothing yields recall 0.
    miss = [{"query": "zzqxnotalken", "path": "src/a.py", "start_line": 1, "end_line": 3}]
    assert cast_eval._recall_at_k(db, miss, k=5) == 0.0


# ---------------------------------------------------------------------------
# search.py integration: verdict auto-enables cAST without env
# ---------------------------------------------------------------------------


def _has_cast_chunks(repo: Path) -> bool:
    with connect(repo) as conn:
        rows = conn.execute(
            "select qualname from chunk_meta where qualname like 'cast:%'"
        ).fetchall()
    return len(rows) > 0


def _big_python_source() -> str:
    # Large enough that cAST emits its synthetic cast:<start>-<end> chunks.
    body = "\n".join(f"    v{i} = {i} * {i} + {i}" for i in range(40))
    funcs = []
    for name in ("aaa", "bbb", "ccc", "ddd"):
        funcs.append(f"def {name}(x):\n    '''{name} doc'''\n{body}\n    return v0\n")
    return "\n\n".join(funcs) + "\n"


def test_search_uses_cast_when_verdict_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_AST_CHUNK", raising=False)
    repo = _make_repo(tmp_path)
    _write(repo, "src/big.py", _big_python_source())
    # No env flag, but verdict says enabled → cAST chunks must appear.
    monkeypatch.setattr("ai_core.cast_eval.verdict", lambda root: True)
    rebuild(repo)
    assert _has_cast_chunks(repo) is True


def test_search_default_unchanged_when_no_verdict_and_env_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_AST_CHUNK", raising=False)
    repo = _make_repo(tmp_path)
    _write(repo, "src/big.py", _big_python_source())
    # No env flag and verdict disabled → default chunker only (no cast: chunks).
    monkeypatch.setattr("ai_core.cast_eval.verdict", lambda root: False)
    rebuild(repo)
    assert _has_cast_chunks(repo) is False
    # Sanity: function-level chunks still produced by the default chunker.
    with connect(repo) as conn:
        names = [r[0] for r in conn.execute(
            "select qualname from chunk_meta where qualname is not null"
        ).fetchall()]
    assert any(n in ("aaa", "bbb", "ccc", "ddd") for n in names)


def test_search_env_flag_still_enables_cast(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AST_CHUNK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "src/big.py", _big_python_source())
    # Even with verdict False, the env flag enables cAST (backward compatible).
    monkeypatch.setattr("ai_core.cast_eval.verdict", lambda root: False)
    rebuild(repo)
    assert _has_cast_chunks(repo) is True
