from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ai_core import embedding, model_artifacts, reranker


class _Response:
    def __init__(self, data: bytes, *, content_length: int | None = None) -> None:
        self._data = data
        self._offset = 0
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int) -> bytes:
        if self._offset >= len(self._data):
            return b""
        block = self._data[self._offset : self._offset + size]
        self._offset += len(block)
        return block


def _fake_urlopen_factory(data: bytes):
    def fake_urlopen(_url, **_kwargs):
        return _Response(data, content_length=len(data))

    return fake_urlopen


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("module", [embedding, reranker])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_model_install_replaces_linked_artifact_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    link_kind: str,
) -> None:
    root = tmp_path / "project"
    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    target = cache / "model.onnx"
    external = tmp_path / f"external-{link_kind}.onnx"
    external.write_bytes(b"external-model")
    external.chmod(0o600)
    if link_kind == "symlink":
        target.symlink_to(external)
    else:
        os.link(external, target)
    downloaded = b"trusted-downloaded-artifact"
    monkeypatch.setattr(
        model_artifacts.urllib.request,
        "urlopen",
        _fake_urlopen_factory(downloaded),
    )
    monkeypatch.setattr(model_artifacts, "_ssl_context", lambda: None)

    result = module.install_model(root)

    assert result["ok"] is True
    assert {entry["file"] for entry in result["downloaded"]} == set(module._MODEL_FILES)
    assert external.read_bytes() == b"external-model"
    assert target.read_bytes() == downloaded
    assert not target.is_symlink()
    assert target.stat().st_nlink == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_verify_rejects_writable_artifact_without_reading_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    for name in module._MODEL_FILES:
        path = cache / name
        path.write_bytes(b"present")
        path.chmod(0o644)
    (cache / "model.onnx").chmod(0o666)

    monkeypatch.setattr(
        model_artifacts.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("verify-only must not fetch")),
    )

    result = module.install_model(root, verify_only=True)

    assert result["ok"] is False
    assert {entry["file"] for entry in result["errors"]} == {"model.onnx"}
    assert set(result["skipped"]) == {"tokenizer.json", "config.json"}
    assert module.is_model_present(root) is False


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_install_rejects_oversized_response_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    limit = 8
    monkeypatch.setitem(model_artifacts._MODEL_ARTIFACT_MAX_BYTES, "model.onnx", limit)

    def fake_urlopen(url, **_kwargs):
        name = next(name for name, value in module._MODEL_FILES.items() if value == url)
        data = b"x" * (limit + 1) if name == "model.onnx" else b"safe"
        return _Response(data, content_length=len(data))

    monkeypatch.setattr(model_artifacts.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(model_artifacts, "_ssl_context", lambda: None)

    result = module.install_model(root)

    assert result["ok"] is False
    assert not (module.model_cache_dir(root) / "model.onnx").exists()
    error = next(entry for entry in result["errors"] if entry["file"] == "model.onnx")
    assert "exceeds" in error["reason"]


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_install_rejects_external_cache_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / ".ai").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(
        model_artifacts.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe cache must not fetch")),
    )

    result = module.install_model(root)

    assert result["ok"] is False
    assert result["errors"][0]["file"] == "<cache>"
    assert not (external / "cache").exists()


def test_atomic_private_bytes_and_confined_reader_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "artifact.bin"
    payload = bytes(range(256))

    model_artifacts.atomic_write_private_bytes(path, payload, root=root)
    loaded = model_artifacts.read_model_artifact(root, path)

    assert loaded == payload
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
@pytest.mark.parametrize("module", [embedding, reranker])
@pytest.mark.parametrize("link_location", ["parent", "final"])
def test_model_uninstall_rejects_external_link_without_deleting_target(
    tmp_path: Path,
    module,
    link_location: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / f"external-{link_location}"
    if link_location == "parent":
        target = external / "cache" / module.model_cache_dir(root).name
        target.mkdir(parents=True)
        (root / ".ai").symlink_to(external, target_is_directory=True)
    else:
        target = external
        target.mkdir()
        cache = module.model_cache_dir(root)
        cache.parent.mkdir(parents=True)
        cache.symlink_to(target, target_is_directory=True)
    payload = target / "model.onnx"
    payload.write_bytes(b"external-model")
    key = str(module.model_cache_dir(root))
    module._RUNTIME_CACHE[key] = ("stale", "runtime")
    module._RUNTIME_CACHE_SIGNATURES[key] = ("stale",)

    result = module.uninstall_model(root)

    assert result["ok"] is False
    assert payload.read_bytes() == b"external-model"
    assert key not in module._RUNTIME_CACHE
    assert key not in module._RUNTIME_CACHE_SIGNATURES


@pytest.mark.skipif(os.name == "nt", reason="Unix child symlink semantics")
@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_uninstall_removes_cache_without_following_child_symlink(
    tmp_path: Path,
    module,
) -> None:
    root = tmp_path / "project"
    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    external = tmp_path / "external-child"
    external.mkdir()
    payload = external / "preserved.bin"
    payload.write_bytes(b"preserve")
    (cache / "linked-child").symlink_to(external, target_is_directory=True)
    (cache / "model.onnx").write_bytes(b"cached")

    result = module.uninstall_model(root)

    assert result == {"ok": True, "removed": True, "cache_dir": str(cache)}
    assert not cache.exists()
    assert payload.read_bytes() == b"preserve"
