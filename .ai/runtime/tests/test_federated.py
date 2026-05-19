from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import federated as federated_mod  # noqa: E402
from ai_core.federated import (  # noqa: E402
    _federated_cache_path,
    cross_project_summary,
    discover_installations,
    gather_cross_project_signals,
)


def _make_proj(home: Path, name: str, decisions_tags=None, todos=None) -> Path:
    proj = home / "workspace" / name
    (proj / ".ai" / "generated").mkdir(parents=True)
    (proj / ".ai" / "generated" / "install-manifest.json").write_text("{}", encoding="utf-8")
    (proj / ".ai" / "memory").mkdir(parents=True)
    if decisions_tags:
        path = proj / ".ai" / "memory" / "decisions.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for tag in decisions_tags:
                f.write(json.dumps({"id": "d", "decision": "x", "tags": [tag]}, ensure_ascii=False) + "\n")
    if todos:
        path = proj / ".ai" / "memory" / "todos.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for title in todos:
                f.write(json.dumps({"id": "t", "title": title, "status": "open"}, ensure_ascii=False) + "\n")
    return proj


def test_discover_installations(tmp_path: Path):
    _make_proj(tmp_path, "alpha")
    _make_proj(tmp_path, "beta")
    found = discover_installations(home=tmp_path)
    names = sorted(p.name for p in found)
    assert names == ["alpha", "beta"]


def test_cross_project_summary_aggregates(tmp_path: Path):
    self_proj = _make_proj(tmp_path, "self_proj", decisions_tags=["release", "release"])
    _make_proj(tmp_path, "other1", decisions_tags=["release", "release", "auth"])
    _make_proj(tmp_path, "other2", decisions_tags=["release", "perf"], todos=["fix bug", "fix typo"])
    out = cross_project_summary(self_proj, home=tmp_path)
    assert out["scanned_projects"] == 2
    tags = {x["tag"] for x in out["common_tags"]}
    assert "release" in tags  # appears in both other projects


def test_cross_project_no_others(tmp_path: Path):
    self_proj = _make_proj(tmp_path, "only")
    out = cross_project_summary(self_proj, home=tmp_path)
    assert out["scanned_projects"] == 0
    assert out.get("note") == "no_other_installs"


def test_no_raw_text_leak(tmp_path: Path):
    self_proj = _make_proj(tmp_path, "self2")
    _make_proj(
        tmp_path, "other_secret",
        decisions_tags=["secret"],
        todos=["delete /Users/foo/secret-file"],
    )
    out = gather_cross_project_signals(self_proj, home=tmp_path)
    flat = json.dumps(out, ensure_ascii=False)
    # raw text from other project's todo should never appear in the federated payload
    assert "/Users/foo" not in flat
    assert "secret-file" not in flat


def test_federated_cache_hit_returns_cached_value(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AI_FEDERATED_CACHE", raising=False)
    self_proj = _make_proj(tmp_path, "self_cache", decisions_tags=["release"])
    _make_proj(tmp_path, "other_a", decisions_tags=["release", "release"])
    _make_proj(tmp_path, "other_b", decisions_tags=["release"])

    # First call populates the cache.
    first = cross_project_summary(self_proj, home=tmp_path)
    cache_path = _federated_cache_path(self_proj)
    assert cache_path.exists()

    # Force any subsequent compute to fail — cache must still be served.
    def _boom(*a, **kw):
        raise AssertionError("compute should not be called when cache is fresh")

    monkeypatch.setattr(federated_mod, "_compute_cross_project_summary", _boom)
    second = cross_project_summary(self_proj, home=tmp_path)
    assert second == first


def test_federated_cache_disabled_by_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_FEDERATED_CACHE", "0")
    self_proj = _make_proj(tmp_path, "self_off", decisions_tags=["release"])
    _make_proj(tmp_path, "other_x", decisions_tags=["release", "release"])

    counter = {"n": 0}
    real_compute = federated_mod._compute_cross_project_summary

    def _counting_compute(self_root, *, home=None):
        counter["n"] += 1
        return real_compute(self_root, home=home)

    monkeypatch.setattr(federated_mod, "_compute_cross_project_summary", _counting_compute)

    cross_project_summary(self_proj, home=tmp_path)
    cross_project_summary(self_proj, home=tmp_path)
    assert counter["n"] == 2
    # Cache file must not be written when env disables caching.
    assert not _federated_cache_path(self_proj).exists()
