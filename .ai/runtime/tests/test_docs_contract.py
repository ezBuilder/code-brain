from __future__ import annotations

import hashlib
from pathlib import Path

import ai_core.docs_contract as docs_contract_module
from ai_core.doctor import check_layout
from ai_core.docs_contract import (
    ARCHITECTURE_PATH,
    WORLD_CLASS_PATH,
    load_source_contract,
    validate_docs_contract,
    validate_document_texts,
)


ROOT = Path(__file__).resolve().parents[3]


def test_repository_docs_contract_is_current() -> None:
    contract = load_source_contract(ROOT)
    assert validate_docs_contract(ROOT, contract) == []


def test_docs_check_script_is_executable() -> None:
    script = ROOT / "scripts" / "docs-check.sh"
    data = script.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    assert script.stat().st_mode & 0o111, (
        "scripts/docs-check.sh must remain executable; "
        f"bytes={len(data)} blob={blob} sha256={hashlib.sha256(data).hexdigest()}"
    )


def test_docs_check_runs_strict_doctor() -> None:
    script = (ROOT / "scripts" / "docs-check.sh").read_text(encoding="utf-8")
    assert "uv run --project .ai/runtime ai doctor --strict --json" in script


def test_doctor_layout_surfaces_docs_contract_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        docs_contract_module,
        "validate_docs_contract",
        lambda _root, _contract: ["fixture docs drift"],
    )
    check = check_layout(ROOT)
    assert not check.ok
    assert "docs contract drift: fixture docs drift" in check.detail


def test_consumer_owned_upgrade_doc_does_not_trigger_source_contract(tmp_path: Path) -> None:
    for relative in (
        ".ai/AGENTS.md",
        ".ai/config.yaml",
        ".ai/.gitignore",
        ".ai/.gitattributes",
        ".ai/runtime/pyproject.toml",
        ".ai/runtime/.python-version",
        ".ai/bin/ai",
        ".ai/memory/queue/.tmp/.gitkeep",
        ".ai/memory/queue/processing/.gitkeep",
        ".ai/memory/queue/dead/.gitkeep",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    (tmp_path / ".ai/generated").mkdir(parents=True)
    (tmp_path / ".ai/memory/audit").mkdir(parents=True)
    upgrade_doc = tmp_path / "docs/WORLD_CLASS_AUTONOMOUS_UPGRADE.md"
    upgrade_doc.parent.mkdir(parents=True)
    upgrade_doc.write_text("consumer-owned\n", encoding="utf-8")

    check = check_layout(tmp_path)

    assert check.ok, check.detail


def test_doctor_count_marker_drift_is_rejected() -> None:
    contract = load_source_contract(ROOT)
    architecture = (ROOT / ARCHITECTURE_PATH).read_text(encoding="utf-8")
    world_class = (ROOT / WORLD_CLASS_PATH).read_text(encoding="utf-8")
    stale = architecture.replace(
        contract.doctor_marker,
        f"<!-- code-brain-contract: doctor-check-count={contract.doctor_check_count - 1} -->",
        1,
    )
    issues = validate_document_texts(
        contract,
        architecture_text=stale,
        world_class_text=world_class,
    )
    assert any("ARCHITECTURE.md: contract marker drift" in issue for issue in issues)


def test_eval_axis_marker_drift_is_rejected() -> None:
    contract = load_source_contract(ROOT)
    architecture = (ROOT / ARCHITECTURE_PATH).read_text(encoding="utf-8")
    world_class = (ROOT / WORLD_CLASS_PATH).read_text(encoding="utf-8")
    stale_axes = contract.eval_axes[:-1]
    stale = world_class.replace(
        contract.eval_marker,
        "<!-- code-brain-contract: eval-axes=" + ",".join(stale_axes) + " -->",
        1,
    )
    issues = validate_document_texts(
        contract,
        architecture_text=architecture,
        world_class_text=stale,
    )
    assert any("WORLD_CLASS_AUTONOMOUS_UPGRADE.md: contract marker drift" in issue for issue in issues)
