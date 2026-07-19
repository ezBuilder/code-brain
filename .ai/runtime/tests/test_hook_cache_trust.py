from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ai_core import hooks


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