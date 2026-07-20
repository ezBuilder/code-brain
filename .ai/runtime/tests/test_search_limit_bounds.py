from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import mcp_server
from ai_core import search as search_mod


def _make_repo(tmp_path: Path, *, files: int = 8) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ai").mkdir()
    (root / ".ai" / "config.yaml").write_text(
        "project_name: search-limit\n",
        encoding="utf-8",
    )
    source_dir = root / "src"
    source_dir.mkdir()
    for index in range(files):
        (source_dir / f"file-{index:03d}.py").write_text(
            f"BoundedSearchNeedle = {index}\n",
            encoding="utf-8",
        )
    return root


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-100, 1),
        (0, 1),
        (1, 1),
        (5, 5),
        (100, 100),
        (101, 100),
        (10**12, 100),
        ("7", 7),
        ("invalid", 5),
        (None, 5),
    ],
)
def test_normalize_result_limit_is_bounded(value, expected: int) -> None:
    assert search_mod.normalize_result_limit(value) == expected


def test_rg_limit_preserves_explicit_nonpositive_no_spawn() -> None:
    assert search_mod.normalize_result_limit(0, default=10, allow_zero=True) == 0
    assert search_mod.normalize_result_limit(-9, default=10, allow_zero=True) == 0
    assert search_mod.normalize_result_limit(10**9, default=10, allow_zero=True) == 100


def test_negative_query_limit_cannot_become_unlimited_sqlite_limit(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    search_mod.rebuild(root)

    payload = search_mod.query(root, "BoundedSearchNeedle", limit=-1)

    assert payload["ok"] is True
    assert len(payload["results"]) == 1


def test_oversized_query_and_context_limits_are_capped(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, files=105)
    search_mod.rebuild(root)

    payload = search_mod.query(root, "BoundedSearchNeedle", limit=10**9)
    packed = search_mod.context_pack(
        root,
        "BoundedSearchNeedle",
        limit=10**9,
        mode="high_fidelity",
    )

    assert len(payload["results"]) == search_mod.SEARCH_RESULT_MAX
    assert len(packed["results"]) <= search_mod.SEARCH_RESULT_MAX
    assert packed["context_budget"]["requested_limit"] == search_mod.SEARCH_RESULT_MAX


def test_dense_candidate_limit_has_fixed_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    assert search_mod.SEARCH_DENSE_CANDIDATE_MAX == search_mod.SEARCH_RESULT_MAX * 8
    assert min(
        search_mod.SEARCH_DENSE_CANDIDATE_MAX,
        max(search_mod.normalize_result_limit(10**9) * 8, 40),
    ) == search_mod.SEARCH_DENSE_CANDIDATE_MAX


def test_mcp_search_limit_schemas_publish_runtime_bounds() -> None:
    by_name = {tool["name"]: tool for tool in mcp_server.TOOLS}
    for name in ("memory_query", "code_query", "context_pack"):
        limit = by_name[name]["inputSchema"]["properties"]["limit"]
        assert limit["minimum"] == 1
        assert limit["maximum"] == search_mod.SEARCH_RESULT_MAX
        assert limit["default"] == search_mod.SEARCH_RESULT_DEFAULT
