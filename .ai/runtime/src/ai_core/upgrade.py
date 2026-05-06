from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .memory import append_audit
from .render import build_manifest, render
from .worker.ipc import PROTOCOL_VERSION


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

