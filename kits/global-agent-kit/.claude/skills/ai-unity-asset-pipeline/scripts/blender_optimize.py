"""Blender background script: normalize, generate LODs, export FBX/GLB."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def arguments() -> argparse.Namespace:
    separator = sys.argv.index("--") if "--" in sys.argv else len(sys.argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target-size", type=float, default=4.0)
    parser.add_argument("--lod0", type=int, default=6000)
    parser.add_argument("--lod1", type=int, default=2000)
    parser.add_argument("--lod2", type=int, default=700)
    return parser.parse_args(sys.argv[separator + 1 :])


def import_mesh(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(path))
    else:
        raise RuntimeError(f"unsupported mesh format: {suffix}")


def mesh_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]


def join_meshes() -> bpy.types.Object:
    meshes = mesh_objects()
    if not meshes:
        raise RuntimeError("input contains no mesh objects")
    if len(meshes) == 1:
        meshes[0].name = "AIAsset"
        return meshes[0]
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.join()
    joined = bpy.context.view_layer.objects.active
    joined.name = "AIAsset"
    return joined


def world_bounds(obj: bpy.types.Object) -> tuple[Vector, Vector]:
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    minimum = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
    maximum = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
    return minimum, maximum


def normalize(obj: bpy.types.Object, target_size: float) -> list[float]:
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    minimum, maximum = world_bounds(obj)
    dimensions = maximum - minimum
    longest = max(dimensions)
    if longest <= 0:
        raise RuntimeError("input mesh has empty bounds")
    scale = target_size / longest
    obj.scale = (scale, scale, scale)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    minimum, maximum = world_bounds(obj)
    center = (minimum + maximum) * 0.5
    offset = Vector((-center.x, -center.y, -minimum.z))
    for vertex in obj.data.vertices:
        vertex.co += offset
    obj.location = (0.0, 0.0, 0.0)
    if len(obj.data.uv_layers) == 0:
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(island_margin=0.02)
        bpy.ops.object.mode_set(mode="OBJECT")
    return [round(value, 6) for value in dimensions]


def triangles(obj: bpy.types.Object) -> int:
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def lod_copy(source: bpy.types.Object, name: str, budget: int) -> bpy.types.Object:
    duplicate = source.copy()
    duplicate.data = source.data.copy()
    bpy.context.collection.objects.link(duplicate)
    duplicate.name = name
    current = triangles(duplicate)
    if current > budget:
        modifier = duplicate.modifiers.new(name="GameReadyDecimate", type="DECIMATE")
        modifier.ratio = max(0.01, min(1.0, budget / current * 0.97))
        modifier.use_collapse_triangulate = True
        bpy.context.view_layer.objects.active = duplicate
        duplicate.select_set(True)
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    return duplicate


def export_object(obj: bpy.types.Object, base: Path) -> dict[str, object]:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    fbx = base.with_suffix(".fbx")
    glb = base.with_suffix(".glb")
    bpy.ops.export_scene.fbx(
        filepath=str(fbx),
        use_selection=True,
        object_types={"MESH"},
        apply_unit_scale=True,
        bake_space_transform=True,
        axis_forward="-Z",
        axis_up="Y",
        add_leaf_bones=False,
        use_mesh_modifiers=True,
        path_mode="AUTO",
    )
    bpy.ops.export_scene.gltf(
        filepath=str(glb), export_format="GLB", use_selection=True, export_apply=True, export_yup=True
    )
    return {"triangles": triangles(obj), "fbx": fbx.name, "glb": glb.name}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    args = arguments()
    source = Path(args.input).resolve()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    import_mesh(source)
    joined = join_meshes()
    bounds = normalize(joined, args.target_size)
    stem = source.stem.lower().replace(" ", "-")
    budgets = {"lod0": args.lod0, "lod1": args.lod1, "lod2": args.lod2}
    lods = []
    for level, budget in budgets.items():
        current = lod_copy(joined, f"{stem}_{level}", budget)
        result = export_object(current, output / current.name)
        if result["triangles"] > budget:
            raise RuntimeError(f"{level} triangle budget failed: {result['triangles']} > {budget}")
        lods.append({"level": level, **result})
        bpy.data.objects.remove(current, do_unlink=True)
    manifest = {
        "schemaVersion": 1,
        "pipelineVersion": "1.0.0",
        "generator": "blender-optimize",
        "source": {"path": source.name, "sha256": sha256(source)},
        "triangleBudgets": budgets,
        "assets": [{"id": stem, "bounds": bounds, "lods": lods}],
    }
    (output / "asset-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"AI_UNITY_ASSET_OPTIMIZE_OK: {output}")


if __name__ == "__main__":
    main()
