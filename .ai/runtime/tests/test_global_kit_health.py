from __future__ import annotations

from pathlib import Path

from ai_core.doctor import check_global_kit_install_drift, check_global_kit_source_health
from ai_core.global_kit_health import (
    check_global_kit_install,
    check_global_kit_source,
    load_global_kit_install_contract,
)


ROOT = Path(__file__).resolve().parents[3]
START = "<!-- code-brain-global-kit:start -->"
END = "<!-- code-brain-global-kit:end -->"


def _write(path: Path, text: str = "fixture\n", *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o755)


def _seed_installed_home(home: Path) -> None:
    kit = ROOT / "kits" / "global-agent-kit"
    contract = load_global_kit_install_contract(ROOT)
    for source_relative, target_relative in contract.managed_rules:
        source = kit.joinpath(*source_relative.parts).read_text(encoding="utf-8").strip()
        _write(
            home.joinpath(*target_relative.parts),
            f"{START}\n{source}\n{END}\n",
        )
    for relative in contract.files:
        _write(home.joinpath(*relative.parts), "{}\n" if relative.name == "settings.json" else "fixture\n")
    for relative in contract.executables:
        _write(home.joinpath(*relative.parts), "#!/usr/bin/env bash\nexit 0\n", executable=True)


def test_repository_global_kit_source_health_is_green() -> None:
    result = check_global_kit_source(ROOT)
    assert result.ok, result.detail
    assert "source inventory current" in result.detail


def test_source_health_rejects_missing_declared_file(tmp_path: Path) -> None:
    kit = tmp_path / "kits" / "global-agent-kit"
    _write(kit / "install.sh", "#!/usr/bin/env bash\n")
    _write(
        kit / "scripts" / "validate.sh",
        'required_files=(\n  "rules/CLAUDE.md"\n  "rules/AGENTS.md"\n)\n',
    )
    _write(kit / "rules" / "CLAUDE.md")

    result = check_global_kit_source(tmp_path)

    assert not result.ok
    assert "missing:rules/AGENTS.md" in result.detail


def test_install_drift_is_not_asserted_for_empty_home(tmp_path: Path) -> None:
    result = check_global_kit_install(ROOT, home=tmp_path)
    assert result.ok
    assert "not installed" in result.detail


def test_current_temp_home_install_is_green(tmp_path: Path) -> None:
    _seed_installed_home(tmp_path)

    result = check_global_kit_install(ROOT, home=tmp_path)

    assert result.ok, result.detail
    assert "installed contract current" in result.detail


def test_managed_rule_tamper_is_install_drift_red(tmp_path: Path) -> None:
    _seed_installed_home(tmp_path)
    target = tmp_path / ".claude" / "CLAUDE.md"
    target.write_text(target.read_text(encoding="utf-8").replace("Verify before claiming success", "Skip verification"), encoding="utf-8")

    result = check_global_kit_install(ROOT, home=tmp_path)

    assert not result.ok
    assert "managed-rule:.claude/CLAUDE.md:drift" in result.detail


def test_doctor_exposes_source_and_install_checks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    source = check_global_kit_source_health(ROOT)
    installed = check_global_kit_install_drift(ROOT)

    assert source.name == "global_kit_source_health"
    assert source.ok, source.detail
    assert installed.name == "global_kit_install_drift"
    assert installed.ok, installed.detail
