from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .doctor import as_payload, run_checks
from .obs import metrics
from .worker.ipc import PROTOCOL_VERSION


def git_output(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def status_report(root: Path) -> dict[str, Any]:
    doctor = as_payload(run_checks(root))
    archive = root / "dist" / f"code-brain-{__version__}.tar.gz"
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    return {
        "ok": bool(doctor["ok"]),
        "runtime_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "git": {
            "branch": git_output(root, "branch", "--show-current"),
            "head": git_output(root, "rev-parse", "--short", "HEAD"),
            "status_short": git_output(root, "status", "--short"),
        },
        "doctor": doctor,
        "metrics": metrics(root),
        "release_artifact": {
            "archive": archive.relative_to(root).as_posix(),
            "archive_exists": archive.exists(),
            "checksum": checksum.relative_to(root).as_posix(),
            "checksum_exists": checksum.exists(),
            "sha256": read_checksum(checksum),
        },
    }


def release_notes(root: Path) -> str:
    commits = git_output(root, "log", "--oneline", "--decorate", "-12")
    report = status_report(root)
    return "\n".join(
        [
            f"# Code Brain {__version__} Release Notes",
            "",
            "## Status",
            "",
            f"- Runtime version: `{__version__}`",
            f"- Protocol version: `{PROTOCOL_VERSION}`",
            f"- Git HEAD: `{report['git']['head']}`",
            f"- Doctor: `{'ok' if report['doctor']['ok'] else 'failed'}`",
            f"- Archive: `{report['release_artifact']['archive']}`",
            f"- SHA-256: `{report['release_artifact']['sha256'] or 'missing'}`",
            "",
            "## Recent Commits",
            "",
            "```text",
            commits,
            "```",
            "",
            "## Verification",
            "",
            "```bash",
            "./bootstrap.sh",
            "./scripts/smoke.sh",
            "./scripts/package.sh",
            "./scripts/install-check.sh",
            "uv run --project .ai/runtime ai doctor --strict --json",
            "git status --short",
            "```",
            "",
        ]
    )


def read_checksum(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text.split()[0] if text else None

