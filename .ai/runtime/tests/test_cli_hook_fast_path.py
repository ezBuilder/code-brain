from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import cli  # noqa: E402


def test_cli_import_keeps_general_command_modules_deferred() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / ".ai" / "runtime" / "src")}
    code = (
        "import sys; import ai_core.cli; "
        "blocked={'ai_core.doctor','ai_core.obs','ai_core.search','ai_core.render'}; "
        "assert not (blocked & set(sys.modules))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["hook", "SessionStart"], ("SessionStart", False, False)),
        (["hook", "PreToolUse", "--json"], ("PreToolUse", True, False)),
        (["--json", "hook", "Stop"], ("Stop", False, False)),
        (["--ci", "hook"], (None, False, True)),
        (["version"], None),
        (["hook", "Stop", "extra"], None),
    ],
)
def test_fast_hook_argument_contract(
    argv: list[str],
    expected: tuple[str | None, bool, bool] | None,
) -> None:
    assert cli._fast_hook_args(argv) == expected


def test_hook_fast_path_skips_full_parser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    emitted: list[object] = []
    monkeypatch.setattr(
        cli,
        "build_parser",
        lambda: (_ for _ in ()).throw(AssertionError("full parser")),
    )
    monkeypatch.setattr(cli, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "read_payload", lambda: {"dry": True})
    monkeypatch.setattr(
        cli,
        "handle_hook",
        lambda _root, hook_name, _payload: {"ok": True, "hook": hook_name},
    )
    monkeypatch.setattr(cli, "emit", lambda payload, **_kwargs: emitted.append(payload))

    assert cli.main(["hook", "DoctorSLOBaseline", "--json"]) == 0
    assert emitted == [{"ok": True, "hook": "DoctorSLOBaseline"}]
