from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
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


# --- retired/expired sibling decisions must not vote across projects ----------
# federated.py mined decisions.jsonl with a raw tail read, bypassing
# memory.live_decision_records — the single source of truth every other decision reader
# already shares. A refuted/stale failure or a lapsed time-boxed decision in ANOTHER
# project therefore reached this project's injected context via cross_project_summary.

def _expired_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    path.chmod(0o600)  # readers reject group/other-writable state files regardless of umask


def _write_decisions(proj: Path, rows: list[dict]) -> None:
    _write_jsonl(proj / ".ai" / "memory" / "decisions.jsonl", rows)


def _mixed_decision_rows(live_count: int = 2) -> list[dict]:
    """Live rows plus one of each retirement mode the shared filter knows about."""
    rows: list[dict] = [
        {"id": f"live{i}", "decision": "keep me", "tags": ["liveinfra"]}
        for i in range(live_count)
    ]
    rows += [
        # a plain decision whose time box has lapsed
        {"id": "exp1", "decision": "time-boxed", "tags": ["expiredtag"],
         "expires_at": _expired_iso()},
        # a failure retired by a later same-id reappend (fold by id, last write wins)
        {"id": "f1", "kind": "failure", "status": "observed", "decision": "fp8 broke",
         "tags": ["refutedtag"]},
        {"id": "f1", "kind": "failure", "status": "refuted", "decision": "fp8 works now",
         "tags": ["refutedtag"]},
        # a failure marked stale in place
        {"id": "f2", "kind": "failure", "status": "stale", "decision": "old flake",
         "tags": ["staletag"]},
    ]
    return rows


_DEAD_TAGS = ("expiredtag", "refutedtag", "staletag")


def test_retired_and_expired_sibling_tags_absent_from_decision_tags(tmp_path: Path):
    """Criterion 1+2 at the exact surface federated.py publishes."""
    self_proj = _make_proj(tmp_path, "self_live")
    other = _make_proj(tmp_path, "other_live")
    _write_decisions(other, _mixed_decision_rows(live_count=2))

    tags = gather_cross_project_signals(self_proj, home=tmp_path)["decision_tags"]

    # the filter must not zero the feature out — live tags still counted, correctly
    assert tags["liveinfra"] == 2
    for dead in _DEAD_TAGS:
        assert dead not in tags, f"{dead} leaked from a retired/expired sibling decision"


def test_retired_and_expired_sibling_tags_absent_from_common_tags(tmp_path: Path, monkeypatch):
    """Same guarantee downstream, where hooks.py renders it into injected context."""
    monkeypatch.setenv("AI_FEDERATED_CACHE", "0")  # assert on a fresh compute, not a cache hit
    self_proj = _make_proj(tmp_path, "self_common")
    for name in ("other_c1", "other_c2"):
        _write_decisions(_make_proj(tmp_path, name), _mixed_decision_rows(live_count=1))

    out = cross_project_summary(self_proj, home=tmp_path)

    assert out["scanned_projects"] == 2
    common = {x["tag"]: x["projects"] for x in out["common_tags"]}
    assert common.get("liveinfra") == 2  # shared live tag survives with its count
    for dead in _DEAD_TAGS:
        assert dead not in common


def test_all_sibling_decisions_retired_yields_no_tags_but_keeps_other_mining(tmp_path: Path):
    """Criterion 5: the decision filter must not disturb the other miners in the same loop."""
    self_proj = _make_proj(tmp_path, "self_other_mining")
    other = _make_proj(tmp_path, "other_other_mining", todos=["fix flaky test"])
    _write_decisions(other, _mixed_decision_rows(live_count=0))
    _write_jsonl(other / ".ai" / "precall_rules" / "catalog.jsonl", [{"kind": "grep_guard"}])
    _write_jsonl(other / ".ai" / "skills" / "catalog.jsonl",
                 [{"slug": "cb-doctor", "status": "installed"},
                  {"slug": "cb-draft", "status": "proposed"}])

    sig = gather_cross_project_signals(self_proj, home=tmp_path)

    assert sig["decision_tags"] == {}  # every decision row was retired or expired
    assert sig["todo_bigrams"]["fix flaky"] == 1
    assert sig["precall_kinds"] == {"grep_guard": 1}
    assert sig["skills_slugs"] == {"cb-doctor": 1}  # non-installed slug still excluded


def test_malformed_sibling_decisions_file_does_not_raise(tmp_path: Path):
    """Criterion 4: non-dict/garbage rows stay fail-soft through the shared filter."""
    self_proj = _make_proj(tmp_path, "self_malformed")
    other = _make_proj(tmp_path, "other_malformed")
    path = other / ".ai" / "memory" / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not json at all\n"
        "[1, 2, 3]\n"
        '"a bare string"\n'
        "null\n"
        + json.dumps({"id": "ok1", "decision": "survivor", "tags": ["goodtag"]}) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    sig = gather_cross_project_signals(self_proj, home=tmp_path)

    assert sig["ok"] is True
    assert sig["decision_tags"].get("goodtag") == 1


def test_unreadable_sibling_project_is_skipped_not_fatal(tmp_path: Path, monkeypatch):
    """Criterion 4: the per-project `except Exception: continue` still absorbs a hard failure."""
    self_proj = _make_proj(tmp_path, "self_unreadable")
    _write_decisions(_make_proj(tmp_path, "other_boom"), [
        {"id": "b1", "decision": "never read", "tags": ["boomtag"]},
    ])
    _write_decisions(_make_proj(tmp_path, "other_fine"), [
        {"id": "g1", "decision": "read me", "tags": ["finetag"]},
    ])

    real_tail = federated_mod.read_jsonl_tail

    def _raising_tail(path, limit):
        if "other_boom" in str(path):
            raise OSError("simulated unreadable sibling")
        return real_tail(path, limit)

    monkeypatch.setattr(federated_mod, "read_jsonl_tail", _raising_tail)

    sig = gather_cross_project_signals(self_proj, home=tmp_path)

    assert sig["ok"] is True
    assert sig["scanned_projects"] == 2  # both were visited
    assert sig["decision_tags"] == {"finetag": 1}  # the broken one was skipped, not fatal
