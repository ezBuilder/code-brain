"""Schema v10: vendored Code Brain payload is excluded from consumer-repo indexes."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.search import (  # noqa: E402
    VENDORED_RUNTIME_PREFIXES,
    _skip_path_prefixes,
    query,
    rebuild,
)


def _make_repo(tmp_path: Path, *, opt_in: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ai").mkdir(parents=True)
    config = "project_name: t\n"
    if opt_in:
        config += "search:\n  index_vendored_runtime: true\n"
    (repo / ".ai" / "config.yaml").write_text(config, encoding="utf-8")
    return repo


def _write(repo: Path, rel: str, content: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_consumer_repo_skips_vendored_runtime(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, ".ai/runtime/src/ai_core/vendored.py", "def vendoredNeedleMarker():\n    pass\n")
    _write(repo, ".ai/evals/cases/sample.jsonl", '{"id":"vendoredNeedleMarker"}\n')
    _write(repo, "src/app.py", "def vendoredNeedleMarker():\n    return 'project code'\n")
    rebuild(repo)
    payload = query(repo, "vendoredNeedleMarker", limit=10)
    paths = [item["path"] for item in payload["results"]]
    assert any(path.startswith("src/app.py") for path in paths)
    assert not any(path.startswith(".ai/runtime/") for path in paths)
    assert not any(path.startswith(".ai/evals/") for path in paths)


def test_operational_tmp_is_never_indexed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, opt_in=True)
    _write(repo, ".ai/tmp/external-clone/source.py", "def tmpNeedleMarker():\n    pass\n")
    _write(repo, "src/app.py", "def tmpNeedleMarker():\n    return 'project code'\n")

    rebuild(repo)

    payload = query(repo, "tmpNeedleMarker", limit=10)
    paths = [item["path"] for item in payload["results"]]
    assert any(path.startswith("src/app.py") for path in paths)
    assert not any(path.startswith(".ai/tmp/") for path in paths)


def test_source_repo_opt_in_keeps_runtime_indexed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, opt_in=True)
    _write(repo, ".ai/runtime/src/ai_core/vendored.py", "def vendoredNeedleMarker():\n    pass\n")
    rebuild(repo)
    payload = query(repo, "vendoredNeedleMarker", limit=10)
    paths = [item["path"] for item in payload["results"]]
    assert any(path.startswith(".ai/runtime/") for path in paths)


def test_skip_prefixes_react_to_config_change(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert set(VENDORED_RUNTIME_PREFIXES) <= set(_skip_path_prefixes(repo))
    config = repo / ".ai" / "config.yaml"
    config.write_text(
        "project_name: t\nsearch:\n  index_vendored_runtime: true\n", encoding="utf-8"
    )
    assert not (set(VENDORED_RUNTIME_PREFIXES) & set(_skip_path_prefixes(repo)))
