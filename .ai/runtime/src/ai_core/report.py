from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .doctor import as_payload, run_checks
from .loss_accounting import summary as loss_accounting_summary
from .obs import metrics
from .redact import redact_value
from .runner_observe import observation_status
from .sandbox import execution_diagnostics
from .worker.ipc import PROTOCOL_VERSION

RELEASE_GATE_SUMMARY_SCHEMA_VERSION = 4
RELEASE_GATE_SUMMARY_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "git_sha",
        "ci",
        "release_ready",
        "release_artifacts",
        "dep_advisory",
        "operational_bounds",
        "checks",
    }
)

OPERATIONAL_CHECK_GROUPS = {
    "audit": ("generated_artifacts_bounded", "audit_index", "audit_chain"),
    "sqlite": ("index_control", "index_storage", "index_freshness"),
    "retention_backup": ("runtime_retention", "loss_accounting"),
    "diagnostics": ("diagnostics_dry_run",),
}
TRANSCRIPT_POLICY_FIELDS = {
    "max_file_bytes",
    "max_line_bytes",
    "max_scan_bytes",
    "max_sessions",
    "max_candidates",
    "max_scan_seconds",
    "max_dedupe_keys",
}


def git_output(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def status_report(root: Path, *, include_usage: bool = True) -> dict[str, Any]:
    doctor = as_payload(run_checks(root))
    git = {
        "branch": git_output(root, "branch", "--show-current"),
        "head": git_output(root, "rev-parse", "--short", "HEAD"),
        "head_12": git_output(root, "rev-parse", "--short=12", "HEAD"),
        "status_short": git_output(root, "status", "--short"),
    }
    artifacts = release_artifacts(root, git=git)
    metrics_payload = metrics(root, include_usage=include_usage)
    bounds = operational_bounds_summary(
        root,
        doctor=doctor,
        metrics_payload=metrics_payload,
    )
    return {
        "ok": bool(doctor["ok"]),
        "release_ready": bool(
            doctor["ok"]
            and bounds["ok"]
            and artifacts["all_present"]
            and artifacts["all_valid"]
            and artifacts["all_current"]
        ),
        "runtime_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "git": git,
        "doctor": doctor,
        "metrics": metrics_payload,
        "operational_bounds": bounds,
        "release_artifact": artifacts["archive"],
        "release_artifacts": artifacts,
    }


def status_exit_ok(payload: dict[str, Any]) -> bool:
    artifacts = payload.get("release_artifacts", {})
    artifacts_ok = not artifacts.get("all_present") or artifacts.get("all_valid") is True
    return bool(payload.get("ok") and artifacts_ok)


def _doctor_group_summary(doctor: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    raw_checks = doctor.get("checks")
    checks = raw_checks if isinstance(raw_checks, list) else []
    by_name = {
        str(item.get("name")): item
        for item in checks
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    missing = [name for name in names if name not in by_name]
    failed = [
        {
            "name": name,
            "detail": str(by_name[name].get("detail") or "failed")[:500],
        }
        for name in names
        if name in by_name and by_name[name].get("ok") is not True
    ]
    return {
        "ok": not missing and not failed,
        "required_checks": list(names),
        "missing": missing,
        "failed": failed,
    }


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _transcript_agent_bounds(payload: object) -> dict[str, Any]:
    item = payload if isinstance(payload, dict) else {}
    scan = item.get("scan") if isinstance(item.get("scan"), dict) else {}
    policy = scan.get("policy") if isinstance(scan.get("policy"), dict) else {}
    policy_fields = set(policy)
    positive_policy = all(
        isinstance(policy.get(field), (int, float))
        and not isinstance(policy.get(field), bool)
        and float(policy[field]) > 0
        for field in TRANSCRIPT_POLICY_FIELDS
    )
    bounded = TRANSCRIPT_POLICY_FIELDS.issubset(policy_fields) and positive_policy
    return {
        "ok": item.get("ok") is True and bounded,
        "bounded": bounded,
        "complete": item.get("complete") is True,
        "partial": item.get("partial") is True,
        "sessions_discovered": _nonnegative_int(scan.get("sessions_discovered")),
        "sessions_scanned": _nonnegative_int(
            item.get("sessions_scanned")
            if item.get("sessions_scanned") is not None
            else scan.get("sessions_scanned")
        ),
        "sessions_skipped": _nonnegative_int(scan.get("sessions_skipped")),
        "sessions_partial": _nonnegative_int(scan.get("sessions_partial")),
        "bytes_scanned": _nonnegative_int(scan.get("bytes_scanned")),
        "bytes_skipped": _nonnegative_int(scan.get("bytes_skipped")),
        "skip_counts": scan.get("skip_counts") if isinstance(scan.get("skip_counts"), dict) else {},
        "warning_counts": (
            scan.get("warning_counts") if isinstance(scan.get("warning_counts"), dict) else {}
        ),
        "policy": {field: policy.get(field) for field in sorted(TRANSCRIPT_POLICY_FIELDS)},
    }


def _transcript_bounds(metrics_payload: dict[str, Any]) -> dict[str, Any]:
    usage = metrics_payload.get("usage")
    if isinstance(usage, dict) and usage.get("skipped") is True:
        return {
            "ok": True,
            "skipped": True,
            "reason": str(usage.get("reason") or "usage scan skipped"),
            "agents": {},
        }
    usage_map = usage if isinstance(usage, dict) else {}
    agents = {
        name: _transcript_agent_bounds(usage_map.get(name))
        for name in ("claude", "codex")
    }
    return {
        "ok": all(item["ok"] for item in agents.values()),
        "skipped": False,
        "reason": None,
        "agents": agents,
    }


def operational_bounds_summary(
    root: Path,
    *,
    doctor: dict[str, Any],
    metrics_payload: dict[str, Any],
    sandbox_payload: dict[str, Any] | None = None,
    runner_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doctor_groups = {
        group: _doctor_group_summary(doctor, names)
        for group, names in OPERATIONAL_CHECK_GROUPS.items()
    }
    transcripts = _transcript_bounds(metrics_payload)
    sandbox = sandbox_payload if sandbox_payload is not None else execution_diagnostics(root)
    sandbox_ok = sandbox.get("ok") is True and sandbox.get("bounded") is True
    runner = runner_payload if runner_payload is not None else observation_status(root)
    runner_ok = runner.get("ok") is True and runner.get("bounded") is True
    loss_accounting = loss_accounting_summary(root)
    loss_accounting_ok = (
        loss_accounting.get("ok") is True
        and loss_accounting.get("bounded") is True
    )
    return redact_value(
        {
            "ok": (
                all(group["ok"] for group in doctor_groups.values())
                and transcripts["ok"]
                and sandbox_ok
                and runner_ok
                and loss_accounting_ok
            ),
            "doctor_groups": doctor_groups,
            "transcripts": transcripts,
            "sandbox": sandbox,
            "runner": runner,
            "loss_accounting": loss_accounting,
        }
    )


def release_gate_summary(
    root: Path,
    *,
    git_sha: str | None = None,
    status: dict[str, Any] | None = None,
    include_usage: bool = True,
) -> dict[str, Any]:
    payload = status or status_report(root, include_usage=include_usage)
    sha = git_sha or git_output(root, "rev-parse", "HEAD")
    artifacts = payload.get("release_artifacts", {})
    doctor = payload.get("doctor", {})
    checks = doctor.get("checks", []) if isinstance(doctor, dict) else []
    operational_bounds = payload.get("operational_bounds")
    if not isinstance(operational_bounds, dict):
        metrics_payload = payload.get("metrics")
        operational_bounds = operational_bounds_summary(
            root,
            doctor=doctor if isinstance(doctor, dict) else {},
            metrics_payload=metrics_payload if isinstance(metrics_payload, dict) else {},
        )
    summary = {
        "schema_version": RELEASE_GATE_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_sha": sha,
        "ci": any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI")),
        "release_ready": bool(payload.get("release_ready")),
        "release_artifacts": redact_value(artifacts),
        "dep_advisory": redact_value(dep_advisory_summary(root)),
        "operational_bounds": redact_value(operational_bounds),
        "checks": redact_value(checks),
    }
    assert_release_gate_summary_schema(summary)
    return summary


def dep_advisory_summary(root: Path) -> dict[str, Any]:
    path = root / "dist" / "dep-advisory.json"
    if not path.exists():
        return {"finding_count": None, "mode": None, "generated_at": None, "skipped": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"finding_count": None, "mode": None, "generated_at": None, "skipped": "invalid-json"}
    if not isinstance(payload, dict):
        return {"finding_count": None, "mode": None, "generated_at": None, "skipped": "invalid-schema"}
    finding_count = payload.get("finding_count")
    if not isinstance(finding_count, int):
        finding_count = None
    mode = payload.get("mode")
    generated_at = payload.get("generated_at")
    skipped = payload.get("skipped")
    return {
        "finding_count": finding_count,
        "mode": mode if isinstance(mode, str) else None,
        "generated_at": generated_at if isinstance(generated_at, str) else None,
        "skipped": skipped if skipped is None or isinstance(skipped, str) else "invalid-schema",
    }


def assert_release_gate_summary_schema(payload: dict[str, Any]) -> None:
    fields = set(payload)
    if fields != RELEASE_GATE_SUMMARY_FIELDS:
        missing = sorted(RELEASE_GATE_SUMMARY_FIELDS - fields)
        extra = sorted(fields - RELEASE_GATE_SUMMARY_FIELDS)
        raise ValueError(f"release gate summary schema fields mismatch: missing={missing}, extra={extra}")
    if payload.get("schema_version") != RELEASE_GATE_SUMMARY_SCHEMA_VERSION:
        raise ValueError(
            "release gate summary schema version mismatch: "
            f"expected={RELEASE_GATE_SUMMARY_SCHEMA_VERSION}, actual={payload.get('schema_version')}"
        )
    dep_advisory = payload.get("dep_advisory")
    if not isinstance(dep_advisory, dict):
        raise ValueError("release gate summary dep_advisory must be an object")
    expected_dep_fields = {"finding_count", "mode", "generated_at", "skipped"}
    dep_fields = set(dep_advisory)
    if dep_fields != expected_dep_fields:
        missing = sorted(expected_dep_fields - dep_fields)
        extra = sorted(dep_fields - expected_dep_fields)
        raise ValueError(f"release gate summary dep_advisory fields mismatch: missing={missing}, extra={extra}")
    operational = payload.get("operational_bounds")
    if not isinstance(operational, dict):
        raise ValueError("release gate summary operational_bounds must be an object")
    expected_operational_fields = {
        "ok",
        "doctor_groups",
        "transcripts",
        "sandbox",
        "runner",
        "loss_accounting",
    }
    operational_fields = set(operational)
    if operational_fields != expected_operational_fields:
        missing = sorted(expected_operational_fields - operational_fields)
        extra = sorted(operational_fields - expected_operational_fields)
        raise ValueError(
            f"release gate summary operational_bounds fields mismatch: missing={missing}, extra={extra}"
        )
    if not isinstance(operational.get("ok"), bool):
        raise ValueError("release gate summary operational_bounds.ok must be a boolean")
    doctor_groups = operational.get("doctor_groups")
    if not isinstance(doctor_groups, dict) or set(doctor_groups) != set(OPERATIONAL_CHECK_GROUPS):
        raise ValueError("release gate summary doctor_groups mismatch")
    transcripts = operational.get("transcripts")
    if not isinstance(transcripts, dict) or set(transcripts) != {"ok", "skipped", "reason", "agents"}:
        raise ValueError("release gate summary transcripts fields mismatch")
    sandbox = operational.get("sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("bounded") is not True:
        raise ValueError("release gate summary sandbox diagnostics must be bounded")
    runner = operational.get("runner")
    if not isinstance(runner, dict) or runner.get("bounded") is not True:
        raise ValueError("release gate summary runner diagnostics must be bounded")
    loss_accounting = operational.get("loss_accounting")
    if not isinstance(loss_accounting, dict) or loss_accounting.get("bounded") is not True:
        raise ValueError("release gate summary loss accounting must be bounded")


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
            f"./scripts/verify-artifacts.sh dist/code-brain-{__version__}.tar.gz",
            "./scripts/install-check.sh",
            f"./scripts/artifact-tamper-check.sh dist/code-brain-{__version__}.tar.gz",
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
        return {"valid": False, "git_head_valid": False, "git_status_valid": False}
    text = release_notes.read_text(encoding="utf-8")
    provenance_payload = json_payload(provenance) or {}
    provenance_git = provenance_payload.get("git", {})
    git_head = provenance_git.get("head") if isinstance(provenance_git, dict) else None
    git_status = provenance_git.get("status_short") if isinstance(provenance_git, dict) else None
    required = [
        f"# Code Brain {__version__} Release Notes",
        file_sha256(archive) or "",
        manifest.name,
        sbom.name,
        provenance.name,
        "./scripts/release-gate.sh",
    ]
    git_head_valid = bool(git_head and f"- Git HEAD: `{git_head}`" in text)
    git_status_valid = git_status == "" and "- Git status: `clean`" in text
    return {
        "valid": all(needle and needle in text for needle in required) and git_head_valid and git_status_valid,
        "git_head_valid": git_head_valid,
        "git_status_valid": git_status_valid,
    }
