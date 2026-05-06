from __future__ import annotations

import hashlib
import json
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
    git = {
        "branch": git_output(root, "branch", "--show-current"),
        "head": git_output(root, "rev-parse", "--short", "HEAD"),
        "head_12": git_output(root, "rev-parse", "--short=12", "HEAD"),
        "status_short": git_output(root, "status", "--short"),
    }
    artifacts = release_artifacts(root, git=git)
    return {
        "ok": bool(doctor["ok"]),
        "release_ready": bool(doctor["ok"] and artifacts["all_present"] and artifacts["all_valid"] and artifacts["all_current"]),
        "runtime_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "git": git,
        "doctor": doctor,
        "metrics": metrics(root),
        "release_artifact": artifacts["archive"],
        "release_artifacts": artifacts,
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
            f"- Release ready: `{'yes' if report['release_ready'] else 'no'}`",
            f"- Archive: `{report['release_artifact']['archive']}`",
            f"- SHA-256: `{report['release_artifact']['sha256'] or 'missing'}`",
            f"- Manifest: `{report['release_artifacts']['manifest']['path']}`",
            f"- SBOM: `{report['release_artifacts']['sbom']['path']}`",
            f"- Provenance: `{report['release_artifacts']['provenance']['path']}`",
            f"- Release notes: `{report['release_artifacts']['release_notes']['path']}`",
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
            "./scripts/env-check.sh",
            "./scripts/lint.sh",
            "./scripts/smoke.sh",
            "./scripts/docs-check.sh",
            "./scripts/package.sh",
            "./scripts/verify-artifacts.sh dist/code-brain-0.1.0.tar.gz",
            "./scripts/install-check.sh",
            "./scripts/artifact-tamper-check.sh dist/code-brain-0.1.0.tar.gz",
            "./scripts/release-gate.sh",
            "uv run --project .ai/runtime ai doctor --strict --json",
            "uv run --project .ai/runtime ai report status --json",
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


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def artifact_entry(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "exists": path.exists(),
        "sha256": file_sha256(path),
    }


def release_artifacts(root: Path, *, git: dict[str, str] | None = None) -> dict[str, Any]:
    archive = root / "dist" / f"code-brain-{__version__}.tar.gz"
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    manifest = root / "dist" / f"code-brain-{__version__}.manifest.json"
    sbom = root / "dist" / f"code-brain-{__version__}.sbom.json"
    provenance = root / "dist" / f"code-brain-{__version__}.provenance.json"
    release_notes = root / "dist" / f"code-brain-{__version__}.release-notes.md"

    archive_entry = {
        "archive": archive.relative_to(root).as_posix(),
        "archive_exists": archive.exists(),
        "checksum": checksum.relative_to(root).as_posix(),
        "checksum_exists": checksum.exists(),
        "sha256": read_checksum(checksum),
        "computed_sha256": file_sha256(archive),
        "checksum_valid": checksum_matches(archive, checksum),
    }
    manifest_entry = artifact_entry(root, manifest) | manifest_summary(manifest)
    sbom_entry = artifact_entry(root, sbom) | sbom_summary(root, sbom)
    provenance_entry = artifact_entry(root, provenance) | provenance_summary(
        archive, manifest, sbom, provenance, release_notes, git=git
    )
    release_notes_entry = artifact_entry(root, release_notes) | release_notes_summary(
        archive, manifest, sbom, provenance, release_notes
    )
    return {
        "archive": archive_entry,
        "manifest": manifest_entry,
        "sbom": sbom_entry,
        "provenance": provenance_entry,
        "release_notes": release_notes_entry,
        "all_present": all(
            (
                archive.exists(),
                checksum.exists(),
                manifest.exists(),
                sbom.exists(),
                provenance.exists(),
                release_notes.exists(),
            )
        ),
        "all_valid": all(
            (
                archive_entry["checksum_valid"],
                manifest_entry["valid"],
                sbom_entry["valid"],
                provenance_entry["valid"],
                release_notes_entry["valid"],
            )
        ),
        "all_current": bool(provenance_entry["current"]),
    }


def checksum_matches(archive: Path, checksum: Path) -> bool:
    expected = read_checksum(checksum)
    actual = file_sha256(archive)
    return bool(expected and actual and expected == actual)


def manifest_summary(path: Path) -> dict[str, Any]:
    payload = json_payload(path)
    if payload is None:
        return {"valid": False, "file_count": None, "archive_sha256": None}
    files = payload.get("files")
    file_count = payload.get("file_count")
    return {
        "valid": isinstance(files, list) and file_count == len(files),
        "file_count": file_count,
        "archive_sha256": payload.get("archive_sha256"),
    }


def sbom_summary(root: Path, path: Path) -> dict[str, Any]:
    payload = json_payload(path)
    lock_path = root / ".ai" / "runtime" / "uv.lock"
    if payload is None:
        return {"valid": False, "package_count": None, "lockfile_sha256": None, "lockfile_valid": False}
    packages = payload.get("packages")
    package_count = payload.get("package_count")
    lockfile_sha = payload.get("lockfile_sha256")
    return {
        "valid": isinstance(packages, list) and package_count == len(packages) and lockfile_sha == file_sha256(lock_path),
        "package_count": package_count,
        "lockfile_sha256": lockfile_sha,
        "lockfile_valid": lockfile_sha == file_sha256(lock_path),
    }


def provenance_summary(
    archive: Path,
    manifest: Path,
    sbom: Path,
    provenance: Path,
    release_notes: Path,
    *,
    git: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = json_payload(provenance)
    if payload is None:
        return {"valid": False, "git": {}, "subjects_valid": False, "current": False, "git_head_matches": False}
    subjects = payload.get("subjects", {})
    required = [archive, manifest, sbom, release_notes]
    subjects_valid = isinstance(subjects, dict) and all(subjects.get(path.name) == file_sha256(path) for path in required)
    provenance_git = payload.get("git", {})
    current_head = (git or {}).get("head_12", "")
    current_status = (git or {}).get("status_short", "")
    provenance_head = provenance_git.get("head") if isinstance(provenance_git, dict) else None
    provenance_status = provenance_git.get("status_short") if isinstance(provenance_git, dict) else None
    git_head_matches = bool(current_head and provenance_head == current_head)
    git_status_clean = provenance_status == ""
    current_git_clean = current_status == ""
    return {
        "valid": subjects_valid and isinstance(provenance_git, dict),
        "git": provenance_git if isinstance(provenance_git, dict) else {},
        "subjects_valid": subjects_valid,
        "git_head_matches": git_head_matches,
        "git_status_clean": git_status_clean,
        "current_git_clean": current_git_clean,
        "current": bool(git_head_matches and git_status_clean and current_git_clean),
    }


def release_notes_summary(archive: Path, manifest: Path, sbom: Path, provenance: Path, release_notes: Path) -> dict[str, Any]:
    if not release_notes.exists():
        return {"valid": False}
    text = release_notes.read_text(encoding="utf-8")
    required = [
        f"# Code Brain {__version__} Release Notes",
        file_sha256(archive) or "",
        manifest.name,
        sbom.name,
        provenance.name,
        "./scripts/release-gate.sh",
    ]
    return {"valid": all(needle and needle in text for needle in required)}
