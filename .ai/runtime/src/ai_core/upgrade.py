from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .memory import append_audit
from .render import build_manifest, render
from .worker.ipc import PROTOCOL_VERSION

DEFAULT_REPO_URL = "https://github.com/ezBuilder/code-brain.git"
DEFAULT_REF = "main"
_TAIL_LIMIT = 4000


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def semver_major(version: str) -> int:
    return int(version.split(".", 1)[0])


def migrate(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    manifest = build_manifest(root)
    current_schema = manifest["schema_version"]
    actions = []
    if current_schema != 1:
        actions.append({"action": "set_schema_version", "from": current_schema, "to": 1})
    if not (root / ".ai" / "generated" / "manifest.json").exists():
        actions.append({"action": "render_manifest"})
    if not dry_run and actions:
        render(root)
        append_audit(root, action="migrate.apply", category="upgrade", payload={"actions": actions})
    return {
        "ok": True,
        "dry_run": dry_run,
        "schema_version": 1,
        "runtime_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "actions": actions,
    }


def upgrade_plan(root: Path, *, target_version: str) -> dict[str, Any]:
    current_major = semver_major(__version__)
    target_major = semver_major(target_version)
    compatible = target_major in {current_major, current_major + 1}
    return {
        "ok": compatible,
        "current_version": __version__,
        "target_version": target_version,
        "compatible": compatible,
        "channel": "local",
        "steps": [
            "run doctor --strict",
            "write cache rollback backup",
            "run migrate",
            "run render",
            "run doctor --strict",
        ],
    }


def upgrade_apply(root: Path, *, target_version: str, dry_run: bool = False) -> dict[str, Any]:
    plan = upgrade_plan(root, target_version=target_version)
    if not plan["compatible"]:
        return {"ok": False, "dry_run": dry_run, "error": "INCOMPATIBLE_MAJOR", "plan": plan}
    backup_path = root / ".ai" / "cache" / "upgrade" / f"rollback-{now_stamp()}.json"
    result = {"ok": True, "dry_run": dry_run, "plan": plan, "backup_path": backup_path.relative_to(root).as_posix()}
    if dry_run:
        return result
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(
        json.dumps(
            {
                "runtime_version": __version__,
                "target_version": target_version,
                "manifest": build_manifest(root),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    migrate(root)
    render(root)
    append_audit(root, action="upgrade.apply", category="upgrade", payload={"target_version": target_version, "backup_path": result["backup_path"]})
    return result


def _tail(text: str, *, limit: int = _TAIL_LIMIT) -> str:
    return text[-limit:] if len(text) > limit else text


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _command_result(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "command": proc.args,
        "returncode": proc.returncode,
        "stdout": _tail(proc.stdout),
        "stderr": _tail(proc.stderr),
    }


def _resolve_source_ref(checkout: Path, ref: str) -> dict[str, Any]:
    if ref in {"", "HEAD"}:
        return {"ok": True, "checked_out": "HEAD", "fetch": None, "checkout": None}
    fetch = _run(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", ref])
    checkout_ref = "FETCH_HEAD" if fetch.returncode == 0 else ref
    co = _run(["git", "-C", str(checkout), "checkout", "--detach", checkout_ref])
    if co.returncode != 0:
        return {"ok": False, "fetch": _command_result(fetch), "checkout": _command_result(co)}
    return {"ok": True, "checked_out": ref, "fetch": _command_result(fetch), "checkout": _command_result(co)}


def _install_command(checkout: Path, root: Path) -> list[str]:
    if os.name == "nt":
        ps1 = checkout / "scripts" / "install-into.ps1"
        shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "upgrade", str(root)]
    return ["bash", str(checkout / "scripts" / "install-into.sh"), "upgrade", str(root)]


def _bootstrap_command(root: Path) -> list[str] | None:
    bootstrap = root / "bootstrap-code-brain.sh"
    if not bootstrap.exists():
        return None
    if os.name == "nt":
        shell = shutil.which("bash")
        return [shell, str(bootstrap)] if shell else None
    return ["bash", str(bootstrap)]


def _installed_source(root: Path) -> dict[str, Any]:
    manifest = root / ".ai" / "generated" / "install-manifest.json"
    if not manifest.exists():
        return {}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def upgrade_latest(
    root: Path,
    *,
    repo_url: str | None = None,
    ref: str | None = None,
    dry_run: bool = False,
    keep_clone: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    installed_source = _installed_source(root)
    repo_url = repo_url or os.environ.get("CODE_BRAIN_REPO_URL") or installed_source.get("source_repo_url") or DEFAULT_REPO_URL
    ref = ref or os.environ.get("CODE_BRAIN_REF") or installed_source.get("source_ref") or DEFAULT_REF
    planned = {
        "clone": ["git", "clone", "--depth", "1", repo_url, "<tmp>/code-brain"],
        "checkout": ref,
        "install": "<tmp>/code-brain/scripts/install-into.sh upgrade " + str(root),
        "bootstrap": "bash ./bootstrap-code-brain.sh",
        "doctor": ".ai/bin/ai doctor --strict --json",
    }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "channel": "github",
            "repo_url": repo_url,
            "ref": ref,
            "target": str(root),
            "planned": planned,
        }
    if shutil.which("git") is None:
        return {"ok": False, "dry_run": False, "error": "GIT_NOT_FOUND", "repo_url": repo_url, "ref": ref, "target": str(root)}
    temp_root = Path(tempfile.mkdtemp(prefix="code-brain-upgrade-"))
    checkout = temp_root / "code-brain"
    try:
        clone = _run(["git", "clone", "--depth", "1", repo_url, str(checkout)])
        if clone.returncode != 0:
            return {"ok": False, "dry_run": False, "error": "CLONE_FAILED", "repo_url": repo_url, "ref": ref, "target": str(root), "clone_path": str(temp_root) if keep_clone else None, "clone": _command_result(clone)}
        ref_result = _resolve_source_ref(checkout, ref)
        if not ref_result["ok"]:
            return {"ok": False, "dry_run": False, "error": "CHECKOUT_FAILED", "repo_url": repo_url, "ref": ref, "target": str(root), "clone_path": str(temp_root) if keep_clone else None, **ref_result}
        sha = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
        source_git_sha = sha.stdout.strip() if sha.returncode == 0 else None
        env = os.environ.copy()
        env["CODE_BRAIN_REPO_URL"] = repo_url
        env["CODE_BRAIN_REF"] = ref
        install = _run(_install_command(checkout, root), env=env)
        if install.returncode != 0:
            return {
                "ok": False,
                "dry_run": False,
                "error": "INSTALL_FAILED",
                "repo_url": repo_url,
                "ref": ref,
                "source_git_sha": source_git_sha,
                "target": str(root),
                "clone_path": str(temp_root) if keep_clone else None,
                "install": _command_result(install),
            }
        bootstrap_payload: dict[str, Any] | None = None
        boot_cmd = _bootstrap_command(root)
        if boot_cmd:
            boot = _run(boot_cmd, cwd=root)
            bootstrap_payload = _command_result(boot)
            if boot.returncode != 0:
                return {
                    "ok": False,
                    "dry_run": False,
                    "error": "BOOTSTRAP_FAILED",
                    "repo_url": repo_url,
                    "ref": ref,
                    "source_git_sha": source_git_sha,
                    "target": str(root),
                    "clone_path": str(temp_root) if keep_clone else None,
                    "install": _command_result(install),
                    "bootstrap": bootstrap_payload,
                }
        doctor = _run([str(root / ".ai" / "bin" / "ai"), "doctor", "--strict", "--json"], cwd=root)
        payload = {
            "ok": doctor.returncode == 0,
            "dry_run": False,
            "channel": "github",
            "repo_url": repo_url,
            "ref": ref,
            "source_git_sha": source_git_sha,
            "target": str(root),
            "clone_path": str(temp_root) if keep_clone else None,
            "install": _command_result(install),
            "bootstrap": bootstrap_payload,
            "doctor": _command_result(doctor),
        }
        if payload["ok"]:
            append_audit(root, action="upgrade.latest", category="upgrade", payload={"repo_url": repo_url, "ref": ref, "source_git_sha": source_git_sha})
        else:
            payload["error"] = "DOCTOR_FAILED"
        return payload
    finally:
        if not keep_clone:
            shutil.rmtree(temp_root, ignore_errors=True)


def rollback(root: Path, *, backup_path: str) -> dict[str, Any]:
    source = root / backup_path
    if not source.exists():
        raise FileNotFoundError(f"rollback backup not found: {backup_path}")
    data = json.loads(source.read_text(encoding="utf-8"))
    manifest_path = root / ".ai" / "generated" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(data["manifest"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_audit(root, action="upgrade.rollback", category="upgrade", payload={"backup_path": backup_path})
    return {"ok": True, "restored": "manifest", "backup_path": backup_path}


def clean_upgrade_cache(root: Path) -> dict[str, Any]:
    path = root / ".ai" / "cache" / "upgrade"
    if path.exists():
        shutil.rmtree(path)
        return {"ok": True, "removed": True}
    return {"ok": True, "removed": False}
