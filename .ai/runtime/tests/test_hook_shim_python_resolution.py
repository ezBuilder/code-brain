"""Contract tests for the POSIX and PowerShell hook launch shims."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
POSIX_SHIM = ROOT / ".ai" / "bin" / "ai-hook"
POWERSHELL_SHIM = ROOT / ".ai" / "bin" / "ai-hook.ps1"


def _root_fixture(tmp_path: Path, *, python: bool) -> tuple[Path, Path, Path]:
    root = tmp_path / "project"
    (root / ".ai" / "runtime" / ".venv" / "bin").mkdir(parents=True)
    (root / ".ai").joinpath("config.yaml").write_text("project_name: test\n", encoding="utf-8")
    log = tmp_path / "launch.log"
    if python:
        executable = root / ".ai" / "runtime" / ".venv" / "bin" / "python"
        executable.write_text(
            "#!/bin/sh\nprintf 'python:%s\\n' \"$*\" >> \"$LAUNCH_LOG\"\nexit 0\n",
            encoding="utf-8",
        )
        executable.chmod(stat.S_IRWXU)
    return root, log, root / ".ai" / "runtime" / ".venv" / "bin" / "python"


def _run(shim: Path, root: Path, log: Path, *, python: bool) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(root),
        "LAUNCH_LOG": str(log),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if not python:
        fake_bin = root.parent / "bin"
        fake_bin.mkdir()
        uv = fake_bin / "uv"
        uv.write_text(
            "#!/bin/sh\nprintf 'uv:%s\\n' \"$*\" >> \"$LAUNCH_LOG\"\nexit 0\n",
            encoding="utf-8",
        )
        uv.chmod(stat.S_IRWXU)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(shim), "SessionStart", "--json"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_posix_venv_launch_has_no_import_probe(tmp_path: Path) -> None:
    root, log, _ = _root_fixture(tmp_path, python=True)
    result = _run(POSIX_SHIM, root, log, python=True)
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "python:-m ai_core.cli hook SessionStart --json"
    ]


def test_posix_missing_venv_uses_uv_fallback(tmp_path: Path) -> None:
    root, log, _ = _root_fixture(tmp_path, python=False)
    result = _run(POSIX_SHIM, root, log, python=False)
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"uv:run --project {root}/.ai/runtime python -m ai_core.cli hook SessionStart --json"
    ]


def test_powershell_shim_uses_same_single_launch_contract() -> None:
    text = POWERSHELL_SHIM.read_text(encoding="utf-8")
    assert '-c "import ai_core.cli"' not in text
    assert '& $Python -m ai_core.cli hook @args' in text
    assert 'uv run --project "$Root/.ai/runtime" python -m ai_core.cli hook @args' in text
