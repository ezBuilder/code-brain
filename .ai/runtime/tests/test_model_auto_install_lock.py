from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from ai_core import embedding, process_janitor, reranker


class _FakeProcess:
    pid = 424242


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize(
    ("module", "kind"),
    [(embedding, "embedding"), (reranker, "reranker")],
)
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_model_auto_install_repairs_linked_marker_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    kind: str,
    link_kind: str,
) -> None:
    root = tmp_path / "project"
    ai_bin = root / ".ai" / "bin" / "ai"
    ai_bin.parent.mkdir(parents=True)
    ai_bin.write_text("placeholder\n", encoding="utf-8")
    ai_bin.chmod(0o755)
    marker = module.model_cache_dir(root) / ".install-lock"
    marker.parent.mkdir(parents=True)
    external = tmp_path / f"external-{kind}-{link_kind}.txt"
    external.write_text("external\n", encoding="utf-8")
    external.chmod(0o600)
    if link_kind == "symlink":
        marker.symlink_to(external)
    else:
        os.link(external, marker)

    spawned: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        spawned.append(list(command))
        assert _kwargs["env"][module._INSTALL_MARKER_ENV] == f"{kind}:owned-token"
        return _FakeProcess()

    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _length: "owned-token")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(process_janitor, "cleanup_children", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(process_janitor, "register_child", lambda *_args, **_kwargs: None)

    module._maybe_spawn_background_install(root)

    assert external.read_text(encoding="utf-8") == "external\n"
    assert marker.read_text(encoding="utf-8") == f"{kind}:owned-token"
    assert not marker.is_symlink()
    assert marker.stat().st_nlink == 1
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert spawned == [[str(ai_bin), kind, "install", "--json"]]


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_auto_install_does_not_claim_when_launcher_is_missing(
    tmp_path: Path,
    module,
) -> None:
    root = tmp_path / "project"

    module._maybe_spawn_background_install(root)

    assert not (module.model_cache_dir(root) / ".install-lock").exists()


@pytest.mark.parametrize(
    ("module", "kind"),
    [(embedding, "embedding"), (reranker, "reranker")],
)
def test_model_auto_install_reclaims_far_future_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    kind: str,
) -> None:
    root = tmp_path / "project"
    ai_bin = root / ".ai" / "bin" / "ai"
    ai_bin.parent.mkdir(parents=True)
    ai_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ai_bin.chmod(0o755)
    marker = module.model_cache_dir(root) / ".install-lock"
    marker.parent.mkdir(parents=True)
    marker.write_text("future-lock", encoding="utf-8")
    if os.name != "nt":
        marker.chmod(0o600)
    future = time.time() + 86_400
    os.utime(marker, (future, future))
    spawned: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        spawned.append(list(command))
        assert _kwargs["env"][module._INSTALL_MARKER_ENV] == f"{kind}:owned-token"
        return _FakeProcess()

    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _length: "owned-token")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(process_janitor, "cleanup_children", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(process_janitor, "register_child", lambda *_args, **_kwargs: None)

    module._maybe_spawn_background_install(root)

    assert spawned == [[str(ai_bin), kind, "install", "--json"]]
    assert marker.read_text(encoding="utf-8") == f"{kind}:owned-token"
    assert marker.stat().st_mtime < future


@pytest.mark.skipif(os.name == "nt", reason="Unix launcher trust semantics")
@pytest.mark.parametrize("module", [embedding, reranker])
@pytest.mark.parametrize("launcher_kind", ["symlink", "hardlink", "fifo", "writable"])
def test_model_auto_install_rejects_untrusted_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    launcher_kind: str,
) -> None:
    root = tmp_path / "project"
    ai_bin = root / ".ai" / "bin" / "ai"
    ai_bin.parent.mkdir(parents=True)
    if launcher_kind in {"symlink", "hardlink"}:
        external = tmp_path / f"external-launcher-{launcher_kind}"
        external.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        external.chmod(0o755)
        if launcher_kind == "symlink":
            ai_bin.symlink_to(external)
        else:
            os.link(external, ai_bin)
    elif launcher_kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(ai_bin)
    else:
        ai_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        ai_bin.chmod(0o777)

    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("untrusted launcher must not be executed")

    monkeypatch.setattr(subprocess, "Popen", unexpected_popen)

    module._maybe_spawn_background_install(root)

    assert not (module.model_cache_dir(root) / ".install-lock").exists()


@pytest.mark.parametrize(
    ("module", "kind"),
    [(embedding, "embedding"), (reranker, "reranker")],
)
def test_model_auto_install_spawn_failure_releases_owned_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    kind: str,
) -> None:
    root = tmp_path / "project"
    ai_bin = root / ".ai" / "bin" / "ai"
    ai_bin.parent.mkdir(parents=True)
    ai_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ai_bin.chmod(0o755)
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _length: "spawn-failure")
    monkeypatch.setattr(process_janitor, "cleanup_children", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    module._maybe_spawn_background_install(root)

    marker = module.model_cache_dir(root) / ".install-lock"
    assert not marker.exists()


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


@pytest.mark.parametrize(
    ("module", "kind"),
    [(embedding, "embedding"), (reranker, "reranker")],
)
def test_running_model_install_marker_suppresses_duplicate_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    kind: str,
) -> None:
    root = tmp_path / "project"
    ai_bin = root / ".ai" / "bin" / "ai"
    ai_bin.parent.mkdir(parents=True)
    ai_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ai_bin.chmod(0o755)
    tokens = iter(["first-owner", "second-owner"])
    spawned: list[dict[str, object]] = []

    def fake_popen(command, **kwargs):
        spawned.append({"command": list(command), "env": kwargs["env"]})
        return _FakeProcess()

    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _length: next(tokens))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(process_janitor, "cleanup_children", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(process_janitor, "register_child", lambda *_args, **_kwargs: None)

    module._maybe_spawn_background_install(root)
    module._maybe_spawn_background_install(root)

    assert len(spawned) == 1
    assert spawned[0]["command"] == [str(ai_bin), kind, "install", "--json"]
    assert spawned[0]["env"][module._INSTALL_MARKER_ENV] == f"{kind}:first-owner"
    assert (module.model_cache_dir(root) / ".install-lock").read_text(encoding="utf-8") == (
        f"{kind}:first-owner"
    )