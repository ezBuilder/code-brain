from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from ai_core import embedding, model_artifacts, reranker


class _FakeTokenizerInstance:
    def __init__(self) -> None:
        self.truncation: int | None = None
        self.padding: tuple[int | None, int] | None = None

    def enable_truncation(self, *, max_length: int) -> None:
        self.truncation = max_length

    def enable_padding(self, *, length: int | None, pad_id: int) -> None:
        self.padding = (length, pad_id)


def _install_fake_runtime_modules(monkeypatch: pytest.MonkeyPatch, calls: dict[str, object]) -> None:
    class FakeTokenizer:
        @staticmethod
        def from_str(value: str):
            calls["tokenizer"] = value
            return _FakeTokenizerInstance()

        @staticmethod
        def from_file(_path: str):
            raise AssertionError("runtime must not reopen tokenizer path")

    def inference_session(source, *, providers):
        calls["model"] = source
        calls.setdefault("models", []).append(source)
        calls["providers"] = providers
        return object()

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        types.SimpleNamespace(InferenceSession=inference_session),
    )
    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        types.SimpleNamespace(Tokenizer=FakeTokenizer),
    )


def _write_runtime_artifacts(module, root: Path, *, tokenizer: bytes = b'{"safe":true}') -> None:
    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    (cache / "model.onnx").write_bytes(b"trusted-model-bytes")
    (cache / "tokenizer.json").write_bytes(tokenizer)


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_runtime_constructs_only_from_trusted_memory_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    _write_runtime_artifacts(module, root)
    calls: dict[str, object] = {}
    _install_fake_runtime_modules(monkeypatch, calls)
    module.reset_runtime_cache()

    runtime = module._get_runtime(root)

    assert runtime is not None
    assert calls["model"] == b"trusted-model-bytes"
    assert calls["tokenizer"] == '{"safe":true}'
    assert calls["providers"] == ["CPUExecutionProvider"]
    assert not isinstance(calls["model"], str)
    module.reset_runtime_cache()


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_runtime_rejects_invalid_tokenizer_encoding_before_session_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    _write_runtime_artifacts(module, root, tokenizer=b"\xff\xfe")
    calls: dict[str, object] = {}
    _install_fake_runtime_modules(monkeypatch, calls)
    module.reset_runtime_cache()

    assert module._get_runtime(root) is None
    assert "model" not in calls
    module.reset_runtime_cache()


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_runtime_rejects_linked_artifact_before_optional_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    external = tmp_path / "external.onnx"
    external.write_bytes(b"external")
    (cache / "model.onnx").symlink_to(external)
    (cache / "tokenizer.json").write_bytes(b"{}")
    imported = {"called": False}

    class UnexpectedModule(types.ModuleType):
        def __getattr__(self, _name):
            imported["called"] = True
            raise AssertionError("untrusted model must fail before optional runtime use")

    monkeypatch.setitem(sys.modules, "onnxruntime", UnexpectedModule("onnxruntime"))
    monkeypatch.setitem(sys.modules, "tokenizers", UnexpectedModule("tokenizers"))
    module.reset_runtime_cache()

    assert module._get_runtime(root) is None
    assert imported["called"] is False
    module.reset_runtime_cache()


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_runtime_fails_closed_when_tokenizer_lacks_memory_constructor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    _write_runtime_artifacts(module, root)
    session_calls: list[object] = []

    class PathOnlyTokenizer:
        @staticmethod
        def from_file(_path: str):
            raise AssertionError("path fallback is forbidden")

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        types.SimpleNamespace(
            InferenceSession=lambda source, **_kwargs: session_calls.append(source) or object()
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        types.SimpleNamespace(Tokenizer=PathOnlyTokenizer),
    )
    module.reset_runtime_cache()

    assert module._get_runtime(root) is None
    assert session_calls == []
    module.reset_runtime_cache()


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_runtime_cache_reloads_after_artifact_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    _write_runtime_artifacts(module, root)
    calls: dict[str, object] = {}
    _install_fake_runtime_modules(monkeypatch, calls)
    module.reset_runtime_cache()

    first = module._get_runtime(root)
    cached = module._get_runtime(root)
    model_artifacts.atomic_write_private_bytes(
        module.model_cache_dir(root) / "model.onnx",
        b"replacement-model-bytes",
        root=root,
    )
    reloaded = module._get_runtime(root)

    assert first is cached
    assert reloaded is not None
    assert reloaded is not first
    assert calls["models"] == [b"trusted-model-bytes", b"replacement-model-bytes"]
    module.reset_runtime_cache()


@pytest.mark.skipif(os.name == "nt", reason="Unix permission and link semantics")
@pytest.mark.parametrize("module", [embedding, reranker])
@pytest.mark.parametrize("mutation", ["delete", "writable", "symlink"])
def test_model_runtime_cache_invalidates_when_artifact_becomes_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    mutation: str,
) -> None:
    root = tmp_path / "project"
    _write_runtime_artifacts(module, root)
    calls: dict[str, object] = {}
    _install_fake_runtime_modules(monkeypatch, calls)
    module.reset_runtime_cache()
    assert module._get_runtime(root) is not None
    model_path = module.model_cache_dir(root) / "model.onnx"
    if mutation == "delete":
        model_path.unlink()
    elif mutation == "writable":
        model_path.chmod(0o666)
    else:
        external = tmp_path / "external.onnx"
        external.write_bytes(b"external")
        model_path.unlink()
        model_path.symlink_to(external)

    assert module._get_runtime(root) is None
    key = str(module.model_cache_dir(root))
    assert key not in module._RUNTIME_CACHE
    assert key not in module._RUNTIME_CACHE_SIGNATURES
    assert len(calls["models"]) == 1
    module.reset_runtime_cache()


@pytest.mark.parametrize("module", [embedding, reranker])
def test_model_install_and_uninstall_drop_cached_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    root = tmp_path / "project"
    key = str(module.model_cache_dir(root))
    module._RUNTIME_CACHE[key] = ("session", "tokenizer")
    module._RUNTIME_CACHE_SIGNATURES[key] = ("signature",)
    monkeypatch.setattr(
        module,
        "install_model_files",
        lambda *_args, **_kwargs: {
            "ok": True,
            "cache_dir": key,
            "downloaded": [{"file": "model.onnx", "bytes": 1}],
            "skipped": [],
            "errors": [],
        },
    )

    module.install_model(root)

    assert key not in module._RUNTIME_CACHE
    assert key not in module._RUNTIME_CACHE_SIGNATURES

    cache = module.model_cache_dir(root)
    cache.mkdir(parents=True)
    (cache / "model.onnx").write_bytes(b"x")
    module._RUNTIME_CACHE[key] = ("session", "tokenizer")
    module._RUNTIME_CACHE_SIGNATURES[key] = ("signature",)

    result = module.uninstall_model(root)

    assert result["ok"] is True
    assert key not in module._RUNTIME_CACHE
    assert key not in module._RUNTIME_CACHE_SIGNATURES
