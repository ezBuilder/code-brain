from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_core import hooks


def _link_sibling_state(tmp_path: Path, target: Path, name: str, content: str, kind: str) -> Path:
    sibling = tmp_path.parent / f"{tmp_path.name}-{name}"
    sibling.write_text(content, encoding="utf-8")
    if os.name != "nt":
        sibling.chmod(0o600)
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        if os.name == "nt":
            pytest.skip("Unix symlink semantics")
        target.symlink_to(sibling)
    else:
        if not hasattr(os, "link"):
            pytest.skip("hard links unavailable")
        os.link(sibling, target)
    return sibling


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_hook_summary_cache_never_injects_external_symlink_content(tmp_path: Path) -> None:
    cache = tmp_path / ".ai" / "cache" / "summary.json"
    cache.parent.mkdir(parents=True)
    external = tmp_path / "external-summary.json"
    external.write_text(json.dumps({"extra": [], "text": "EXTERNAL_INJECTION"}), encoding="utf-8")
    cache.symlink_to(external)
    calls = {"count": 0}

    def compute() -> str:
        calls["count"] += 1
        return "SAFE_SUMMARY"

    result = hooks._cached_hook_summary(
        tmp_path,
        cache_name="summary",
        deps=[],
        compute=compute,
    )

    assert result == "SAFE_SUMMARY"
    assert calls["count"] == 1
    assert not cache.is_symlink()
    assert external.read_text(encoding="utf-8").find("EXTERNAL_INJECTION") >= 0
    if os.name != "nt":
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hook_recommend_cache_never_uses_external_hardlink(tmp_path: Path) -> None:
    cache = tmp_path / ".ai" / "cache" / "recommend.json"
    cache.parent.mkdir(parents=True)
    external = tmp_path / "external-recommend.json"
    external.write_text(
        json.dumps(
            {
                "min_signal": 1,
                "extra": [],
                "result": {"candidates": [{"name": "EXTERNAL_INJECTION"}]},
            }
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        external.chmod(0o600)
    os.link(external, cache)
    calls = {"count": 0}

    def compute() -> dict:
        calls["count"] += 1
        return {"candidates": [{"name": "SAFE_RECOMMENDATION"}]}

    result = hooks._cached_recommend_invoke(
        tmp_path,
        cache_name="recommend",
        deps=[],
        compute=compute,
        min_signal=1,
    )

    assert result == {"candidates": [{"name": "SAFE_RECOMMENDATION"}]}
    assert calls["count"] == 1
    assert cache.stat().st_ino != external.stat().st_ino
    assert "EXTERNAL_INJECTION" in external.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Unix private mode")
def test_public_hook_cache_is_ignored_and_rewritten_private(tmp_path: Path) -> None:
    cache = tmp_path / ".ai" / "cache" / "summary.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"extra": [], "text": "PUBLIC_INJECTION"}), encoding="utf-8")
    cache.chmod(0o644)

    result = hooks._cached_hook_summary(
        tmp_path,
        cache_name="summary",
        deps=[],
        compute=lambda: "SAFE_PUBLIC_REPLACEMENT",
    )

    assert result == "SAFE_PUBLIC_REPLACEMENT"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_hook_audit_readers_ignore_linked_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    records = [
        {"ts": ts, "action": "skill.recommend_pending", "payload": {"id": "linked"}},
        {"ts": ts, "action": "skill.accept", "payload": {"id": "linked"}},
        {"ts": ts, "action": "skill.auto_accept", "payload": {"id": "linked"}},
    ]
    audit = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    _link_sibling_state(
        tmp_path,
        audit,
        f"linked-audit-{link_kind}.jsonl",
        "".join(json.dumps(record) + "\n" for record in records),
        link_kind,
    )
    monkeypatch.setattr(hooks, "all_audit_files", lambda _root: [audit])

    assert hooks._recently_surfaced_ids(tmp_path, 24.0) == set()
    assert hooks._cooldown_weights(tmp_path, 12.0) == {}
    assert hooks._adaptive_half_life(tmp_path, 12.0) == 12.0
    assert hooks._adaptive_min_signal_from_satisfaction(tmp_path, 3) == 3
    assert hooks._satisfaction_summary_context_uncached(tmp_path) == ""
    assert "surfaced" not in hooks._compact_meta_line(tmp_path)

    from ai_core import recommend as recommend_mod

    calls = {"list_catalog": 0}

    def list_catalog(_root: Path) -> list[object]:
        calls["list_catalog"] += 1
        return []

    monkeypatch.setattr(recommend_mod, "list_catalog", list_catalog)
    hooks._try_autonomous_accept(tmp_path, "Stop")
    assert calls["list_catalog"] == 1


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_failure_live_versions_ignores_linked_state(tmp_path: Path, link_kind: str) -> None:
    versions = tmp_path / ".ai" / "memory" / "env-versions.json"
    _link_sibling_state(
        tmp_path,
        versions,
        f"linked-env-versions-{link_kind}.json",
        json.dumps({"python": "linked-version"}),
        link_kind,
    )

    assert hooks._failure_live_versions(tmp_path) == {}