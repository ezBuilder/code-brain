from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .redact import redact_value

KIT_REL = Path("kits/global-agent-kit")
REQUIRED_FILES = (
    "README.md",
    "Makefile",
    "install.sh",
    "scripts/validate.sh",
    "scripts/doctor.sh",
    "scripts/codex-doctor.sh",
    "rules/CLAUDE.md",
    "rules/AGENTS.md",
)


def kit_path(root: Path) -> Path:
    return Path(root) / KIT_REL


def status(root: Path) -> dict[str, Any]:
    path = kit_path(root)
    missing = [rel for rel in REQUIRED_FILES if not (path / rel).exists()]
    payload = {
        "ok": path.is_dir() and not missing,
        "name": "code-brain-global-kit",
        "path": str(KIT_REL),
        "present": path.is_dir(),
        "missing": missing,
        "commands": {
            "validate": "ai kit validate --json",
            "install_dry_run": "kits/global-agent-kit/install.sh --all --dry-run",
        },
    }
    return redact_value(payload)


def validate(root: Path) -> dict[str, Any]:
    path = kit_path(root)
    stat = status(root)
    if not stat.get("ok"):
        return stat
    proc = subprocess.run(
        ["bash", "scripts/validate.sh"],
        cwd=path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return redact_value(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout_tail": _tail(proc.stdout),
            "stderr_tail": _tail(proc.stderr),
        }
    )


def _tail(text: str, *, limit: int = 30) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-limit:]
