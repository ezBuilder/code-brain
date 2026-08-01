"""Install-lock marker release semantics for `ai embedding/reranker install`.

The background auto-install spawner that used to CLAIM this marker was removed
in -006 (the query path must never download; the reranker spawn even targeted a
command that did not exist). What remains contractual is the RELEASE side:
`install_model` releases an owned marker exactly once, and never releases a
marker owned by a newer claimant. test_network_defaults.py guards the removal
itself (no spawn from any activation path).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import embedding, reranker


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_install_completion_releases_owned_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    token = "owned-child-token"
    marker = cache / ".install-lock"
    marker.write_text(token, encoding="utf-8")
    if os.name != "nt":
        marker.chmod(0o600)
    monkeypatch.setenv(module._INSTALL_MARKER_ENV, token)
    monkeypatch.setattr(
        module,
        "install_model_files",
        lambda *_args, **_kwargs: {
            "ok": False,
            "cache_dir": str(cache),
            "downloaded": [],
            "skipped": [],
            "errors": [{"file": "model.onnx", "reason": "download failed"}],
        },
    )

    result = module.install_model(root)

    assert result["ok"] is False
    assert not marker.exists()


@pytest.mark.parametrize("module", [embedding, reranker])
def test_old_model_install_cannot_release_newer_owner_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    marker = cache / ".install-lock"
    marker.write_text("new-owner-token", encoding="utf-8")
    if os.name != "nt":
        marker.chmod(0o600)
    monkeypatch.setenv(module._INSTALL_MARKER_ENV, "old-owner-token")
    monkeypatch.setattr(
        module,
        "install_model_files",
        lambda *_args, **_kwargs: {
            "ok": True,
            "cache_dir": str(cache),
            "downloaded": [],
            "skipped": list(module._MODEL_FILES),
            "errors": [],
        },
    )

    module.install_model(root)

    assert marker.read_text(encoding="utf-8") == "new-owner-token"
