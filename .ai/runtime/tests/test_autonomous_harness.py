from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.autonomous_harness import analyze, context_line, directive, requested  # noqa: E402


def test_analyze_bootstrap_project_without_user_init(tmp_path: Path) -> None:
    payload = analyze(tmp_path)

    assert payload["ok"] is True
    assert payload["should_use"] is True
    assert payload["mode"] == "bootstrap"
    assert payload["completion_target"] == 0.95
    assert ".env*" in payload["policy"]["protected_paths"]


def test_analyze_hardening_project_with_sources_and_tests(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    for index in range(5):
        (src / f"m{index}.py").write_text("print('x')\n", encoding="utf-8")
    (tests / "test_m.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    payload = analyze(tmp_path)

    assert payload["mode"] == "hardening"
    assert payload["signals"]["source_files"] == 5
    assert payload["signals"]["test_files"] == 1
    assert "pyproject.toml" in payload["signals"]["dependency_manifests"]


def test_analyze_code_brain_runtime_layout(tmp_path: Path) -> None:
    src = tmp_path / ".ai" / "runtime" / "src" / "ai_core"
    tests = tmp_path / ".ai" / "runtime" / "tests"
    src.mkdir(parents=True)
    tests.mkdir(parents=True)
    for index in range(5):
        (src / f"m{index}.py").write_text("print('x')\n", encoding="utf-8")
    (tests / "test_m.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    payload = analyze(tmp_path)

    assert payload["mode"] == "hardening"
    assert payload["signals"]["source_files"] == 5
    assert payload["signals"]["test_files"] == 1


def test_context_line_is_short_and_actionable(tmp_path: Path) -> None:
    line = context_line(tmp_path)

    assert "Autonomous harness:" in line
    assert "target=95%" in line
    assert "self-apply" in line
    assert len(line.encode("utf-8")) < 600


def test_requested_detects_korean_harness_command() -> None:
    assert requested({"prompt": "신규 프로젝트에 하네스 적용해서 95%까지 자율 개선해"})
    assert not requested({"prompt": "상태만 확인해"})


def test_directive_says_no_separate_command_needed(tmp_path: Path) -> None:
    text = directive(tmp_path, explicit=True)

    assert "Explicit harness request detected" in text
    assert "Do not wait for a separate `ai harness` command" in text
    assert "target=95%" in text
