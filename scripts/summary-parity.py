#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.report import assert_release_gate_summary_schema  # noqa: E402


def load_summary(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: summary is not a JSON object")
    try:
        assert_release_gate_summary_schema(payload)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return payload


def canonical(payload: dict[str, Any]) -> dict[str, Any]:
    artifacts = payload.get("release_artifacts", {})
    dep_advisory = payload.get("dep_advisory", {})
    operational = payload.get("operational_bounds", {})
    checks = payload.get("checks", [])
    check_map = {}
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict) and isinstance(check.get("name"), str):
                check_map[check["name"]] = bool(check.get("ok"))
    artifact_subset = {}
    if isinstance(artifacts, dict):
        for key in ("all_present", "all_valid", "all_current"):
            artifact_subset[key] = artifacts.get(key)
        for key in ("archive", "manifest", "sbom", "provenance", "release_notes"):
            entry = artifacts.get(key)
            if isinstance(entry, dict):
                artifact_subset[key] = {
                    subkey: entry.get(subkey)
                    for subkey in (
                        "exists",
                        "valid",
                        "current",
                        "checksum_valid",
                        "git_head_matches",
                        "git_status_valid",
                        "git_head_valid",
                    )
                    if subkey in entry
                }
    operational_subset: dict[str, Any] = {
        "ok": None,
        "doctor_groups": {},
        "transcripts": {},
        "sandbox": {},
        "runner": {},
        "loss_accounting": {},
    }
    if isinstance(operational, dict):
        operational_subset["ok"] = operational.get("ok")
        doctor_groups = operational.get("doctor_groups")
        if isinstance(doctor_groups, dict):
            operational_subset["doctor_groups"] = {
                name: {
                    "ok": item.get("ok"),
                    "missing": sorted(str(value) for value in item.get("missing", []) if isinstance(value, str)),
                    "failed": sorted(
                        str(value.get("name"))
                        for value in item.get("failed", [])
                        if isinstance(value, dict) and isinstance(value.get("name"), str)
                    ),
                }
                for name, item in sorted(doctor_groups.items())
                if isinstance(name, str) and isinstance(item, dict)
            }
        transcripts = operational.get("transcripts")
        if isinstance(transcripts, dict):
            agents = transcripts.get("agents")
            operational_subset["transcripts"] = {
                "ok": transcripts.get("ok"),
                "skipped": transcripts.get("skipped"),
                "agents": {
                    name: {
                        "ok": item.get("ok"),
                        "bounded": item.get("bounded"),
                        "policy": item.get("policy") if isinstance(item.get("policy"), dict) else {},
                    }
                    for name, item in sorted(agents.items())
                    if isinstance(name, str) and isinstance(item, dict)
                }
                if isinstance(agents, dict)
                else {},
            }
        sandbox = operational.get("sandbox")
        if isinstance(sandbox, dict):
            operational_subset["sandbox"] = {
                "ok": sandbox.get("ok"),
                "bounded": sandbox.get("bounded"),
            }
        runner = operational.get("runner")
        if isinstance(runner, dict):
            operational_subset["runner"] = {
                "ok": runner.get("ok"),
                "bounded": runner.get("bounded"),
                "observed": runner.get("observed"),
                "killed_9": runner.get("killed_9"),
                "transport_restart": runner.get("transport_restart"),
            }
        loss_accounting = operational.get("loss_accounting")
        if isinstance(loss_accounting, dict):
            policy = loss_accounting.get("policy")
            operational_subset["loss_accounting"] = {
                "ok": loss_accounting.get("ok"),
                "bounded": loss_accounting.get("bounded"),
                "policy": policy if isinstance(policy, dict) else {},
            }
    return {
        "schema_version": payload.get("schema_version"),
        "git_sha": payload.get("git_sha"),
        "release_ready": payload.get("release_ready"),
        "release_artifacts": artifact_subset,
        "dep_advisory": {
            key: dep_advisory.get(key) if isinstance(dep_advisory, dict) else None
            for key in ("finding_count", "mode", "skipped")
        },
        "operational_bounds": operational_subset,
        "checks": dict(sorted(check_map.items())),
    }


def compare(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    left_canonical = canonical(left)
    right_canonical = canonical(right)
    mismatches = []
    for field in (
        "schema_version",
        "git_sha",
        "release_ready",
        "release_artifacts",
        "dep_advisory",
        "operational_bounds",
        "checks",
    ):
        if left_canonical.get(field) != right_canonical.get(field):
            mismatches.append({"field": field, "left": left_canonical.get(field), "right": right_canonical.get(field)})
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare release-gate summary canonical fields.")
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        left = load_summary(Path(args.left))
        right = load_summary(Path(args.right))
    except ValueError as exc:
        payload = {"ok": False, "error": str(exc), "mismatches": []}
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 2

    mismatches = compare(left, right)
    payload = {"ok": not mismatches, "mismatches": mismatches}
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif mismatches:
        for mismatch in mismatches:
            print(
                f"{mismatch['field']}: left={json.dumps(mismatch['left'], sort_keys=True)} "
                f"right={json.dumps(mismatch['right'], sort_keys=True)}",
                file=sys.stderr,
            )
    else:
        print("ok")
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
