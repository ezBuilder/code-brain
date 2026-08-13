#!/usr/bin/env python3
"""Validate and expand a data-driven long-horizon transport asset catalog."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


TRANSPORT_DOMAINS = frozenset({"road", "micromobility", "rail", "air", "water", "cable", "space"})


class CatalogError(RuntimeError):
    pass


def _require_int(value: Any, label: str) -> int:
    if not isinstance(value, int):
        raise CatalogError(f"{label} must be an integer")
    return value


def _axis_count(value: Any, label: str) -> int:
    if value is None:
        return 1
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be an object")
    total = 1
    for axis, options in value.items():
        if not isinstance(axis, str) or not axis or not isinstance(options, list) or not options:
            raise CatalogError(f"{label}.{axis} must contain at least one option")
        if any(not isinstance(option, str) or not option for option in options):
            raise CatalogError(f"{label}.{axis} options must be non-empty strings")
        total *= len(options)
    return total


def load_catalog(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CatalogError(f"catalog not found: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"invalid catalog JSON: {exc}") from exc
    validate_catalog(payload)
    return payload


def load_gameplay_profiles(path: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CatalogError(f"gameplay profiles not found: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"invalid gameplay profile JSON: {exc}") from exc
    validate_gameplay_profiles(payload, catalog)
    return payload


def validate_gameplay_profiles(payload: dict[str, Any], catalog: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise CatalogError("gameplay profile schemaVersion must be 1")
    required = payload.get("requiredResolvedFields")
    if not isinstance(required, list) or len(required) != len(set(required)) or any(
        not isinstance(field, str) or not field for field in required
    ):
        raise CatalogError("gameplay requiredResolvedFields must be unique strings")
    era_scalars = payload.get("eraScalars")
    if not isinstance(era_scalars, dict):
        raise CatalogError("gameplay eraScalars are required")
    for era in catalog["eraProfiles"]:
        scalar = era_scalars.get(era["id"])
        if not isinstance(scalar, dict):
            raise CatalogError(f"missing gameplay era scalar: {era['id']}")
        for key in ("speed", "capacity", "range", "power", "purchase", "running", "maintenance", "loading", "safety"):
            if not isinstance(scalar.get(key), (int, float)) or scalar[key] <= 0:
                raise CatalogError(f"invalid gameplay era scalar: {era['id']}.{key}")
    defaults = payload.get("domainDefaults")
    if not isinstance(defaults, dict) or any(domain not in defaults for domain in TRANSPORT_DOMAINS):
        raise CatalogError("gameplay domainDefaults must cover every transport domain")
    failures = payload.get("failureProfiles")
    if not isinstance(failures, dict) or not failures:
        raise CatalogError("gameplay failureProfiles are required")
    profiles = payload.get("archetypeProfiles")
    if not isinstance(profiles, dict):
        raise CatalogError("gameplay archetypeProfiles are required")
    transport_ids = {
        asset["id"] for asset in catalog["archetypes"] if asset["domain"] in TRANSPORT_DOMAINS
    }
    profile_ids = set(profiles)
    if transport_ids != profile_ids:
        missing = sorted(transport_ids - profile_ids)
        extra = sorted(profile_ids - transport_ids)
        raise CatalogError(f"gameplay archetype coverage mismatch: missing={missing}, extra={extra}")
    for asset_id, override in profiles.items():
        if not isinstance(override, dict):
            raise CatalogError(f"gameplay archetype profile must be an object: {asset_id}")


def _era_for_year(catalog: dict[str, Any], year: int) -> dict[str, Any]:
    for era in catalog["eraProfiles"]:
        if era["startYear"] <= year <= era["endYear"]:
            return era
    raise CatalogError(f"no era covers year {year}")


def _evidence_for_year(profiles: dict[str, Any], year: int) -> dict[str, Any]:
    for tier in profiles.get("evidenceTiers", []):
        if tier["startYear"] <= year <= tier["endYear"]:
            return tier
    raise CatalogError(f"no evidence tier covers year {year}")


def _round(value: float, digits: int = 2) -> int | float:
    rounded = round(value, digits)
    return int(rounded) if rounded.is_integer() else rounded


def _energy_failure_profile(domain: str, energy: str, fallback: str) -> str:
    normalized = energy.lower()
    if "field" in normalized or "programmable" in normalized:
        return "field-propulsion"
    if domain == "space":
        if "nuclear" in normalized:
            return "spacecraft-nuclear"
        if "electric" in normalized or "fusion" in normalized:
            return "spacecraft-electric"
        return "spacecraft-chemical"
    if domain == "air" and ("electric" in normalized or "hydrogen" in normalized):
        return "aviation-electric"
    if domain == "water" and ("electric" in normalized or "hydrogen" in normalized):
        return "maritime-electric"
    if domain in {"road", "micromobility"} and "electric" in normalized:
        return "electric-ground"
    return fallback


def build_gameplay_catalog(
    catalog: dict[str, Any],
    profiles: dict[str, Any],
    start_year: int,
    end_year: int,
    domains: set[str] | None = None,
) -> dict[str, Any]:
    selected_domains = set(domains or TRANSPORT_DOMAINS)
    unknown = selected_domains - TRANSPORT_DOMAINS
    if unknown:
        raise CatalogError(f"gameplay catalog accepts transport domains only: {sorted(unknown)}")
    timeline = catalog["timeline"]
    if start_year < timeline["startYear"] or end_year > timeline["endYear"] or start_year > end_year:
        raise CatalogError(
            f"requested range must stay within {timeline['startYear']}..{timeline['endYear']}"
        )

    asset_states = profiles["assetStatePolicy"]["meshReferenceStates"]
    models: list[dict[str, Any]] = []
    for era in catalog["eraProfiles"]:
        overlap_start = max(start_year, era["startYear"])
        overlap_end = min(end_year, era["endYear"])
        if overlap_start > overlap_end:
            continue
        current_scalar = profiles["eraScalars"][era["id"]]
        for asset in catalog["archetypes"]:
            if asset["domain"] not in selected_domains:
                continue
            active_start = max(overlap_start, asset["availableFrom"])
            active_end = min(overlap_end, asset["availableTo"])
            if active_start > active_end:
                continue
            design_year = (active_start + active_end) // 2
            reference_year = int(
                profiles["archetypeProfiles"][asset["id"]].get(
                    "referenceYear", min(max(2025, asset["availableFrom"]), asset["availableTo"])
                )
            )
            reference_era = _era_for_year(catalog, reference_year)
            reference_scalar = profiles["eraScalars"][reference_era["id"]]
            base = {
                **profiles["domainDefaults"][asset["domain"]],
                **profiles["archetypeProfiles"][asset["id"]],
            }

            def scale(field: str, scalar: str) -> float:
                return float(base[field]) * float(current_scalar[scalar]) / float(reference_scalar[scalar])

            reliability = min(
                99.5,
                max(
                    60.0,
                    float(base["reliabilityPercent"])
                    + float(current_scalar["reliabilityDelta"])
                    - float(reference_scalar["reliabilityDelta"]),
                ),
            )
            safety_ratio = float(current_scalar["safety"]) / float(reference_scalar["safety"])
            breakdown_distance = (
                float(base["breakdownMeanDistanceKm"])
                * (reliability / float(base["reliabilityPercent"])) ** 2
                / safety_ratio
            )
            energy = profiles.get("domainEnergyByEra", {}).get(asset["domain"], {}).get(
                era["id"], base["energyCarrier"]
            )
            failure_id = _energy_failure_profile(
                asset["domain"], str(energy), str(base["failureProfileId"])
            )
            failure = profiles["failureProfiles"].get(failure_id)
            if not isinstance(failure, dict):
                raise CatalogError(f"unknown failure profile {failure_id} for {asset['id']}")
            evidence = _evidence_for_year(profiles, design_year)
            maximum_speed = scale("speedKph", "speed")
            model = {
                "modelId": f"{asset['domain']}-{asset['id']}-{era['id']}",
                "archetypeId": asset["id"],
                "domain": asset["domain"],
                "recipe": asset["recipe"],
                "eraId": era["id"],
                "designYear": design_year,
                "availableFrom": active_start,
                "availableTo": active_end,
                "evidenceTier": evidence["id"],
                "technologyReadinessLevel": evidence["defaultTrl"],
                "role": base["role"],
                "capacityUnit": base["capacityUnit"],
                "capacity": round(scale("capacity", "capacity")),
                "cargoClasses": base["cargoClasses"],
                "maximumSpeedKph": _round(maximum_speed, 1),
                "cruiseSpeedKph": _round(maximum_speed * float(base["cruiseRatio"]), 1),
                "rangeKm": _round(scale("rangeKm", "range"), 1),
                "massTonnes": _round(float(base["massTonnes"]), 3),
                "powerKw": _round(scale("powerKw", "power"), 1),
                "accelerationMps2": _round(float(base["accelerationMps2"]) * current_scalar["power"] / reference_scalar["power"], 2),
                "dimensionsMeters": base["dimensionsMeters"],
                "purchaseCostCredits": round(scale("purchaseKCredits", "purchase") * 1000),
                "runningCostCreditsPerKm": _round(scale("runningCreditsPerKm", "running"), 3),
                "maintenanceCostCreditsPerYear": round(scale("maintenanceKCreditsPerYear", "maintenance") * 1000),
                "serviceLifeYears": base["serviceLifeYears"],
                "modelSupportYears": base["modelSupportYears"],
                "baseReliabilityPercent": _round(reliability, 1),
                "annualReliabilityDecayPercent": _round(float(base["reliabilityDecayPercent"]) * safety_ratio, 2),
                "breakdownMeanDistanceKm": round(breakdown_distance),
                "catastrophicFailureRatePerMillionKm": _round(float(base["catastrophicFailureRatePerMillionKm"]) * safety_ratio, 5),
                "loadingUnitsPerMinute": _round(scale("loadingUnitsPerMinute", "loading"), 1),
                "energyCarrier": energy,
                "emissionsClass": (
                    "zero-tailpipe"
                    if any(token in str(energy) for token in ("electric", "human", "field", "programmable"))
                    else "low" if any(token in str(energy) for token in ("hydrogen", "green-methanol"))
                    else base["emissionsClass"]
                ),
                "noiseClass": base["noiseClass"],
                "infrastructureClass": base["infrastructureClass"],
                "hazardClass": base["hazardClass"],
                "failureProfileId": failure_id,
                "fireProbabilityGivenFailure": failure["fireProbabilityGivenFailure"],
                "explosionProbabilityGivenFire": failure["explosionProbabilityGivenFire"],
                "assetStates": asset_states,
                "runtimeVfxPacks": failure["runtimeVfx"],
                "meshAxes": asset.get("meshAxes", {}),
                "materialAxes": asset.get("materialAxes", {}),
                "addressableLabel": f"asset-era-{era['id']}",
            }
            missing = [field for field in profiles["requiredResolvedFields"] if field not in model]
            if missing:
                raise CatalogError(f"resolved gameplay model {model['modelId']} is missing {missing}")
            models.append(model)

    if not models:
        raise CatalogError("no gameplay models match the requested range and domains")
    return {
        "schemaVersion": 1,
        "catalogId": catalog.get("id", "transport-catalog"),
        "gameplayProfileId": profiles["id"],
        "requestedYears": [start_year, end_year],
        "currency": profiles["currency"],
        "futureDataPolicy": {
            "observedThrough": 2026,
            "projectedThrough": 2069,
            "extrapolatedThrough": 2149,
            "speculativeFrom": 2150,
            "rule": "Future values are deterministic game balance, not real-world predictions.",
        },
        "assetStatePolicy": profiles["assetStatePolicy"],
        "summary": {
            "modelCount": len(models),
            "archetypeCount": len({model["archetypeId"] for model in models}),
            "domains": sorted({model["domain"] for model in models}),
            "eras": sorted({model["eraId"] for model in models}),
        },
        "models": models,
    }


def validate_catalog(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise CatalogError("catalog schemaVersion must be 1")
    timeline = payload.get("timeline")
    if not isinstance(timeline, dict):
        raise CatalogError("catalog timeline is required")
    timeline_start = _require_int(timeline.get("startYear"), "timeline.startYear")
    timeline_end = _require_int(timeline.get("endYear"), "timeline.endYear")
    if timeline_start > timeline_end:
        raise CatalogError("catalog timeline is reversed")
    regional_profiles = payload.get("regionalProfiles", [])
    if not isinstance(regional_profiles, list) or not regional_profiles or any(
        not isinstance(profile, str) or not profile for profile in regional_profiles
    ) or len(set(regional_profiles)) != len(regional_profiles):
        raise CatalogError("catalog regionalProfiles must be unique non-empty strings")

    eras = payload.get("eraProfiles")
    if not isinstance(eras, list) or not eras:
        raise CatalogError("catalog needs eraProfiles")
    era_ids: set[str] = set()
    expected_start = timeline_start
    for era in eras:
        if not isinstance(era, dict):
            raise CatalogError("invalid era profile")
        era_id = era.get("id")
        start = _require_int(era.get("startYear"), f"era {era_id} startYear")
        end = _require_int(era.get("endYear"), f"era {era_id} endYear")
        if not isinstance(era_id, str) or not era_id or era_id in era_ids:
            raise CatalogError(f"duplicate or invalid era id: {era_id}")
        if start != expected_start or end < start:
            raise CatalogError(f"era {era_id} must continue the timeline at {expected_start}")
        performance = era.get("performanceProfile")
        road_speeds = performance.get("roadSpeedKph") if isinstance(performance, dict) else None
        if not isinstance(road_speeds, dict) or any(
            not isinstance(road_speeds.get(kind), (int, float)) or road_speeds[kind] <= 0
            for kind in ("bus", "truck")
        ):
            raise CatalogError(f"era {era_id} needs positive bus/truck roadSpeedKph values")
        era_ids.add(era_id)
        expected_start = end + 1
    if expected_start - 1 != timeline_end:
        raise CatalogError("eraProfiles do not cover the complete timeline")

    recipes = payload.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        raise CatalogError("catalog needs recipes")
    recipe_ids: set[str] = set()
    for recipe in recipes:
        if not isinstance(recipe, dict):
            raise CatalogError("invalid recipe")
        recipe_id = recipe.get("id")
        if not isinstance(recipe_id, str) or not recipe_id or recipe_id in recipe_ids:
            raise CatalogError(f"duplicate or invalid recipe id: {recipe_id}")
        if not isinstance(recipe.get("generator"), str) or not recipe["generator"]:
            raise CatalogError(f"recipe {recipe_id} has no generator")
        recipe_ids.add(recipe_id)

    archetypes = payload.get("archetypes")
    if not isinstance(archetypes, list) or not archetypes:
        raise CatalogError("catalog needs archetypes")
    asset_ids: set[str] = set()
    for asset in archetypes:
        if not isinstance(asset, dict):
            raise CatalogError("invalid archetype")
        asset_id = asset.get("id")
        recipe_id = asset.get("recipe")
        domain = asset.get("domain")
        start = _require_int(asset.get("availableFrom"), f"asset {asset_id} availableFrom")
        end = _require_int(asset.get("availableTo"), f"asset {asset_id} availableTo")
        if not isinstance(asset_id, str) or not asset_id or asset_id in asset_ids:
            raise CatalogError(f"duplicate or invalid asset id: {asset_id}")
        if recipe_id not in recipe_ids:
            raise CatalogError(f"asset {asset_id} references unknown recipe {recipe_id}")
        if not isinstance(domain, str) or not domain:
            raise CatalogError(f"asset {asset_id} has no domain")
        if start < timeline_start or end > timeline_end or start > end:
            raise CatalogError(f"asset {asset_id} availability is outside the timeline")
        _axis_count(asset.get("meshAxes"), f"asset {asset_id} meshAxes")
        _axis_count(asset.get("materialAxes"), f"asset {asset_id} materialAxes")
        asset_ids.add(asset_id)


def build_plan(
    payload: dict[str, Any],
    start_year: int,
    end_year: int,
    domains: set[str] | None = None,
) -> dict[str, Any]:
    timeline = payload["timeline"]
    if start_year < timeline["startYear"] or end_year > timeline["endYear"] or start_year > end_year:
        raise CatalogError(
            f"requested range must stay within {timeline['startYear']}..{timeline['endYear']}"
        )

    selected_assets = [
        asset
        for asset in payload["archetypes"]
        if not domains or asset["domain"] in domains
    ]
    if not selected_assets:
        raise CatalogError("no archetypes match the requested domains")

    batches: list[dict[str, Any]] = []
    domain_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"archetypes": 0, "uniqueMeshes": 0, "logicalVariants": 0}
    )
    active_archetypes: set[str] = set()
    recipes: set[str] = set()
    total_meshes = 0
    total_logical = 0

    for era in payload["eraProfiles"]:
        overlap_start = max(start_year, era["startYear"])
        overlap_end = min(end_year, era["endYear"])
        if overlap_start > overlap_end:
            continue
        items = []
        for asset in selected_assets:
            active_start = max(overlap_start, asset["availableFrom"])
            active_end = min(overlap_end, asset["availableTo"])
            if active_start > active_end:
                continue
            mesh_count = _axis_count(asset.get("meshAxes"), f"asset {asset['id']} meshAxes")
            material_count = _axis_count(
                asset.get("materialAxes"), f"asset {asset['id']} materialAxes"
            )
            logical_count = mesh_count * material_count
            refresh_years = max(1, int(asset.get("materialRefreshYears", 25)))
            release_waves = max(1, math.ceil((active_end - active_start + 1) / refresh_years))
            item = {
                "assetId": asset["id"],
                "domain": asset["domain"],
                "recipe": asset["recipe"],
                "activeYears": [active_start, active_end],
                "meshAxes": asset.get("meshAxes", {}),
                "materialAxes": asset.get("materialAxes", {}),
                "uniqueMeshes": mesh_count,
                "materialVariantsPerMesh": material_count,
                "logicalVariants": logical_count,
                "materialReleaseWaves": release_waves,
                "addressableLabel": f"asset-era-{era['id']}",
            }
            items.append(item)
            active_archetypes.add(asset["id"])
            recipes.add(asset["recipe"])
            total_meshes += mesh_count
            total_logical += logical_count
            totals = domain_totals[asset["domain"]]
            totals["uniqueMeshes"] += mesh_count
            totals["logicalVariants"] += logical_count
        if items:
            batches.append(
                {
                    "eraId": era["id"],
                    "years": [overlap_start, overlap_end],
                    "styleTags": era.get("styleTags", []),
                    "performanceProfile": era["performanceProfile"],
                    "items": items,
                }
            )

    for asset in selected_assets:
        if asset["id"] in active_archetypes:
            domain_totals[asset["domain"]]["archetypes"] += 1

    return {
        "schemaVersion": 1,
        "catalogId": payload.get("id", "transport-catalog"),
        "requestedYears": [start_year, end_year],
        "strategy": {
            "geometry": "one shared parametric mesh per era and mesh-axis combination",
            "materials": "GPU-instanced variants share geometry and texture arrays",
            "regionalization": "regional liveries, signage, drive-side and climate overlays do not duplicate base meshes",
            "runtime": "Addressables labels load only the current and adjacent eras",
        },
        "summary": {
            "archetypes": len(active_archetypes),
            "recipes": len(recipes),
            "eraBatches": len(batches),
            "uniqueMeshes": total_meshes,
            "logicalVariants": total_logical,
            "meshReuseRatio": round(total_logical / max(1, total_meshes), 2),
            "regionalProfiles": len(payload.get("regionalProfiles", [])),
            "domains": dict(sorted(domain_totals.items())),
        },
        "batches": batches,
    }
