from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .private_write import (
    atomic_write_private_bytes,
    ensure_root_confined_directory,
    read_root_confined_bytes,
    validate_root_confined_regular_file,
)

_MODEL_ARTIFACT_MAX_BYTES = {
    "model.onnx": 256 * 1024 * 1024,
    "tokenizer.json": 32 * 1024 * 1024,
    "config.json": 4 * 1024 * 1024,
}
_DEFAULT_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024


def artifact_max_bytes(name: str) -> int:
    return _MODEL_ARTIFACT_MAX_BYTES.get(name, _DEFAULT_ARTIFACT_MAX_BYTES)


def read_model_artifact(root: Path, path: Path) -> bytes:
    """Read an owner-controlled, non-writable model artifact without following links."""
    data, _state = read_root_confined_bytes(
        path,
        root=root,
        max_bytes=artifact_max_bytes(path.name),
        require_private=False,
        require_owner=True,
        reject_group_other_writable=True,
    )
    if not data:
        raise OSError("model artifact is empty")
    return data


def artifact_present(root: Path, path: Path) -> bool:
    return artifact_signature(root, path) is not None


def artifact_signature(root: Path, path: Path) -> tuple[int, int, int, int, int, int] | None:
    """Return a trusted artifact identity or None when the path is unsafe/missing."""
    try:
        state = validate_root_confined_regular_file(
            path,
            root=root,
            min_bytes=1,
            max_bytes=artifact_max_bytes(path.name),
            require_owner=True,
            reject_group_other_writable=True,
        )
    except OSError:
        return None
    return (
        int(state.st_dev),
        int(state.st_ino),
        int(state.st_size),
        int(getattr(state, "st_mtime_ns", int(state.st_mtime * 1_000_000_000))),
        int(getattr(state, "st_ctime_ns", int(state.st_ctime * 1_000_000_000))),
        int(state.st_mode),
    )


def artifacts_present(root: Path, cache: Path, names: tuple[str, ...]) -> bool:
    return artifacts_signature(root, cache, names) is not None


def artifacts_signature(
    root: Path,
    cache: Path,
    names: tuple[str, ...],
) -> tuple[tuple[int, int, int, int, int, int], ...] | None:
    signatures = tuple(artifact_signature(root, cache / name) for name in names)
    if any(signature is None for signature in signatures):
        return None
    return tuple(signature for signature in signatures if signature is not None)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _read_response_bounded(response, *, max_bytes: int) -> bytes:
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            content_length = headers.get("Content-Length")
            if content_length is not None and int(content_length) > max_bytes:
                raise OSError(f"model artifact exceeds {max_bytes} bytes")
        except (TypeError, ValueError):
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        block = response.read(65536)
        if not block:
            break
        total += len(block)
        if total > max_bytes:
            raise OSError(f"model artifact exceeds {max_bytes} bytes")
        chunks.append(bytes(block))
    data = b"".join(chunks)
    if not data:
        raise OSError("model artifact download is empty")
    return data


def install_model_files(
    root: Path,
    cache: Path,
    files: Mapping[str, str],
    *,
    verify_only: bool = False,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Verify or install model artifacts through confined bounded atomic I/O."""
    result: dict[str, Any] = {
        "ok": True,
        "cache_dir": str(cache),
        "downloaded": [],
        "skipped": [],
        "errors": [],
    }
    try:
        ensure_root_confined_directory(cache, root=root, mode=0o700)
    except OSError as exc:
        result["ok"] = False
        result["errors"].append({"file": "<cache>", "reason": str(exc)[:200]})
        return result

    context = None if verify_only else _ssl_context()
    for name, url in files.items():
        target = cache / name
        if artifact_present(root, target):
            result["skipped"].append(name)
            continue
        if verify_only:
            result["errors"].append({"file": name, "reason": "missing_or_untrusted"})
            result["ok"] = False
            continue
        try:
            with urllib.request.urlopen(url, timeout=timeout_seconds, context=context) as response:
                data = _read_response_bounded(response, max_bytes=artifact_max_bytes(name))
            atomic_write_private_bytes(target, data, root=root)
            result["downloaded"].append({"file": name, "bytes": len(data)})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            result["errors"].append({"file": name, "reason": str(exc)[:200]})
            result["ok"] = False
    return result
