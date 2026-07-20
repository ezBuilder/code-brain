from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ai_core import render
from ai_core.doctor import check_gitattributes, check_manifest


def _source(root: Path, text: str = "# Contract\n") -> Path:
    path = root / ".ai" / "AGENTS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_render_rejects_linked_contract_source_without_external_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-contract.md"
    external.write_text("outside secret\n", encoding="utf-8")
    source = root / ".ai" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    source.symlink_to(external)

    with pytest.raises(OSError):
        render.render(root)

    assert external.read_text(encoding="utf-8") == "outside secret\n"
    assert not (root / "AGENTS.md").exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_render_rejects_hardlinked_contract_source_without_external_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-contract.md"
    external.write_text("outside secret\n", encoding="utf-8")
    source = root / ".ai" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    os.link(external, source)

    with pytest.raises(OSError):
        render.render(root)

    assert external.read_text(encoding="utf-8") == "outside secret\n"


@pytest.mark.skipif(os.name == "nt", reason="Unix permission semantics")
def test_render_rejects_group_writable_contract_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = _source(root)
    source.chmod(0o666)

    with pytest.raises(PermissionError):
        render.render(root)


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_render_repairs_linked_output_without_external_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    contract = "# Trusted Contract\n"
    _source(root, contract)
    external = tmp_path / "outside-agents.md"
    external.write_text("outside secret\n", encoding="utf-8")
    target = root / "AGENTS.md"
    target.symlink_to(external)

    payload = render.render(root)

    assert payload["planned"][1]["unsafe"] is True
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == contract
    assert external.read_text(encoding="utf-8") == "outside secret\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_render_repairs_hardlinked_output_without_external_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    contract = "# Trusted Contract\n"
    _source(root, contract)
    external = tmp_path / "outside-claude.md"
    external.write_text("outside secret\n", encoding="utf-8")
    target = root / "CLAUDE.md"
    os.link(external, target)

    render.render(root)

    assert target.stat().st_ino != external.stat().st_ino
    assert target.read_text(encoding="utf-8") == contract
    assert external.read_text(encoding="utf-8") == "outside secret\n"


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_render_rejects_external_generated_parent_before_writing_docs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _source(root)
    external = tmp_path / "outside-generated"
    external.mkdir()
    ai = root / ".ai"
    (ai / "generated").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        render.render(root)

    assert list(external.iterdir()) == []
    assert not (root / "AGENTS.md").exists()
    assert not (root / "CLAUDE.md").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_render_dry_run_reports_unsafe_output_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _source(root)
    external = tmp_path / "outside-agents.md"
    external.write_text("outside secret\n", encoding="utf-8")
    target = root / "AGENTS.md"
    target.symlink_to(external)

    payload = render.render(root, dry_run=True)

    agents = next(item for item in payload["planned"] if item["path"] == "AGENTS.md")
    assert agents == {"path": "AGENTS.md", "exists": True, "changed": True, "unsafe": True}
    assert target.is_symlink()
    assert external.read_text(encoding="utf-8") == "outside secret\n"


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_no_overwrite_refuses_unsafe_output_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _source(root)
    external = tmp_path / "outside-agents.md"
    external.write_text("outside secret\n", encoding="utf-8")
    target = root / "AGENTS.md"
    target.symlink_to(external)

    with pytest.raises(FileExistsError):
        render.render(root, no_overwrite=True)

    assert target.is_symlink()
    assert external.read_text(encoding="utf-8") == "outside secret\n"


def test_render_outputs_are_private_and_manifest_is_canonical(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    contract = "# Contract\n"
    _source(root, contract)

    payload = render.render(root)
    manifest = root / ".ai" / "generated" / "manifest.json"

    assert payload["planned"][0]["path"] == ".ai/generated/manifest.json"
    assert json.loads(manifest.read_text(encoding="utf-8")) == payload["manifest"]
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == contract
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == contract
    if os.name != "nt":
        for path in (manifest, root / "AGENTS.md", root / "CLAUDE.md"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_render_output_size_is_bounded_before_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _source(root, "x" * 1000)
    monkeypatch.setattr(render, "OUTPUT_MAX_BYTES", 128)

    with pytest.raises(ValueError, match="output exceeds size limit"):
        render.render(root)

    assert not (root / "AGENTS.md").exists()
    assert not (root / "CLAUDE.md").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_doctor_rejects_linked_manifest_and_gitattributes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external_manifest = tmp_path / "outside-manifest.json"
    external_manifest.write_text("{}\n", encoding="utf-8")
    manifest = root / ".ai" / "generated" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.symlink_to(external_manifest)
    external_attributes = tmp_path / "outside-gitattributes"
    external_attributes.write_text("* text=auto eol=lf\n", encoding="utf-8")
    attributes = root / ".ai" / ".gitattributes"
    attributes.symlink_to(external_attributes)

    manifest_check = check_manifest(root)
    attributes_check = check_gitattributes(root)

    assert manifest_check.ok is False
    assert manifest_check.detail == "manifest unavailable or untrusted"
    assert attributes_check.ok is False
    assert attributes_check.detail == "unavailable or untrusted"
    assert external_manifest.read_text(encoding="utf-8") == "{}\n"
