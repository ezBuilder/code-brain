#!/usr/bin/env python3
"""Cross-project AI concept to Unity asset orchestration.

The command intentionally keeps provider generation outside the trusted local
post-processing lane. An authorized provider produces a mesh; this tool owns
normalization, LODs, Unity publication, and deterministic fleet generation.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from asset_catalog import (
    TRANSPORT_DOMAINS,
    CatalogError,
    build_gameplay_catalog,
    build_plan,
    load_catalog,
    load_gameplay_profiles,
)


SKILL_DIR = Path(__file__).resolve().parents[1]
BLENDER_FLEET = SKILL_DIR / "scripts" / "blender_vehicle_fleet.py"
BLENDER_OPTIMIZE = SKILL_DIR / "scripts" / "blender_optimize.py"
IMPORTER_TEMPLATE = SKILL_DIR / "templates" / "AIAssetImportPostprocessor.cs"
IMAGEGEN_PROMPT_TEMPLATE = SKILL_DIR / "templates" / "imagegen-multiview-prompt.txt"
DEFAULT_CATALOG = SKILL_DIR / "catalogs" / "transport_tycoon_1950_2999.json"
DEFAULT_GAMEPLAY_PROFILES = SKILL_DIR / "catalogs" / "transport_gameplay_profiles.json"
PIPELINE_VERSION = "1.7.0"
PROMPT_VERSION = "RF-IMAGEGEN-MULTIVIEW-1.2"
GPT_IMAGE_MODEL = "gpt-image-2"
GPT_IMAGE_SIZE = "2048x2048"
GPT_IMAGE_QUALITY = "high"
DEFAULT_DESTINATION = "Assets/AIAssetPipeline/Generated"

ASSET_STATE_INSTRUCTIONS = {
    "base": (
        "Clean, intact, factory-operational geometry. Separate lamps, windows, displays, "
        "cabin lighting, and engine glow into emissive-ready material regions. No dirt, damage, "
        "failure, smoke, flame, leak, sparks, explosion, or dramatic night lighting."
    ),
    "damaged": (
        "The exact same design in a repairable collision-damaged state. Add stable localized "
        "deformation, cracked glazing, bent panels, and one visibly disabled running component. "
        "No active smoke, fire, sparks, leak, explosion, debris cloud, or emergency responders."
    ),
    "wrecked": (
        "The exact same design in an extinguished terminal wreck state. Add charred materials, "
        "broken glazing, warped panels, and disabled running gear while preserving a readable "
        "silhouette. No active flame, smoke, sparks, explosion, glowing embers, or debris cloud."
    ),
}


class PipelineError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def executable_path(name: str, candidates: Iterable[Path] = ()) -> Path | None:
    configured = os.environ.get(name)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    command = shutil.which(name.lower())
    return Path(command).resolve() if command else None


def find_blender() -> Path | None:
    return executable_path(
        "BLENDER_BIN",
        (
            Path("/Applications/Blender.app/Contents/MacOS/Blender"),
            Path.home() / "Applications/Blender.app/Contents/MacOS/Blender",
        ),
    ) or (Path(shutil.which("blender")).resolve() if shutil.which("blender") else None)


def unity_version_for_project(project: Path) -> str | None:
    version_file = project / "ProjectSettings" / "ProjectVersion.txt"
    if not version_file.is_file():
        return None
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("m_EditorVersion:"):
            return line.split(":", 1)[1].strip()
    return None


def find_unity(project: Path | None = None) -> Path | None:
    configured = os.environ.get("UNITY_EDITOR")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    version = unity_version_for_project(project) if project else None
    if version:
        candidate = Path(f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity")
        if candidate.is_file():
            return candidate.resolve()
    hub = Path("/Applications/Unity/Hub/Editor")
    if hub.is_dir():
        candidates = sorted(hub.glob("*/Unity.app/Contents/MacOS/Unity"), reverse=True)
        if candidates:
            return candidates[0].resolve()
    return None


def run_checked(command: list[str], *, cwd: Path | None = None) -> None:
    display = " ".join(command)
    print(f"run: {display}")
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise PipelineError(f"command failed ({completed.returncode}): {display}")


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PipelineError(f"{label} not found: {resolved}")
    return resolved


def require_unity_project(path: Path) -> Path:
    project = path.expanduser().resolve()
    if not (project / "Assets").is_dir() or not (project / "ProjectSettings").is_dir():
        raise PipelineError(f"Unity project not found: {project}")
    return project


def safe_unity_destination(project: Path, destination: str) -> Path:
    relative = Path(destination)
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "Assets" or ".." in relative.parts:
        raise PipelineError("Unity destination must be a relative Assets/... path")
    resolved = (project / relative).resolve()
    if project not in resolved.parents:
        raise PipelineError("Unity destination escapes the project")
    return resolved


def copy_file(source: Path, destination: Path, *, force: bool) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if source.is_file() and sha256_file(source) == sha256_file(destination):
            return "unchanged"
        if not force:
            raise PipelineError(f"refusing to overwrite without --force: {destination}")
    shutil.copy2(source, destination)
    return "copied"


def command_doctor(args: argparse.Namespace) -> None:
    project = require_unity_project(Path(args.project)) if args.project else None
    blender = find_blender()
    gltf_transform = shutil.which("gltf-transform")
    blender_mcp = shutil.which("blender-mcp-secure")
    blender_mcp_addon = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Blender"
        / "5.2"
        / "scripts"
        / "addons"
        / "blender_mcp_secure"
        / "__init__.py"
    )
    unity = find_unity(project)
    payload = {
        "ok": blender is not None and gltf_transform is not None and (project is None or unity is not None),
        "pipelineVersion": PIPELINE_VERSION,
        "skillDir": str(SKILL_DIR),
        "tools": {
            "blender": str(blender) if blender else None,
            "blenderMcpCli": blender_mcp,
            "blenderMcpAddon": str(blender_mcp_addon) if blender_mcp_addon.is_file() else None,
            "gltfTransform": gltf_transform,
            "unity": str(unity) if unity else None,
            "unityVersion": unity_version_for_project(project) if project else None,
            "python": sys.executable,
        },
        "project": str(project) if project else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise PipelineError("doctor failed: install Blender, glTF Transform, and the project's Unity editor")


def command_init(args: argparse.Namespace) -> None:
    project = require_unity_project(Path(args.project))
    importer = project / "Assets" / "AIAssetPipeline" / "Editor" / IMPORTER_TEMPLATE.name
    state_dir = project / ".ai-asset-pipeline"
    state_dir.mkdir(parents=True, exist_ok=True)
    status = copy_file(IMPORTER_TEMPLATE, importer, force=args.force)
    config = state_dir / "pipeline.json"
    payload = {
        "schemaVersion": 1,
        "pipelineVersion": PIPELINE_VERSION,
        "defaultDestination": DEFAULT_DESTINATION,
        "triangleBudgets": {"lod0": 36000, "lod1": 9000, "lod2": 1800},
        "units": "meters",
        "pivot": "bottom-center",
        "defaultCatalog": "transport_tycoon_1950_2999",
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if config.exists() and config.read_text(encoding="utf-8") != encoded and not args.force:
        raise PipelineError(f"refusing to overwrite without --force: {config}")
    config.write_text(encoded, encoding="utf-8")
    print(json.dumps({"ok": True, "project": str(project), "importer": status, "config": str(config)}, indent=2))


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "default"


def render_prompt_record(values: dict[str, str], *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    template = require_file(IMAGEGEN_PROMPT_TEMPLATE, "ImageGen prompt template").read_text(
        encoding="utf-8"
    )
    if any(not value for value in values.values()):
        raise PipelineError("prompt fields must not be empty")
    year = int(values["year"])
    if year < 1950 or year > 2999:
        raise PipelineError("prompt year must stay within 1950..2999")
    prompt = template.format(**values).rstrip() + "\n"
    fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    record = {
        "schemaVersion": 1,
        "pipelineVersion": PIPELINE_VERSION,
        "promptVersion": PROMPT_VERSION,
        "assetId": values["asset_id"],
        "fields": values,
        "promptSha256": fingerprint,
        "prompt": prompt,
        "imageOutput": {
            "model": GPT_IMAGE_MODEL,
            "size": GPT_IMAGE_SIZE,
            "quality": GPT_IMAGE_QUALITY,
            "format": "png",
            "background": "opaque",
        },
    }
    if metadata:
        record["metadata"] = metadata
    return record


def write_prompt_record(record: dict[str, Any], output: Path, *, force: bool) -> None:
    sidecar = Path(str(output) + ".json")
    encoded_prompt = str(record["prompt"])
    encoded_sidecar = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    for destination, encoded in ((output, encoded_prompt), (sidecar, encoded_sidecar)):
        if destination.exists() and destination.read_text(encoding="utf-8") != encoded and not force:
            raise PipelineError(f"refusing to overwrite without --force: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")


def command_prompt(args: argparse.Namespace) -> None:
    asset_state = args.asset_state.strip()
    if asset_state not in ASSET_STATE_INSTRUCTIONS:
        raise PipelineError(f"unknown asset state: {asset_state}")
    values = {
        "asset_id": args.asset_id.strip(),
        "subject": args.subject.strip(),
        "year": str(args.year),
        "region": args.region.strip(),
        "technology": args.technology.strip(),
        "livery": args.livery.strip(),
        "details": args.details.strip(),
        "asset_state": asset_state,
        "state_instructions": ASSET_STATE_INSTRUCTIONS[asset_state],
    }
    record = render_prompt_record(values)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        write_prompt_record(record, output, force=args.force)
        record["output"] = str(output)
        record["sidecar"] = str(Path(str(output) + ".json"))
    if args.json or args.output:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(record["prompt"], end="")


def axis_combinations(axes: dict[str, list[str]]) -> list[dict[str, str]]:
    if not axes:
        return [{}]
    names = sorted(axes)
    return [
        dict(zip(names, values))
        for values in itertools.product(*(axes[name] for name in names))
    ]


def _write_text_artifact(path: Path, encoded: str, *, force: bool) -> None:
    if path.exists() and path.read_text(encoding="utf-8") != encoded and not force:
        raise PipelineError(f"refusing to overwrite without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")


def _selected_asset_states(args: argparse.Namespace) -> list[str]:
    requested = list(args.state or [])
    if args.include_disaster_states:
        requested.extend(("base", "damaged", "wrecked"))
    if not requested:
        requested.append("base")
    states = list(dict.fromkeys(requested))
    unknown = [state for state in states if state not in ASSET_STATE_INSTRUCTIONS]
    if unknown:
        raise PipelineError(f"unknown asset states: {unknown}")
    return states


def generate_prompt_batch(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_prompts < 0:
        raise PipelineError("max-prompts must be zero or greater")
    catalog_path = Path(args.catalog) if args.catalog else DEFAULT_CATALOG
    gameplay_path = Path(args.gameplay_profiles) if args.gameplay_profiles else DEFAULT_GAMEPLAY_PROFILES
    try:
        payload = load_catalog(catalog_path)
        gameplay_profiles = load_gameplay_profiles(gameplay_path, payload)
        if args.region not in payload["regionalProfiles"]:
            raise CatalogError(f"unknown regional profile: {args.region}")
        selected_domains = set(args.domain) if args.domain else None
        plan = build_plan(payload, args.start_year, args.end_year, selected_domains)
        gameplay = build_gameplay_catalog(
            payload,
            gameplay_profiles,
            args.start_year,
            args.end_year,
            selected_domains or set(TRANSPORT_DOMAINS),
        )
    except CatalogError as exc:
        raise PipelineError(str(exc)) from exc

    gameplay_index = {
        (model["eraId"], model["archetypeId"]): model for model in gameplay["models"]
    }
    states = _selected_asset_states(args)
    selected = set(args.archetype or [])
    output_dir = Path(args.output_dir).expanduser().resolve()
    jobs: list[dict[str, Any]] = []
    for batch in plan["batches"]:
        for item in batch["items"]:
            if item["domain"] not in TRANSPORT_DOMAINS:
                continue
            if selected and item["assetId"] not in selected:
                continue
            gameplay_model = gameplay_index[(batch["eraId"], item["assetId"])]
            for axes in axis_combinations(item["meshAxes"]):
                axis_label = ", ".join(f"{key}: {value}" for key, value in axes.items()) or "standard configuration"
                axis_suffix = "-".join(slug(value) for value in axes.values())
                for asset_state in states:
                    if args.max_prompts and len(jobs) >= args.max_prompts:
                        break
                    design_year = gameplay_model["designYear"]
                    asset_id = "-".join(
                        value for value in (
                            item["domain"], item["assetId"], str(design_year),
                            slug(args.region), axis_suffix, asset_state,
                        ) if value
                    )
                    values = {
                        "asset_id": asset_id,
                        "subject": f"{item['assetId'].replace('-', ' ')}; {axis_label}",
                        "year": str(design_year),
                        "region": f"{args.region}; {batch['eraId']} era design language",
                        "technology": "; ".join(
                            [item["recipe"], *batch["styleTags"], axis_label, gameplay_model["energyCarrier"]]
                        ),
                        "livery": f"unbranded {args.region} production livery; neutral PBR material separation; emissive-ready lamps and windows",
                        "details": (
                            f"physically plausible {item['domain']} construction; reusable {item['recipe']} topology; "
                            f"maximum speed {gameplay_model['maximumSpeedKph']} km/h; capacity "
                            f"{gameplay_model['capacity']} {gameplay_model['capacityUnit']}; infrastructure "
                            f"{gameplay_model['infrastructureClass']}; hazard class {gameplay_model['hazardClass']}"
                        ),
                        "asset_state": asset_state,
                        "state_instructions": ASSET_STATE_INSTRUCTIONS[asset_state],
                    }
                    metadata = {
                        "catalogId": plan["catalogId"],
                        "eraId": batch["eraId"],
                        "eraYears": batch["years"],
                        "archetype": item["assetId"],
                        "domain": item["domain"],
                        "recipe": item["recipe"],
                        "assetState": asset_state,
                        "referenceKind": "mesh-reconstruction",
                        "meshAxes": axes,
                        "materialAxes": item["materialAxes"],
                        "gameplayModel": gameplay_model,
                        "effectPolicy": gameplay_profiles["assetStatePolicy"],
                        "addressableLabel": item["addressableLabel"],
                    }
                    record = render_prompt_record(values, metadata=metadata)
                    prompt_file = output_dir / batch["eraId"] / item["assetId"] / f"{asset_id}.txt"
                    write_prompt_record(record, prompt_file, force=args.force)
                    destination = f".chatgpt2codex/images/transport/{batch['eraId']}/{item['assetId']}/{asset_id}.png"
                    jobs.append(
                        {
                            "assetId": asset_id,
                            "assetState": asset_state,
                            "status": "prompt-ready",
                            "promptFile": str(prompt_file),
                            "promptSidecar": str(Path(str(prompt_file) + ".json")),
                            "promptSha256": record["promptSha256"],
                            "chatgpt2codexImage": {
                                "tool": "chatgpt_image_generate",
                                "requestedMode": "very-high",
                                "destination": destination,
                                "idempotencyKey": f"rf-{record['promptSha256'][:32]}",
                                "expectedOutput": {
                                    "model": GPT_IMAGE_MODEL,
                                    "width": 2048,
                                    "height": 2048,
                                    "quality": GPT_IMAGE_QUALITY,
                                    "format": "png",
                                    "background": "opaque",
                                },
                                "rejectUnless": [
                                    "validated-image-bytes",
                                    "mime-image-png",
                                    "exact-2048x2048",
                                    "sha256-recorded",
                                    "single-asset-single-state",
                                    "front-left-rear-top-consistent",
                                ],
                            },
                            "assetBuildAgent": {
                                "requestedModel": "gpt-5.6-sol",
                                "reasoningEffort": "xhigh",
                                "workflow": "validated image -> approved mesh provider or reusable Blender recipe -> LOD/PBR/pivot -> publish -> Unity multiview and runtime proof",
                            },
                            "metadata": metadata,
                        }
                    )
                if args.max_prompts and len(jobs) >= args.max_prompts:
                    break
            if args.max_prompts and len(jobs) >= args.max_prompts:
                break
        if args.max_prompts and len(jobs) >= args.max_prompts:
            break
    if not jobs:
        raise PipelineError("no prompt jobs matched the requested catalog filters")

    used_failure_ids = sorted(
        {job["metadata"]["gameplayModel"]["failureProfileId"] for job in jobs}
    )
    vfx_manifest = {
        "schemaVersion": 1,
        "pipelineVersion": PIPELINE_VERSION,
        "policy": gameplay_profiles["assetStatePolicy"],
        "sharedFailureProfiles": {
            failure_id: gameplay_profiles["failureProfiles"][failure_id]
            for failure_id in used_failure_ids
        },
        "implementation": {
            "dayNight": "shared PBR materials plus emissive masks and runtime lights",
            "breakdownFireExplosion": "shared Unity VFX Graph or Particle System packs attached to semantic sockets",
            "damage": "base/damaged/wrecked mesh states plus reusable decals; never one mesh per VFX combination",
            "requiredSockets": ["fx-engine", "fx-power", "fx-cargo", "fx-running-gear", "fx-exhaust", "fx-center-of-mass"],
        },
    }
    build_jobs = [
        {
            "assetId": job["assetId"],
            "assetState": job["assetState"],
            "status": "awaiting-image",
            "sourceImage": job["chatgpt2codexImage"]["destination"],
            "promptSha256": job["promptSha256"],
            "requestedModel": "gpt-5.6-sol",
            "reasoningEffort": "xhigh",
            "recipe": job["metadata"]["recipe"],
            "gameplayModelId": job["metadata"]["gameplayModel"]["modelId"],
            "addressableLabel": job["metadata"]["addressableLabel"],
            "requiredProof": ["front", "side", "rear", "top", "three-quarter", "lod", "pivot", "scale", "runtime-vfx-sockets"],
        }
        for job in jobs
    ]
    manifest = {
        "schemaVersion": 2,
        "pipelineVersion": PIPELINE_VERSION,
        "promptVersion": PROMPT_VERSION,
        "catalog": str(catalog_path.expanduser().resolve()),
        "gameplayProfiles": str(gameplay_path.expanduser().resolve()),
        "requestedYears": plan["requestedYears"],
        "region": args.region,
        "assetStates": states,
        "jobCount": len(jobs),
        "chatgpt2codex": {
            "leasePreset": "full-write",
            "imageTool": "chatgpt_image_generate",
            "statusTool": "chatgpt_image_status",
            "requestedImageMode": "very-high",
            "requestedBuildModel": "gpt-5.6-sol",
            "requestedReasoningEffort": "xhigh",
            "note": "The connected ChatGPT/IDE controls model availability. Queue creation never authorizes provider spend.",
        },
        "jobs": jobs,
    }
    artifacts = {
        "manifest": output_dir / "asset-image-batch.json",
        "imageQueue": output_dir / "chatgpt2codex-image-queue.jsonl",
        "buildQueue": output_dir / "chatgpt2codex-build-queue.jsonl",
        "vfxManifest": output_dir / "transport-vfx-pack.json",
    }
    _write_text_artifact(artifacts["manifest"], json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", force=args.force)
    _write_text_artifact(artifacts["imageQueue"], "".join(json.dumps(job, ensure_ascii=False) + "\n" for job in jobs), force=args.force)
    _write_text_artifact(artifacts["buildQueue"], "".join(json.dumps(job, ensure_ascii=False) + "\n" for job in build_jobs), force=args.force)
    _write_text_artifact(artifacts["vfxManifest"], json.dumps(vfx_manifest, ensure_ascii=False, indent=2) + "\n", force=args.force)
    return {
        "ok": True,
        "jobCount": len(jobs),
        **{key: str(path) for key, path in artifacts.items()},
    }


def command_prompt_batch(args: argparse.Namespace) -> None:
    print(json.dumps(generate_prompt_batch(args), ensure_ascii=False, indent=2))


def blender_command(script: Path, arguments: list[str]) -> list[str]:
    blender = find_blender()
    if blender is None:
        raise PipelineError("Blender executable not found")
    require_file(script, "Blender pipeline script")
    return [str(blender), "--background", "--factory-startup", "--python", str(script), "--", *arguments]


def publish_outputs(
    source_dir: Path,
    project: Path,
    destination: str,
    *,
    force: bool,
    reference: Path | None,
) -> dict[str, Any]:
    output = source_dir.expanduser().resolve()
    if not output.is_dir():
        raise PipelineError(f"generated output not found: {output}")
    target = safe_unity_destination(project, destination)
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    allowed = {".fbx", ".glb", ".gltf", ".bin", ".png", ".jpg", ".jpeg", ".json"}
    for source in sorted(output.iterdir()):
        if source.is_file() and source.suffix.lower() in allowed:
            copy_file(source, target / source.name, force=force)
            copied.append(source.name)
    if reference is not None:
        reference_target = target / "References" / reference.name
        copy_file(reference, reference_target, force=force)
        copied.append(str(Path("References") / reference.name))
    if not copied:
        raise PipelineError(f"no publishable output found in {output}")
    return {"target": str(target), "files": copied}


def command_fleet(args: argparse.Namespace) -> None:
    reference = require_file(Path(args.reference), "reference image")
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_checked(
        blender_command(
            BLENDER_FLEET,
            [
                "--output-dir",
                str(output_dir),
                "--reference",
                str(reference),
                "--lod0",
                str(args.lod0),
                "--lod1",
                str(args.lod1),
                "--lod2",
                str(args.lod2),
            ],
        )
    )
    manifest = output_dir / "asset-manifest.json"
    validate_manifest(manifest)
    publication = None
    if args.unity_project:
        project = require_unity_project(Path(args.unity_project))
        command_init(argparse.Namespace(project=str(project), force=args.force))
        publication = publish_outputs(
            output_dir,
            project,
            args.unity_destination,
            force=args.force,
            reference=reference,
        )
    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(manifest),
                "referenceSha256": sha256_file(reference),
                "publication": publication,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_optimize(args: argparse.Namespace) -> None:
    source = require_file(Path(args.input), "input mesh")
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_checked(
        blender_command(
            BLENDER_OPTIMIZE,
            [
                "--input",
                str(source),
                "--output-dir",
                str(output_dir),
                "--lod0",
                str(args.lod0),
                "--lod1",
                str(args.lod1),
                "--lod2",
                str(args.lod2),
                "--target-size",
                str(args.target_size),
            ],
        )
    )
    manifest = output_dir / "asset-manifest.json"
    validate_manifest(manifest)
    print(json.dumps({"ok": True, "manifest": str(manifest)}, indent=2))


def command_publish(args: argparse.Namespace) -> None:
    project = require_unity_project(Path(args.project))
    command_init(argparse.Namespace(project=str(project), force=args.force))
    reference = require_file(Path(args.reference), "reference image") if args.reference else None
    result = publish_outputs(
        Path(args.source_dir), project, args.destination, force=args.force, reference=reference
    )
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))


def validate_manifest(path: Path) -> dict[str, Any]:
    manifest = require_file(path, "asset manifest")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid asset manifest: {exc}") from exc
    assets = payload.get("assets")
    budgets = payload.get("triangleBudgets")
    if not isinstance(assets, list) or len(assets) == 0 or not isinstance(budgets, dict):
        raise PipelineError("asset manifest has no assets or triangle budgets")
    gltf_transform = shutil.which("gltf-transform")
    for asset in assets:
        if not isinstance(asset, dict) or not asset.get("id"):
            raise PipelineError("asset manifest contains an invalid asset")
        lods = asset.get("lods")
        if not isinstance(lods, list) or len(lods) != 3:
            raise PipelineError(f"{asset.get('id')} must contain exactly three LODs")
        for lod in lods:
            level = str(lod.get("level"))
            triangles = lod.get("triangles")
            budget = budgets.get(level)
            if not isinstance(triangles, int) or not isinstance(budget, int) or triangles > budget:
                raise PipelineError(
                    f"triangle budget failed: {asset.get('id')} {level}={triangles}, budget={budget}"
                )
            for key in ("fbx", "glb"):
                relative = lod.get(key)
                artifact = manifest.parent / relative if isinstance(relative, str) else None
                if artifact is None or not artifact.is_file():
                    raise PipelineError(f"missing {key}: {asset.get('id')} {level}")
                if key == "glb" and gltf_transform is not None:
                    inspected = subprocess.run(
                        [gltf_transform, "inspect", str(artifact)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    if inspected.returncode != 0:
                        raise PipelineError(
                            f"invalid GLB: {asset.get('id')} {level}: {inspected.stderr.strip()}"
                        )
    return payload


def command_validate(args: argparse.Namespace) -> None:
    payload = validate_manifest(Path(args.manifest))
    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(Path(args.manifest).expanduser().resolve()),
                "assetCount": len(payload["assets"]),
                "triangleBudgets": payload["triangleBudgets"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_catalog(args: argparse.Namespace) -> None:
    catalog_path = Path(args.catalog) if args.catalog else DEFAULT_CATALOG
    try:
        payload = load_catalog(catalog_path)
        plan = build_plan(
            payload,
            args.start_year,
            args.end_year,
            set(args.domain) if args.domain else None,
        )
    except CatalogError as exc:
        raise PipelineError(str(exc)) from exc
    if args.output_plan:
        destination = Path(args.output_plan).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    result = {
        "ok": True,
        "catalog": str(catalog_path.expanduser().resolve()),
        "requestedYears": plan["requestedYears"],
        "strategy": plan["strategy"],
        "summary": plan["summary"],
        "plan": str(Path(args.output_plan).expanduser().resolve()) if args.output_plan else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def generate_gameplay_catalog(args: argparse.Namespace) -> dict[str, Any]:
    catalog_path = Path(args.catalog) if args.catalog else DEFAULT_CATALOG
    gameplay_path = Path(args.gameplay_profiles) if args.gameplay_profiles else DEFAULT_GAMEPLAY_PROFILES
    try:
        catalog = load_catalog(catalog_path)
        profiles = load_gameplay_profiles(gameplay_path, catalog)
        payload = build_gameplay_catalog(
            catalog,
            profiles,
            args.start_year,
            args.end_year,
            set(args.domain) if args.domain else set(TRANSPORT_DOMAINS),
        )
    except CatalogError as exc:
        raise PipelineError(str(exc)) from exc
    destination = Path(args.output).expanduser().resolve()
    _write_text_artifact(
        destination,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        force=args.force,
    )
    return {
        "ok": True,
        "output": str(destination),
        **payload["summary"],
    }


def command_gameplay_catalog(args: argparse.Namespace) -> None:
    print(json.dumps(generate_gameplay_catalog(args), ensure_ascii=False, indent=2))


def command_transport_factory(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).expanduser().resolve()
    gameplay_args = argparse.Namespace(
        catalog=args.catalog,
        gameplay_profiles=args.gameplay_profiles,
        start_year=args.start_year,
        end_year=args.end_year,
        domain=sorted(TRANSPORT_DOMAINS),
        output=str(output_dir / "transport-gameplay-catalog.json"),
        force=args.force,
    )
    gameplay_result = generate_gameplay_catalog(gameplay_args)
    batch_args = argparse.Namespace(
        catalog=args.catalog,
        gameplay_profiles=args.gameplay_profiles,
        start_year=args.start_year,
        end_year=args.end_year,
        region=args.region,
        domain=sorted(TRANSPORT_DOMAINS),
        archetype=args.archetype,
        state=["base"] if args.base_only else None,
        include_disaster_states=not args.base_only,
        max_prompts=args.max_prompts,
        output_dir=str(output_dir),
        force=args.force,
    )
    batch_result = generate_prompt_batch(batch_args)
    runbook = {
        "schemaVersion": 1,
        "pipelineVersion": PIPELINE_VERSION,
        "scope": "all transport archetypes, 1950-2999; buildings and infrastructure excluded",
        "stateMachine": [
            "prompt-ready", "image-queued", "image-ready", "mesh-ready",
            "optimized", "published", "unity-verified",
        ],
        "chatgpt2codex": {
            "imageMode": "very-high",
            "buildModel": "gpt-5.6-sol",
            "reasoningEffort": "xhigh",
            "imageQueue": batch_result["imageQueue"],
            "buildQueue": batch_result["buildQueue"],
        },
        "rules": [
            "Process one immutable prompt job per image generation call.",
            "Reject inconsistent front/side/rear/top geometry before mesh generation.",
            "Base, damaged and wrecked are mesh states; day/night, smoke, fire and explosion are runtime material/VFX states.",
            "Reuse a catalog recipe and shared VFX pack before adding code.",
            "Paid mesh providers, credentials, commits, pushes and unrestricted Blender scripting remain approval-gated.",
            "Do not mark a job verified without Unity multiview, scale, pivot, LOD and VFX-socket proof.",
        ],
    }
    runbook_path = output_dir / "chatgpt2codex-transport-factory-runbook.json"
    _write_text_artifact(
        runbook_path,
        json.dumps(runbook, ensure_ascii=False, indent=2) + "\n",
        force=args.force,
    )

    publication = None
    if args.unity_project:
        project = require_unity_project(Path(args.unity_project))
        target = project / "Assets" / "Resources" / "RouteFoundry" / "Transport"
        gameplay_status = copy_file(
            Path(gameplay_result["output"]), target / "transport-gameplay-catalog.json", force=args.force
        )
        vfx_status = copy_file(
            Path(batch_result["vfxManifest"]), target / "transport-vfx-pack.json", force=args.force
        )
        publication = {
            "target": str(target),
            "gameplayCatalog": gameplay_status,
            "vfxManifest": vfx_status,
        }

    print(
        json.dumps(
            {
                "ok": True,
                "pipelineVersion": PIPELINE_VERSION,
                "gameplay": gameplay_result,
                "imageAndBuildQueues": batch_result,
                "runbook": str(runbook_path),
                "publication": publication,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ai-unity-asset")
    sub = root.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify globally installed tools")
    doctor.add_argument("--project")
    doctor.set_defaults(handler=command_doctor)

    init = sub.add_parser("init", help="install safe Unity import defaults")
    init.add_argument("--project", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=command_init)

    prompt = sub.add_parser("prompt", help="render a fixed multi-view ImageGen prompt")
    prompt.add_argument("--asset-id", required=True)
    prompt.add_argument("--subject", required=True)
    prompt.add_argument("--year", type=int, required=True)
    prompt.add_argument("--region", default="global neutral industrial design")
    prompt.add_argument("--technology", default="period-correct production technology")
    prompt.add_argument("--livery", default="unbranded neutral production materials")
    prompt.add_argument("--details", default="physically plausible production details")
    prompt.add_argument("--asset-state", choices=tuple(ASSET_STATE_INSTRUCTIONS), default="base")
    prompt.add_argument("--output")
    prompt.add_argument("--json", action="store_true")
    prompt.add_argument("--force", action="store_true")
    prompt.set_defaults(handler=command_prompt)

    prompt_batch = sub.add_parser(
        "prompt-batch", help="build era-specific ImageGen and ChatGPT To Codex queues"
    )
    prompt_batch.add_argument("--catalog")
    prompt_batch.add_argument("--gameplay-profiles")
    prompt_batch.add_argument("--start-year", type=int, default=1950)
    prompt_batch.add_argument("--end-year", type=int, default=2999)
    prompt_batch.add_argument("--region", default="global-neutral")
    prompt_batch.add_argument(
        "--domain",
        action="append",
        choices=(
            "road", "micromobility", "rail", "air", "water", "cable",
            "space", "building", "industry", "infrastructure",
        ),
    )
    prompt_batch.add_argument("--archetype", action="append")
    prompt_batch.add_argument("--state", action="append", choices=tuple(ASSET_STATE_INSTRUCTIONS))
    prompt_batch.add_argument("--include-disaster-states", action="store_true")
    prompt_batch.add_argument("--max-prompts", type=int, default=0)
    prompt_batch.add_argument("--output-dir", required=True)
    prompt_batch.add_argument("--force", action="store_true")
    prompt_batch.set_defaults(handler=command_prompt_batch)

    fleet = sub.add_parser("fleet", help="build the six-vehicle reference-matched fleet")
    fleet.add_argument("--reference", required=True)
    fleet.add_argument("--output-dir", required=True)
    fleet.add_argument("--unity-project")
    fleet.add_argument("--unity-destination", default=DEFAULT_DESTINATION)
    fleet.add_argument("--lod0", type=int, default=36000)
    fleet.add_argument("--lod1", type=int, default=9000)
    fleet.add_argument("--lod2", type=int, default=1800)
    fleet.add_argument("--force", action="store_true")
    fleet.set_defaults(handler=command_fleet)

    optimize = sub.add_parser("optimize", help="normalize an authorized provider mesh")
    optimize.add_argument("--input", required=True)
    optimize.add_argument("--output-dir", required=True)
    optimize.add_argument("--target-size", type=float, default=4.0)
    optimize.add_argument("--lod0", type=int, default=6000)
    optimize.add_argument("--lod1", type=int, default=2000)
    optimize.add_argument("--lod2", type=int, default=700)
    optimize.set_defaults(handler=command_optimize)

    publish = sub.add_parser("publish", help="copy generated assets into a Unity project")
    publish.add_argument("--source-dir", required=True)
    publish.add_argument("--project", required=True)
    publish.add_argument("--destination", default=DEFAULT_DESTINATION)
    publish.add_argument("--reference")
    publish.add_argument("--force", action="store_true")
    publish.set_defaults(handler=command_publish)

    validate = sub.add_parser("validate", help="validate files and triangle budgets")
    validate.add_argument("--manifest", required=True)
    validate.set_defaults(handler=command_validate)

    catalog = sub.add_parser(
        "catalog", help="plan a reusable 1950-2999 multi-domain asset factory"
    )
    catalog.add_argument("--catalog")
    catalog.add_argument("--start-year", type=int, default=1950)
    catalog.add_argument("--end-year", type=int, default=2999)
    catalog.add_argument(
        "--domain",
        action="append",
        choices=(
            "road", "micromobility", "rail", "air", "water", "cable",
            "space", "building", "industry", "infrastructure",
        ),
    )
    catalog.add_argument("--output-plan")
    catalog.set_defaults(handler=command_catalog)

    gameplay = sub.add_parser(
        "gameplay-catalog", help="resolve complete per-era transport gameplay and failure data"
    )
    gameplay.add_argument("--catalog")
    gameplay.add_argument("--gameplay-profiles")
    gameplay.add_argument("--start-year", type=int, default=1950)
    gameplay.add_argument("--end-year", type=int, default=2999)
    gameplay.add_argument(
        "--domain",
        action="append",
        choices=tuple(sorted(TRANSPORT_DOMAINS)),
    )
    gameplay.add_argument("--output", required=True)
    gameplay.add_argument("--force", action="store_true")
    gameplay.set_defaults(handler=command_gameplay_catalog)

    factory = sub.add_parser(
        "transport-factory",
        help="build all-transport gameplay, ImageGen, ChatGPT To Codex and VFX queues",
    )
    factory.add_argument("--catalog")
    factory.add_argument("--gameplay-profiles")
    factory.add_argument("--start-year", type=int, default=1950)
    factory.add_argument("--end-year", type=int, default=2999)
    factory.add_argument("--region", default="global-neutral")
    factory.add_argument("--archetype", action="append")
    factory.add_argument("--max-prompts", type=int, default=0)
    factory.add_argument("--base-only", action="store_true")
    factory.add_argument("--output-dir", required=True)
    factory.add_argument("--unity-project")
    factory.add_argument("--force", action="store_true")
    factory.set_defaults(handler=command_transport_factory)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
    except PipelineError as exc:
        print(f"ai-unity-asset: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
