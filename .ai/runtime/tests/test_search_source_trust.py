from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from ai_core import astgrep_integration
from ai_core import search as search_mod


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ai").mkdir()
    (repo / ".ai" / "config.yaml").write_text("project_name: trust\n", encoding="utf-8")
    return repo


def _write(repo: Path, rel: str, content: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_full_rebuild_rejects_external_hardlink_source(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    external = tmp_path / "external.py"
    external.write_text("ExternalHardlinkNeedle = True\n", encoding="utf-8")
    linked = repo / "src" / "linked.py"
    linked.parent.mkdir(parents=True)
    os.link(external, linked)

    rebuilt = search_mod.rebuild(repo)
    result = search_mod.query(repo, "ExternalHardlinkNeedle")

    assert rebuilt["ok"] is True
    assert result["results"] == []
    assert external.read_text(encoding="utf-8") == "ExternalHardlinkNeedle = True\n"
    assert external.stat().st_nlink == 2


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_targeted_rebuild_rejects_external_parent_symlink(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "safe.py", "SafeNeedle = True\n")
    search_mod.rebuild(repo)
    external = tmp_path / "external-src"
    external.mkdir()
    (external / "outside.py").write_text("ExternalParentNeedle = True\n", encoding="utf-8")
    (repo / "src").symlink_to(external, target_is_directory=True)

    rebuilt = search_mod.rebuild(
        repo,
        incremental=True,
        paths={"src/outside.py"},
    )
    result = search_mod.query(repo, "ExternalParentNeedle")

    assert rebuilt["ok"] is True
    assert rebuilt["targeted"] is True
    assert result["results"] == []
    assert (external / "outside.py").read_text(encoding="utf-8") == (
        "ExternalParentNeedle = True\n"
    )


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_live_snippet_and_fallback_reject_replaced_hardlink(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "TrustedIndexedNeedle = True\n")
    search_mod.rebuild(repo)
    source.unlink()
    external = tmp_path / "external-replacement.py"
    external.write_text("ExternalReplacementNeedle = True\n", encoding="utf-8")
    os.link(external, source)

    indexed = search_mod.query(repo, "TrustedIndexedNeedle")
    external_result = search_mod.query(repo, "ExternalReplacementNeedle")

    assert indexed["results"] == []
    assert external_result["results"] == []


def test_full_rebuild_uses_descriptor_state_without_restat_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "DescriptorStateNeedle = True\n")
    original_read = search_mod._read_indexable_text
    original_stat = Path.stat
    source_read = {"done": False}

    def tracked_read(root: Path, path: Path):
        loaded = original_read(root, path)
        if path == source and loaded is not None:
            source_read["done"] = True
        return loaded

    def reject_restat(path: Path, *args, **kwargs):
        if path == source and source_read["done"]:
            raise AssertionError("source path must not be re-statted after trusted descriptor read")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(search_mod, "_read_indexable_text", tracked_read)
    monkeypatch.setattr(Path, "stat", reject_restat)
    monkeypatch.setenv("AI_SEARCH_CODEGRAPH", "0")

    rebuilt = search_mod.rebuild(repo)

    assert rebuilt["ok"] is True
    with search_mod.connect(repo) as conn:
        row = conn.execute(
            "select size, mtime_ns, ctime_ns from file_state where path = ?",
            ("src/main.py",),
        ).fetchone()
    assert row is not None
    assert row[0] == len("DescriptorStateNeedle = True\n".encode("utf-8"))


def test_multilang_codegraph_parses_private_trusted_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/app.js", "function safeValue() { return 1; }\n")
    received_paths: list[Path] = []

    def extract_symbols(path: str):
        candidate = Path(path)
        received_paths.append(candidate)
        assert candidate != source
        assert candidate.name == "source.js"
        assert candidate.read_text(encoding="utf-8") == "function safeValue() { return 1; }\n"
        if os.name != "nt":
            assert stat.S_IMODE(candidate.parent.stat().st_mode) & 0o077 == 0
        return [
            {
                "qualname": "safeValue",
                "kind": "function",
                "lineno": 1,
                "end_lineno": 1,
            }
        ]

    def extract_calls(path: str):
        candidate = Path(path)
        received_paths.append(candidate)
        assert candidate != source
        assert candidate.read_text(encoding="utf-8") == "function safeValue() { return 1; }\n"
        return []

    monkeypatch.setenv("AI_SEARCH_CODEGRAPH", "1")
    monkeypatch.setattr(astgrep_integration, "extract_symbols_js", extract_symbols)
    monkeypatch.setattr(astgrep_integration, "extract_calls_js", extract_calls)

    rebuilt = search_mod.rebuild(repo)

    assert rebuilt["ok"] is True
    assert len(received_paths) == 3
    assert all(not path.exists() for path in received_paths)
    with search_mod.connect(repo) as conn:
        symbols = conn.execute(
            "select path, qualname, kind, lang from code_symbols where path = ?",
            ("src/app.js",),
        ).fetchall()
    assert [tuple(row) for row in symbols] == [
        ("src/app.js", "safeValue", "function", "javascript")
    ]


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_indexable_policy_rejects_parent_symlink_before_content_read(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    outside = external / "outside.py"
    outside.write_text("OutsideNeedle = True\n", encoding="utf-8")
    (repo / "src").symlink_to(external, target_is_directory=True)

    assert search_mod._indexable_text_stat(repo, repo / "src" / "outside.py") is None
    assert search_mod._read_indexable_text(repo, repo / "src" / "outside.py") is None
