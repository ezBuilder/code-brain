from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import config


def _write_config(root: Path, text: str) -> Path:
    path = root / ".ai" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_parses_nested_values_and_quoted_hash(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "version: 1\n"
        "feature:\n"
        "  enabled: true\n"
        "  label: \"value # preserved\" # comment\n"
        "  path: .ai/cache/code.sqlite\n",
    )

    payload = config.load_config(tmp_path)

    assert payload == {
        "version": 1,
        "feature": {
            "enabled": True,
            "label": "value # preserved",
            "path": ".ai/cache/code.sqlite",
        },
    }


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_config_rejects_final_symlink_without_external_read(tmp_path: Path) -> None:
    external = tmp_path / "outside.yaml"
    external.write_text("secret: outside\n", encoding="utf-8")
    root = tmp_path / "repo"
    path = root / ".ai" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    with pytest.raises(ValueError, match="unavailable or untrusted"):
        config.load_config(root)

    assert external.read_text(encoding="utf-8") == "secret: outside\n"


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_config_rejects_parent_symlink_without_external_read(tmp_path: Path) -> None:
    external = tmp_path / "outside-ai"
    external.mkdir()
    outside = external / "config.yaml"
    outside.write_text("secret: outside\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ai").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="unavailable or untrusted"):
        config.load_config(root)

    assert outside.read_text(encoding="utf-8") == "secret: outside\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_config_rejects_hardlinked_file(tmp_path: Path) -> None:
    external = tmp_path / "outside.yaml"
    external.write_text("secret: outside\n", encoding="utf-8")
    root = tmp_path / "repo"
    path = root / ".ai" / "config.yaml"
    path.parent.mkdir(parents=True)
    os.link(external, path)

    with pytest.raises(ValueError, match="unavailable or untrusted"):
        config.load_config(root)


@pytest.mark.skipif(os.name == "nt", reason="Unix permission semantics")
def test_config_rejects_group_writable_file(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "version: 1\n")
    path.chmod(0o666)

    with pytest.raises(ValueError, match="unavailable or untrusted"):
        config.load_config(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version: 1\nversion: 2\n", "duplicate config key"),
        ("root:\n    child: 1\n  sibling: 2\n", "inconsistent config indentation"),
        ("root:\n\tchild: 1\n", "invalid config indentation"),
        ("bad key: 1\n", "invalid config syntax"),
        ("value: \"unterminated\n", "unterminated quoted config value"),
        ("root: 1\n  child: 2\n", "inconsistent config indentation"),
    ],
)
def test_invalid_config_shapes_fail_without_echoing_values(
    tmp_path: Path,
    text: str,
    message: str,
) -> None:
    _write_config(tmp_path, text)

    with pytest.raises(ValueError, match=message) as exc_info:
        config.load_config(tmp_path)

    assert text.strip() not in str(exc_info.value)


def test_config_size_line_key_depth_and_value_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "CONFIG_MAX_BYTES", 64)
    _write_config(tmp_path, "value: " + ("x" * 100) + "\n")
    with pytest.raises(ValueError, match="unavailable or untrusted"):
        config.load_config(tmp_path)

    monkeypatch.setattr(config, "CONFIG_MAX_BYTES", 4096)
    monkeypatch.setattr(config, "CONFIG_MAX_LINES", 2)
    _write_config(tmp_path, "a: 1\nb: 2\nc: 3\n")
    with pytest.raises(ValueError, match="line limit"):
        config.load_config(tmp_path)

    monkeypatch.setattr(config, "CONFIG_MAX_LINES", 100)
    monkeypatch.setattr(config, "CONFIG_MAX_KEYS", 2)
    _write_config(tmp_path, "a: 1\nb: 2\nc: 3\n")
    with pytest.raises(ValueError, match="key limit"):
        config.load_config(tmp_path)

    monkeypatch.setattr(config, "CONFIG_MAX_KEYS", 100)
    monkeypatch.setattr(config, "CONFIG_MAX_DEPTH", 2)
    _write_config(tmp_path, "a:\n  b:\n    c:\n      d: 1\n")
    with pytest.raises(ValueError, match="nesting depth"):
        config.load_config(tmp_path)

    monkeypatch.setattr(config, "CONFIG_MAX_DEPTH", 16)
    monkeypatch.setattr(config, "CONFIG_MAX_VALUE_CHARS", 8)
    _write_config(tmp_path, "value: 123456789\n")
    with pytest.raises(ValueError, match="invalid config value"):
        config.load_config(tmp_path)


def test_missing_config_preserves_missing_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="config.yaml is missing"):
        config.load_config(tmp_path)


def test_parse_scalar_rejects_nul_and_oversized_value(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="invalid config scalar"):
        config.parse_scalar("bad\x00value")
    monkeypatch.setattr(config, "CONFIG_MAX_VALUE_CHARS", 4)
    with pytest.raises(ValueError, match="invalid config scalar"):
        config.parse_scalar("12345")
