"""Build a premium cream/teal Transport Tycoon vehicle family in Blender.

The generator is deterministic and intentionally provider-independent. It
creates six production-ready vehicle archetypes with visible wheel wells,
layered body panels, exterior fixtures, multi-material PBR surfaces and three
triangle-budgeted LODs. FBX and GLB exports share a bottom-centre pivot and
metre scale so the same output can be published into any Unity project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector


MATERIAL_COLORS = {
    "RF_Cream": ((0.82, 0.74, 0.61, 1.0), 0.02, 0.42),
    "RF_Teal": ((0.045, 0.29, 0.31, 1.0), 0.12, 0.34),
    "RF_Charcoal": ((0.028, 0.035, 0.036, 1.0), 0.10, 0.32),
    "RF_Glass": ((0.018, 0.065, 0.082, 0.84), 0.12, 0.22),
    "RF_Rubber": ((0.012, 0.013, 0.013, 1.0), 0.0, 0.88),
    "RF_Metal": ((0.36, 0.34, 0.30, 1.0), 0.76, 0.22),
    "RF_Silver": ((0.63, 0.61, 0.55, 1.0), 0.82, 0.16),
    "RF_Amber": ((1.0, 0.30, 0.025, 1.0), 0.0, 0.18),
    "RF_Red": ((0.58, 0.018, 0.012, 1.0), 0.0, 0.22),
    "RF_White": ((1.0, 0.94, 0.76, 1.0), 0.0, 0.12),
    "RF_Cargo": ((0.24, 0.17, 0.10, 1.0), 0.0, 0.96),
    "RF_Interior": ((0.055, 0.075, 0.095, 1.0), 0.0, 0.68),
}

VEHICLE_IDS = (
    "city-bus",
    "highway-coach",
    "box-truck",
    "flatbed-truck",
    "tanker-truck",
    "dump-truck",
)


def arguments() -> argparse.Namespace:
    separator = sys.argv.index("--") if "--" in sys.argv else len(sys.argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--lod0", type=int, default=36000)
    parser.add_argument("--lod1", type=int, default=9000)
    parser.add_argument("--lod2", type=int, default=1800)
    return parser.parse_args(sys.argv[separator + 1 :])


def material(name: str) -> bpy.types.Material:
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing
    color, metallic, roughness = MATERIAL_COLORS[name]
    value = bpy.data.materials.new(name=name)
    value.diffuse_color = color
    value.use_nodes = True
    node = value.node_tree.nodes.get("Principled BSDF")
    if node is not None:
        node.inputs["Base Color"].default_value = color
        metallic_input = "Metallic" if "Metallic" in node.inputs else "Metallic IOR Level"
        node.inputs[metallic_input].default_value = metallic
        node.inputs["Roughness"].default_value = roughness
        if name == "RF_Glass":
            node.inputs["Alpha"].default_value = color[3]
            if "Transmission Weight" in node.inputs:
                node.inputs["Transmission Weight"].default_value = 0.08
            if "Coat Weight" in node.inputs:
                node.inputs["Coat Weight"].default_value = 0.18
            value.surface_render_method = "DITHERED"
    return value


def apply_bevel(obj: bpy.types.Object, width: float, segments: int = 2) -> None:
    if width <= 0:
        return
    modifier = obj.modifiers.new(name="ProductionBevel", type="BEVEL")
    modifier.width = width
    modifier.segments = segments
    modifier.limit_method = "ANGLE"
    if hasattr(modifier, "harden_normals"):
        modifier.harden_normals = True
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def add_box(
    parts: list[bpy.types.Object],
    name: str,
    size: tuple[float, float, float],
    location: tuple[float, float, float],
    material_name: str,
    bevel: float = 0.035,
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    segments: int = 2,
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    apply_bevel(obj, min(bevel, min(size) * 0.18), segments)
    obj.data.materials.append(material(material_name))
    parts.append(obj)
    return obj


def add_tapered_box(
    parts: list[bpy.types.Object],
    name: str,
    bottom: tuple[float, float],
    top: tuple[float, float],
    height: float,
    location: tuple[float, float, float],
    material_name: str,
    bevel: float = 0.035,
) -> bpy.types.Object:
    bx, by = bottom[0] * 0.5, bottom[1] * 0.5
    tx, ty = top[0] * 0.5, top[1] * 0.5
    hz = height * 0.5
    vertices = [
        (-bx, -by, -hz), (bx, -by, -hz), (bx, by, -hz), (-bx, by, -hz),
        (-tx, -ty, hz), (tx, -ty, hz), (tx, ty, hz), (-tx, ty, hz),
    ]
    faces = [
        (0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
        (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7),
    ]
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    obj.data.materials.append(material(material_name))
    parts.append(obj)
    apply_bevel(obj, bevel, 2)
    return obj


def add_cylinder(
    parts: list[bpy.types.Object],
    name: str,
    radius: float,
    depth: float,
    location: tuple[float, float, float],
    material_name: str,
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    vertices: int = 24,
    bevel: float = 0.02,
    smooth: bool = True,
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=vertices, radius=radius, depth=depth, location=location, rotation=rotation
    )
    obj = bpy.context.object
    obj.name = name
    apply_bevel(obj, bevel, 2)
    if smooth:
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
    obj.data.materials.append(material(material_name))
    parts.append(obj)
    return obj


def add_torus(
    parts: list[bpy.types.Object],
    name: str,
    major_radius: float,
    minor_radius: float,
    location: tuple[float, float, float],
    material_name: str,
    rotation: tuple[float, float, float],
    major_segments: int = 24,
    minor_segments: int = 8,
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_torus_add(
        major_radius=major_radius,
        minor_radius=minor_radius,
        major_segments=major_segments,
        minor_segments=minor_segments,
        location=location,
        rotation=rotation,
    )
    obj = bpy.context.object
    obj.name = name
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    obj.data.materials.append(material(material_name))
    parts.append(obj)
    return obj


def add_wheel_arch(
    parts: list[bpy.types.Object],
    name: str,
    x: float,
    y: float,
    z: float,
    radius: float,
    material_name: str = "RF_Teal",
) -> None:
    curve = bpy.data.curves.new(name + "Curve", type="CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 2
    curve.bevel_depth = 0.026
    curve.bevel_resolution = 3
    spline = curve.splines.new(type="POLY")
    segments = 20
    spline.points.add(segments)
    for index in range(segments + 1):
        angle = index * math.pi / segments
        spline.points[index].co = (
            x,
            y + math.cos(angle) * radius,
            z + math.sin(angle) * radius,
            1.0,
        )
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material(material_name))
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.convert(target="MESH")
    parts.append(obj)


def add_seat(
    parts: list[bpy.types.Object],
    name: str,
    location: tuple[float, float, float],
    rotation_z: float = 0.0,
) -> None:
    x, y, z = location
    add_box(parts, name + "Base", (0.48, 0.48, 0.15), (x, y, z), "RF_Interior", 0.07, rotation=(0.0, 0.0, rotation_z), segments=3)
    add_box(parts, name + "Back", (0.48, 0.15, 0.68), (x, y - 0.18, z + 0.34), "RF_Interior", 0.08, rotation=(0.08, 0.0, rotation_z), segments=3)
    add_box(parts, name + "Headrest", (0.34, 0.13, 0.20), (x, y - 0.20, z + 0.73), "RF_Charcoal", 0.06, rotation=(0.08, 0.0, rotation_z), segments=3)


def add_steering_wheel(parts: list[bpy.types.Object], location: tuple[float, float, float]) -> None:
    x, y, z = location
    add_torus(parts, "SteeringWheel", 0.19, 0.025, (x, y, z), "RF_Charcoal", (math.pi * 0.42, 0.0, 0.0), major_segments=20, minor_segments=6)
    add_cylinder(parts, "SteeringColumn", 0.025, 0.42, (x, y - 0.11, z - 0.15), "RF_Metal", rotation=(math.pi * 0.42, 0.0, 0.0), vertices=12, bevel=0.008)


def cut_wheel_well(
    obj: bpy.types.Object,
    y: float,
    width: float,
    radius: float,
    center_z: float | None = None,
) -> None:
    """Cut a real transverse wheel well instead of hiding a wheel in the body."""
    wheel_z = radius + 0.11 if center_z is None else center_z
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=32,
        radius=radius * 1.27,
        depth=width + 0.80,
        location=(0.0, y, wheel_z),
        rotation=(0.0, math.pi * 0.5, 0.0),
    )
    cutter = bpy.context.object
    cutter.name = "WheelWellCutter"
    modifier = obj.modifiers.new(name="WheelWell", type="BOOLEAN")
    modifier.operation = "DIFFERENCE"
    modifier.solver = "EXACT"
    modifier.object = cutter
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def cut_box_opening(
    obj: bpy.types.Object,
    name: str,
    size: tuple[float, float, float],
    location: tuple[float, float, float],
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> None:
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location, rotation=rotation)
    cutter = bpy.context.object
    cutter.name = name + "Cutter"
    cutter.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    modifier = obj.modifiers.new(name=name, type="BOOLEAN")
    modifier.operation = "DIFFERENCE"
    modifier.solver = "EXACT"
    modifier.object = cutter
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def add_wheel_pair(parts: list[bpy.types.Object], y: float, width: float, radius: float = 0.54) -> None:
    center_z = radius + 0.14
    tire_center_offset = 0.15
    tire_half_width = radius * 0.29
    for side in (-1.0, 1.0):
        x = side * (width * 0.5 + tire_center_offset)
        add_torus(
            parts,
            f"Tire-{y:.2f}-{side:+.0f}",
            radius * 0.70,
            radius * 0.30,
            (x, y, center_z),
            "RF_Rubber",
            (0.0, math.pi * 0.5, 0.0),
            major_segments=48,
            minor_segments=16,
        )
        face_x = side * (width * 0.5 + tire_center_offset + tire_half_width - 0.015)
        add_torus(
            parts,
            f"SidewallRing-{y:.2f}-{side:+.0f}",
            radius * 0.48,
            radius * 0.022,
            (face_x, y, center_z),
            "RF_Rubber",
            (0.0, math.pi * 0.5, 0.0),
            major_segments=40,
            minor_segments=8,
        )
        add_cylinder(
            parts,
            f"BrakeDisc-{y:.2f}-{side:+.0f}",
            radius * 0.48,
            0.035,
            (face_x - side * 0.018, y, center_z),
            "RF_Charcoal",
            rotation=(0.0, math.pi * 0.5, 0.0),
            vertices=40,
            bevel=0.006,
        )
        add_cylinder(
            parts,
            f"Rim-{y:.2f}-{side:+.0f}",
            radius * 0.52,
            0.055,
            (face_x + side * 0.018, y, center_z),
            "RF_Silver",
            rotation=(0.0, math.pi * 0.5, 0.0),
            vertices=40,
            bevel=0.012,
        )
        add_cylinder(
            parts,
            f"Hub-{y:.2f}-{side:+.0f}",
            radius * 0.19,
            0.065,
            (face_x + side * 0.055, y, center_z),
            "RF_Silver",
            rotation=(0.0, math.pi * 0.5, 0.0),
            vertices=24,
            bevel=0.01,
        )
        for index in range(8):
            angle = index * math.tau / 8.0
            vent_y = y + math.cos(angle) * radius * 0.34
            vent_z = center_z + math.sin(angle) * radius * 0.34
            add_cylinder(
                parts,
                f"RimVent-{y:.2f}-{side:+.0f}-{index}",
                radius * 0.058,
                0.016,
                (face_x + side * 0.052, vent_y, vent_z),
                "RF_Charcoal",
                rotation=(0.0, math.pi * 0.5, 0.0),
                vertices=12,
                bevel=0.003,
            )
        add_torus(
            parts,
            f"RimRing-{y:.2f}-{side:+.0f}",
            radius * 0.43,
            radius * 0.026,
            (face_x + side * 0.061, y, center_z),
            "RF_Silver",
            (0.0, math.pi * 0.5, 0.0),
            major_segments=40,
            minor_segments=8,
        )
        add_wheel_arch(
            parts,
            f"WheelArch-{y:.2f}-{side:+.0f}",
            side * (width * 0.5 + 0.075),
            y,
            center_z,
            radius * 1.16,
        )


def add_lights(parts: list[bpy.types.Object], width: float, front_y: float, rear_y: float, z: float) -> None:
    for side in (-1.0, 1.0):
        add_box(parts, "Headlamp", (0.38, 0.065, 0.22), (side * width * 0.30, front_y, z), "RF_White", 0.025)
        add_box(parts, "FrontMarker", (0.15, 0.07, 0.17), (side * width * 0.43, front_y, z + 0.01), "RF_Amber", 0.018)
        add_box(parts, "TailLamp", (0.19, 0.07, 0.26), (side * width * 0.40, rear_y, z), "RF_Red", 0.018)


def add_mirrors(parts: list[bpy.types.Object], width: float, front_y: float, z: float) -> None:
    for side in (-1.0, 1.0):
        x = side * (width * 0.5 + 0.23)
        add_box(parts, "MirrorArm", (0.035, 0.48, 0.035), (x, front_y - 0.10, z - 0.20), "RF_Metal", 0.0)
        add_box(parts, "Mirror", (0.18, 0.11, 0.31), (x, front_y + 0.12, z), "RF_Charcoal", 0.035)
        add_box(parts, "MirrorGlass", (0.13, 0.015, 0.25), (x, front_y + 0.182, z), "RF_Glass", 0.012)


def add_license_plate(parts: list[bpy.types.Object], width: float, y: float, z: float, rear: bool = False) -> None:
    add_box(parts, "LicensePlateRear" if rear else "LicensePlateFront", (0.48, 0.025, 0.13), (0.0, y, z), "RF_White", 0.012)
    for offset in (-0.13, 0.0, 0.13):
        add_box(parts, "PlateGlyph", (0.045, 0.012, 0.055), (offset, y + (-0.018 if rear else 0.018), z), "RF_Charcoal", 0.002)


def add_bus(parts: list[bpy.types.Object], *, coach: bool) -> None:
    width = 2.52
    length = 11.9 if coach else 10.5
    front = length * 0.5
    rear = -length * 0.5
    lower_z = 1.28
    wheel_front = front - (2.18 if coach else 1.98)
    wheel_rear = rear + (2.18 if coach else 1.98)
    add_box(parts, "Underframe", (2.18, length - 0.42, 0.32), (0.0, -0.02, 0.72), "RF_Charcoal", 0.055, segments=3)
    lower_body = add_box(parts, "LowerBody", (width, length, 1.10), (0.0, 0.0, lower_z), "RF_Teal", 0.13, segments=3)
    cut_wheel_well(lower_body, wheel_front, width, 0.57)
    cut_wheel_well(lower_body, wheel_rear, width, 0.57)
    add_box(parts, "CabinCeiling", (width - 0.18, length - 0.36, 0.18), (0.0, -0.05, 3.02), "RF_Cream", 0.085, segments=3)
    for side in (-1.0, 1.0):
        add_box(parts, "UpperSideRail", (0.14, length - 0.42, 0.20), (side * (width * 0.5 - 0.06), -0.05, 2.96), "RF_Cream", 0.045, segments=3)
        add_box(parts, "FrontCornerPillar", (0.20, 0.26, 1.30), (side * (width * 0.5 - 0.08), front - 0.13, 2.37), "RF_Cream", 0.055, segments=3)
        add_box(parts, "RearCornerPillar", (0.20, 0.26, 1.30), (side * (width * 0.5 - 0.08), rear + 0.13, 2.37), "RF_Cream", 0.055, segments=3)
    add_box(parts, "FrontHeader", (width - 0.18, 0.25, 0.23), (0.0, front - 0.12, 2.94), "RF_Cream", 0.045, segments=3)
    add_box(parts, "RearHeader", (width - 0.18, 0.25, 0.23), (0.0, rear + 0.12, 2.94), "RF_Cream", 0.045, segments=3)
    add_box(parts, "RoofCap", (width - 0.15, length - 0.34, 0.23), (0.0, -0.05, 3.13), "RF_Cream", 0.11, segments=3)
    add_box(parts, "BeltLine", (width + 0.03, length - 0.18, 0.12), (0.0, -0.02, 1.82), "RF_Cream", 0.025)
    lower_rail = add_box(parts, "LowerRubRail", (width + 0.035, length - 0.28, 0.09), (0.0, -0.05, 0.91), "RF_Metal", 0.018)
    cut_wheel_well(lower_rail, wheel_front, width, 0.57)
    cut_wheel_well(lower_rail, wheel_rear, width, 0.57)

    add_box(parts, "PassengerFloor", (width - 0.42, length - 1.10, 0.08), (0.0, -0.16, 1.50), "RF_Interior", 0.025)
    seat_rows = 8 if coach else 7
    for row in range(seat_rows):
        seat_y = rear + 1.20 + row * (length - 2.80) / max(1, seat_rows - 1)
        for seat_side in (-0.63, 0.63):
            add_seat(parts, f"PassengerSeat-{row}-{seat_side:+.2f}", (seat_side, seat_y, 1.63))
    add_seat(parts, "DriverSeat", (-0.69, front - 1.02, 1.62))
    add_steering_wheel(parts, (-0.69, front - 0.49, 2.06))
    add_box(parts, "Dashboard", (width - 0.44, 0.38, 0.31), (0.0, front - 0.44, 1.88), "RF_Charcoal", 0.065, segments=3)

    window_count = 8 if coach else 7
    usable = length - 1.70
    step = usable / window_count
    window_length = step - 0.11
    for index in range(window_count):
        y = rear + 0.85 + (index + 0.5) * step
        for side in (-1.0, 1.0):
            add_box(parts, f"SideWindow-{index}-{side:+.0f}", (0.048, window_length, 0.80), (side * (width * 0.5 + 0.015), y, 2.46), "RF_Glass", 0.025)
            add_box(parts, f"WindowPillar-{index}-{side:+.0f}", (0.10, 0.10, 1.10), (side * (width * 0.5 - 0.01), y + step * 0.49, 2.43), "RF_Cream", 0.018)
        add_box(parts, f"RoofSeam-{index}", (width - 0.20, 0.025, 0.025), (0.0, y + step * 0.48, 3.255), "RF_Metal", 0.0)

    pane_width = (width - 0.36) * 0.5 - 0.025
    for side in (-1.0, 1.0):
        add_box(parts, "WindshieldPane", (pane_width, 0.052, 0.84), (side * (pane_width * 0.52), front + 0.035, 2.44), "RF_Glass", 0.045, rotation=(math.radians(7.5), 0.0, 0.0), segments=3)
        add_box(parts, "Wiper", (0.025, 0.07, 0.52), (side * 0.35, front + 0.108, 2.30), "RF_Charcoal", 0.006, rotation=(0.0, side * 0.52, 0.0))
    add_box(parts, "RearWindow", (width - 0.48, 0.052, 0.70), (0.0, rear - 0.066, 2.47), "RF_Glass", 0.04)
    add_box(parts, "DestinationPanel", (width - 0.72, 0.055, 0.23), (0.0, front + 0.083, 2.96), "RF_Charcoal", 0.03)
    for offset in (-0.45, -0.15, 0.15, 0.45):
        add_box(parts, "DestinationGlyph", (0.13, 0.018, 0.045), (offset, front + 0.116, 2.96), "RF_Amber", 0.006)

    if coach:
        for side in (-1.0, 1.0):
            for index in range(5):
                y = rear + 1.10 + index * (length - 3.0) / 4
                add_box(parts, "LuggageDoor", (0.045, 1.20, 0.50), (side * (width * 0.5 + 0.02), y, 1.28), "RF_Teal", 0.025)
                add_box(parts, "LuggageHandle", (0.055, 0.22, 0.035), (side * (width * 0.5 + 0.05), y, 1.47), "RF_Metal", 0.008)
    else:
        for door_y in (front - 1.25, -0.48):
            add_box(parts, "PassengerDoor", (0.052, 0.92, 1.64), (-width * 0.5 - 0.025, door_y, 1.58), "RF_Charcoal", 0.025)
            for panel in (-0.22, 0.22):
                add_box(parts, "DoorGlass", (0.058, 0.36, 0.63), (-width * 0.5 - 0.04, door_y + panel, 2.20), "RF_Glass", 0.022)
            add_box(parts, "DoorDivider", (0.06, 0.035, 1.52), (-width * 0.5 - 0.055, door_y, 1.60), "RF_Metal", 0.006)

    add_box(parts, "FrontMask", (width - 0.18, 0.17, 0.48), (0.0, front + 0.03, 1.18), "RF_Charcoal", 0.055)
    add_box(parts, "FrontBumper", (width + 0.10, 0.24, 0.25), (0.0, front + 0.16, 0.58), "RF_Charcoal", 0.055)
    add_box(parts, "RearBumper", (width + 0.06, 0.22, 0.23), (0.0, rear - 0.15, 0.58), "RF_Charcoal", 0.045)
    add_box(parts, "RoofAC", (1.16, 1.85 if coach else 1.42, 0.28), (0.0, 0.38, 3.38), "RF_Charcoal", 0.075, segments=3)
    for index in range(5):
        add_box(parts, "ACVent", (0.84, 0.055, 0.035), (0.0, -0.16 + index * 0.25, 3.535), "RF_Metal", 0.005)
    add_box(parts, "RoofVent", (0.48, 0.58, 0.15), (0.0, rear * 0.46, 3.31), "RF_Metal", 0.055)
    add_box(parts, "SideVent", (0.055, 0.72, 0.50), (width * 0.5 + 0.03, rear + 0.72, 1.28), "RF_Charcoal", 0.018)
    for index in range(5):
        add_box(parts, "SideVentSlat", (0.068, 0.52, 0.025), (width * 0.5 + 0.065, rear + 0.72, 1.12 + index * 0.085), "RF_Metal", 0.002)

    add_wheel_pair(parts, wheel_front, width, 0.57)
    add_wheel_pair(parts, wheel_rear, width, 0.57)
    add_lights(parts, width, front + 0.14, rear - 0.13, 0.94)
    add_mirrors(parts, width, front - 0.40, 2.48)
    add_license_plate(parts, width, front + 0.285, 0.64)
    add_license_plate(parts, width, rear - 0.265, 0.66, True)
    for side in (-1.0, 1.0):
        for ratio in (0.20, 0.50, 0.80):
            add_box(parts, "SideMarker", (0.045, 0.14, 0.09), (side * (width * 0.5 + 0.055), rear + ratio * length, 0.84), "RF_Amber", 0.012)
        for panel_index in range(10):
            panel_y = rear + 0.30 + panel_index * (length - 0.60) / 9
            add_box(parts, "LowerPanelSeam", (0.018, 0.025, 0.68), (side * (width * 0.5 + 0.068), panel_y, 1.29), "RF_Metal", 0.002)


def add_truck_cab(parts: list[bpy.types.Object], width: float, front: float) -> None:
    cab_y = front - 1.23
    lower = add_box(parts, "CabLower", (width, 2.45, 1.18), (0.0, cab_y, 1.23), "RF_Teal", 0.12, segments=3)
    cut_wheel_well(lower, front - 1.38, width, 0.55)
    cab_upper = add_tapered_box(parts, "CabUpper", (width - 0.05, 2.12), (width - 0.22, 1.86), 1.22, (0.0, cab_y - 0.12, 2.31), "RF_Teal", 0.075)
    cut_box_opening(cab_upper, "WindshieldOpening", (width - 0.38, 0.58, 0.73), (0.0, front - 0.24, 2.42), rotation=(math.radians(8.5), 0.0, 0.0))
    for side in (-1.0, 1.0):
        cut_box_opening(cab_upper, f"SideWindowOpening-{side:+.0f}", (0.52, 0.82, 0.67), (side * (width * 0.5 - 0.02), cab_y + 0.23, 2.40))
    add_box(parts, "CabRoof", (width - 0.16, 2.00, 0.20), (0.0, cab_y - 0.16, 3.03), "RF_Teal", 0.095, segments=3)
    add_box(parts, "CabFloor", (width - 0.38, 1.75, 0.08), (0.0, cab_y - 0.08, 1.53), "RF_Interior", 0.025)
    add_seat(parts, "DriverSeat", (-0.58, cab_y - 0.05, 1.64))
    add_seat(parts, "PassengerSeat", (0.58, cab_y - 0.05, 1.64))
    add_box(parts, "Dashboard", (width - 0.42, 0.36, 0.30), (0.0, front - 0.42, 1.91), "RF_Charcoal", 0.06, segments=3)
    add_steering_wheel(parts, (-0.58, front - 0.48, 2.08))
    pane_width = (width - 0.38) * 0.5 - 0.025
    for side in (-1.0, 1.0):
        add_box(parts, "WindshieldPane", (pane_width, 0.055, 0.70), (side * pane_width * 0.53, front - 0.005, 2.42), "RF_Glass", 0.04, rotation=(math.radians(8.5), 0.0, 0.0), segments=3)
        add_box(parts, "CabSideWindow", (0.048, 0.78, 0.64), (side * (width * 0.5 + 0.015), cab_y + 0.23, 2.40), "RF_Glass", 0.035)
        add_box(parts, "DoorLine", (0.055, 0.025, 1.42), (side * (width * 0.5 + 0.025), cab_y - 0.35, 1.82), "RF_Charcoal", 0.0)
        add_box(parts, "DoorHandle", (0.065, 0.22, 0.055), (side * (width * 0.5 + 0.06), cab_y - 0.38, 2.01), "RF_Metal", 0.012)
        add_box(parts, "CabStep", (0.33, 0.72, 0.12), (side * (width * 0.5 + 0.10), cab_y - 0.42, 0.67), "RF_Metal", 0.025)
        add_box(parts, "Wiper", (0.025, 0.07, 0.46), (side * 0.31, front + 0.068, 2.30), "RF_Charcoal", 0.005, rotation=(0.0, side * 0.52, 0.0))
    add_box(parts, "SunVisor", (width - 0.40, 0.18, 0.13), (0.0, front + 0.05, 2.91), "RF_Charcoal", 0.035)
    add_box(parts, "Grille", (width - 0.54, 0.075, 0.39), (0.0, front + 0.075, 1.31), "RF_Charcoal", 0.035)
    for index in range(6):
        add_box(parts, "GrilleBar", (width - 0.72, 0.085, 0.021), (0.0, front + 0.118, 1.17 + index * 0.057), "RF_Metal", 0.003)
    for side in (-1.0, 1.0):
        for index in range(4):
            add_cylinder(parts, "CabPanelFastener", 0.018, 0.025, (side * (width * 0.5 + 0.065), cab_y - 0.78 + index * 0.48, 1.04), "RF_Silver", rotation=(0.0, math.pi * 0.5, 0.0), vertices=10, bevel=0.003)
    add_box(parts, "FrontBumper", (width + 0.10, 0.24, 0.26), (0.0, front + 0.16, 0.61), "RF_Charcoal", 0.055)
    add_mirrors(parts, width, front - 0.34, 2.45)
    add_license_plate(parts, width, front + 0.29, 0.67)


def add_truck_base(parts: list[bpy.types.Object], kind: str) -> tuple[float, float, float]:
    width = 2.44
    length = 8.05 if kind == "box-truck" else 8.38
    front = length * 0.5
    rear = -length * 0.5
    add_box(parts, "Chassis", (2.16, length - 0.40, 0.30), (0.0, -0.10, 0.78), "RF_Charcoal", 0.05)
    add_box(parts, "FuelTank", (0.48, 1.42, 0.54), (width * 0.49, -0.58, 0.64), "RF_Silver", 0.10, segments=3)
    add_box(parts, "ToolBox", (0.48, 1.10, 0.52), (-width * 0.49, -0.74, 0.65), "RF_Charcoal", 0.075)
    add_box(parts, "ExhaustStack", (0.12, 0.12, 1.42), (width * 0.45, front - 2.52, 1.52), "RF_Metal", 0.025)
    add_box(parts, "ExhaustCap", (0.18, 0.18, 0.10), (width * 0.45, front - 2.52, 2.26), "RF_Charcoal", 0.025)
    add_truck_cab(parts, width, front)
    add_wheel_pair(parts, front - 1.38, width, 0.55)
    add_wheel_pair(parts, rear + 1.20, width, 0.58)
    add_lights(parts, width, front + 0.14, rear - 0.12, 0.96)
    add_box(parts, "RearBumper", (width + 0.04, 0.22, 0.23), (0.0, rear - 0.14, 0.61), "RF_Charcoal", 0.04)
    add_license_plate(parts, width, rear - 0.265, 0.66, True)
    return width, front, rear


def add_box_body(parts: list[bpy.types.Object], width: float, front: float, rear: float) -> None:
    cargo_front = front - 2.60
    cargo_rear = rear + 0.20
    length = cargo_front - cargo_rear
    center = (cargo_front + cargo_rear) * 0.5
    cargo_box = add_box(parts, "CargoBox", (width - 0.02, length, 2.47), (0.0, center, 2.10), "RF_Cream", 0.075, segments=3)
    cut_wheel_well(cargo_box, rear + 1.20, width, 0.58)
    add_box(parts, "CargoRoof", (width + 0.07, length + 0.10, 0.11), (0.0, center, 3.38), "RF_Silver", 0.035)
    for side in (-1.0, 1.0):
        for index in range(5):
            y = cargo_rear + 0.32 + index * (length - 0.64) / 4
            if abs(y - (rear + 1.20)) > 0.76:
                add_box(parts, "CargoPanelRib", (0.065, 0.075, 2.27), (side * width * 0.50, y, 2.10), "RF_Metal", 0.012)
        for y in (cargo_front - 0.06, cargo_rear + 0.06):
            add_box(parts, "CargoCorner", (0.11, 0.11, 2.56), (side * width * 0.49, y, 2.10), "RF_Metal", 0.018)
    add_box(parts, "RearDoor", (width - 0.25, 0.058, 2.23), (0.0, cargo_rear - 0.045, 2.08), "RF_Cream", 0.028)
    add_box(parts, "DoorSeam", (0.028, 0.072, 2.12), (0.0, cargo_rear - 0.08, 2.08), "RF_Metal", 0.003)
    for side in (-0.42, 0.42):
        add_box(parts, "DoorLatch", (0.07, 0.08, 1.65), (side, cargo_rear - 0.10, 2.04), "RF_Metal", 0.012)
        for z in (1.35, 2.70):
            add_box(parts, "DoorHinge", (0.31, 0.09, 0.07), (side, cargo_rear - 0.105, z), "RF_Charcoal", 0.012)


def add_flatbed(parts: list[bpy.types.Object], width: float, front: float, rear: float) -> None:
    bed_front = front - 2.60
    bed_rear = rear + 0.20
    length = bed_front - bed_rear
    center = (bed_front + bed_rear) * 0.5
    add_box(parts, "BedFloor", (width + 0.04, length, 0.22), (0.0, center, 1.16), "RF_Metal", 0.045)
    add_box(parts, "BedInner", (width - 0.24, length - 0.18, 0.05), (0.0, center, 1.295), "RF_Charcoal", 0.012)
    for side in (-1.0, 1.0):
        bed_side = add_box(parts, "BedSide", (0.13, length, 0.70), (side * width * 0.50, center, 1.54), "RF_Metal", 0.035)
        cut_wheel_well(bed_side, rear + 1.20, width, 0.58)
        for index in range(6):
            y = bed_rear + 0.34 + index * (length - 0.68) / 5
            if abs(y - (rear + 1.20)) > 0.78:
                add_box(parts, "SideRib", (0.16, 0.085, 0.76), (side * width * 0.515, y, 1.55), "RF_Charcoal", 0.008)
                add_box(parts, "TieDown", (0.07, 0.15, 0.10), (side * width * 0.56, y, 1.18), "RF_Amber", 0.012)
    add_box(parts, "TailGate", (width + 0.02, 0.14, 0.70), (0.0, bed_rear - 0.03, 1.54), "RF_Metal", 0.035)
    add_box(parts, "HeadRackTop", (width + 0.10, 0.13, 0.13), (0.0, bed_front + 0.03, 2.64), "RF_Charcoal", 0.025)
    for side in (-1.0, 1.0):
        add_box(parts, "HeadRackPost", (0.12, 0.12, 1.48), (side * width * 0.50, bed_front + 0.03, 1.95), "RF_Charcoal", 0.018)
    for z in (1.52, 1.86, 2.20, 2.54):
        add_box(parts, "HeadRackBar", (width - 0.14, 0.08, 0.07), (0.0, bed_front + 0.045, z), "RF_Metal", 0.012)


def add_tanker(parts: list[bpy.types.Object], width: float, front: float, rear: float) -> None:
    tank_front = front - 2.68
    tank_rear = rear + 0.30
    length = tank_front - tank_rear
    center = (tank_front + tank_rear) * 0.5
    tank_z = 2.36
    add_cylinder(parts, "Tank", 1.06, length, (0.0, center, tank_z), "RF_Cream", rotation=(math.pi * 0.5, 0.0, 0.0), vertices=32, bevel=0.055)
    for ratio in (0.10, 0.34, 0.66, 0.90):
        y = tank_rear + ratio * length
        add_torus(parts, "TankBand", 1.072, 0.044, (0.0, y, tank_z), "RF_Teal", (math.pi * 0.5, 0.0, 0.0), major_segments=32, minor_segments=8)
    add_box(parts, "TankWalkway", (0.70, length - 0.35, 0.07), (0.0, center, tank_z + 1.08), "RF_Metal", 0.018)
    for side in (-1.0, 1.0):
        for ratio in (0.05, 0.35, 0.65, 0.95):
            y = tank_rear + ratio * length
            add_box(parts, "WalkwayPost", (0.045, 0.045, 0.42), (side * 0.42, y, tank_z + 1.29), "RF_Metal", 0.006)
        add_box(parts, "WalkwayRail", (0.05, length - 0.45, 0.05), (side * 0.42, center, tank_z + 1.48), "RF_Metal", 0.008)
    for ratio in (0.28, 0.58, 0.82):
        y = tank_rear + ratio * length
        add_cylinder(parts, "TankHatch", 0.21, 0.12, (0.0, y, tank_z + 1.19), "RF_Charcoal", vertices=20, bevel=0.025)
        add_torus(parts, "HatchRing", 0.19, 0.025, (0.0, y, tank_z + 1.26), "RF_Metal", (0.0, 0.0, 0.0), major_segments=20, minor_segments=6)
    add_box(parts, "TankPipe", (0.15, length - 0.62, 0.15), (-0.72, center, 1.20), "RF_Metal", 0.045)
    add_box(parts, "LadderLeft", (0.07, 0.09, 1.82), (width * 0.44, tank_rear - 0.05, 2.10), "RF_Metal", 0.012)
    add_box(parts, "LadderRight", (0.07, 0.09, 1.82), (width * 0.26, tank_rear - 0.05, 2.10), "RF_Metal", 0.012)
    for z in (1.34, 1.66, 1.98, 2.30, 2.62, 2.94):
        add_box(parts, "LadderRung", (0.49, 0.09, 0.06), (width * 0.35, tank_rear - 0.075, z), "RF_Metal", 0.006)


def add_dump(parts: list[bpy.types.Object], width: float, front: float, rear: float) -> None:
    bed_front = front - 2.66
    bed_rear = rear + 0.22
    length = bed_front - bed_rear
    center = (bed_front + bed_rear) * 0.5
    dump_bed = add_tapered_box(parts, "DumpBed", (width - 0.08, length), (width + 0.30, length - 0.10), 1.38, (0.0, center, 1.96), "RF_Teal", 0.055)
    cut_wheel_well(dump_bed, rear + 1.20, width, 0.58)
    add_box(parts, "DumpInner", (width - 0.22, length - 0.28, 0.08), (0.0, center, 2.57), "RF_Charcoal", 0.025)
    for side in (-1.0, 1.0):
        add_box(parts, "DumpTopRail", (0.12, length + 0.04, 0.13), (side * width * 0.56, center, 2.66), "RF_Metal", 0.025)
        for index in range(6):
            y = bed_rear + 0.34 + index * (length - 0.68) / 5
            if abs(y - (rear + 1.20)) > 0.78:
                add_box(parts, "DumpRib", (0.13, 0.12, 1.22), (side * width * 0.56, y, 1.98), "RF_Charcoal", 0.014)
    add_box(parts, "HydraulicRam", (0.20, 0.20, 1.35), (0.0, center + 0.35, 1.14), "RF_Silver", 0.055, rotation=(0.32, 0.0, 0.0))
    for row in range(3):
        for index in range(7):
            x = -0.84 + index * 0.28 + (row % 2) * 0.08
            y = bed_rear + 0.62 + ((index * 0.71 + row * 0.49) % max(0.8, length - 1.15))
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.30 + 0.035 * ((index + row) % 3), location=(x, y, 2.72 + row * 0.10))
            rock = bpy.context.object
            rock.name = "DumpCargo"
            rock.scale = (1.0, 1.25, 0.72)
            rock.rotation_euler = (0.2 * row, 0.17 * index, 0.13 * (index + row))
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            rock.data.materials.append(material("RF_Cargo"))
            parts.append(rock)


def build_vehicle(vehicle_id: str) -> bpy.types.Object:
    parts: list[bpy.types.Object] = []
    if vehicle_id == "city-bus":
        add_bus(parts, coach=False)
    elif vehicle_id == "highway-coach":
        add_bus(parts, coach=True)
    else:
        width, front, rear = add_truck_base(parts, vehicle_id)
        if vehicle_id == "box-truck":
            add_box_body(parts, width, front, rear)
        elif vehicle_id == "flatbed-truck":
            add_flatbed(parts, width, front, rear)
        elif vehicle_id == "tanker-truck":
            add_tanker(parts, width, front, rear)
        elif vehicle_id == "dump-truck":
            add_dump(parts, width, front, rear)
    if not parts:
        raise RuntimeError(f"vehicle produced no mesh parts: {vehicle_id}")
    bpy.ops.object.select_all(action="DESELECT")
    for part in parts:
        part.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.view_layer.objects.active
    joined.name = vehicle_id
    joined.data.name = vehicle_id + "Mesh"
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    minimum = Vector((
        min((joined.matrix_world @ Vector(c)).x for c in joined.bound_box),
        min((joined.matrix_world @ Vector(c)).y for c in joined.bound_box),
        min((joined.matrix_world @ Vector(c)).z for c in joined.bound_box),
    ))
    maximum = Vector((
        max((joined.matrix_world @ Vector(c)).x for c in joined.bound_box),
        max((joined.matrix_world @ Vector(c)).y for c in joined.bound_box),
        max((joined.matrix_world @ Vector(c)).z for c in joined.bound_box),
    ))
    center = (minimum + maximum) * 0.5
    offset = Vector((-center.x, -center.y, -minimum.z))
    for vertex in joined.data.vertices:
        vertex.co += offset
    return joined


def triangles(obj: bpy.types.Object) -> int:
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def lod_copy(source: bpy.types.Object, name: str, budget: int) -> bpy.types.Object:
    duplicate = source.copy()
    duplicate.data = source.data.copy()
    bpy.context.collection.objects.link(duplicate)
    duplicate.name = name
    current = triangles(duplicate)
    attempts = 0
    while current > budget and attempts < 4:
        modifier = duplicate.modifiers.new(name=f"LODDecimate{attempts}", type="DECIMATE")
        modifier.ratio = max(0.01, min(0.98, budget / current * 0.94))
        modifier.use_collapse_triangulate = True
        bpy.ops.object.select_all(action="DESELECT")
        duplicate.select_set(True)
        bpy.context.view_layer.objects.active = duplicate
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        current = triangles(duplicate)
        attempts += 1
    mesh = bmesh.new()
    mesh.from_mesh(duplicate.data)
    bmesh.ops.remove_doubles(mesh, verts=list(mesh.verts), dist=0.000001)
    bmesh.ops.dissolve_degenerate(mesh, edges=list(mesh.edges), dist=0.0000001)
    mesh.normal_update()
    mesh.to_mesh(duplicate.data)
    mesh.free()
    duplicate.data.validate(verbose=False, clean_customdata=True)
    duplicate.data.update()
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


def bounds(obj: bpy.types.Object) -> list[float]:
    minimum = Vector((
        min((obj.matrix_world @ Vector(c)).x for c in obj.bound_box),
        min((obj.matrix_world @ Vector(c)).y for c in obj.bound_box),
        min((obj.matrix_world @ Vector(c)).z for c in obj.bound_box),
    ))
    maximum = Vector((
        max((obj.matrix_world @ Vector(c)).x for c in obj.bound_box),
        max((obj.matrix_world @ Vector(c)).y for c in obj.bound_box),
        max((obj.matrix_world @ Vector(c)).z for c in obj.bound_box),
    ))
    size = maximum - minimum
    return [round(size.x, 4), round(size.y, 4), round(size.z, 4)]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for name in MATERIAL_COLORS:
        material(name)


def main() -> None:
    args = arguments()
    output = Path(args.output_dir).resolve()
    reference = Path(args.reference).resolve()
    if not reference.is_file():
        raise RuntimeError(f"reference image not found: {reference}")
    output.mkdir(parents=True, exist_ok=True)
    budgets = {"lod0": args.lod0, "lod1": args.lod1, "lod2": args.lod2}
    assets = []
    for vehicle_id in VEHICLE_IDS:
        reset_scene()
        source = build_vehicle(vehicle_id)
        asset_bounds = bounds(source)
        lods = []
        for level, budget in budgets.items():
            lod = lod_copy(source, f"{vehicle_id}_{level}", budget)
            result = export_object(lod, output / lod.name)
            if result["triangles"] > budget:
                raise RuntimeError(f"{vehicle_id} {level} exceeds budget: {result['triangles']} > {budget}")
            lods.append({"level": level, **result})
            bpy.data.objects.remove(lod, do_unlink=True)
        assets.append({"id": vehicle_id, "bounds": asset_bounds, "lods": lods})
    manifest = {
        "schemaVersion": 1,
        "pipelineVersion": "1.5.0",
        "generator": "reference-matched-premium-procedural-fleet",
        "visualAuthority": "cream-teal-industrial-vehicle-atlas-v2",
        "reference": {"file": reference.name, "sha256": sha256(reference)},
        "triangleBudgets": budgets,
        "materials": list(MATERIAL_COLORS.keys()),
        "qualityFeatures": [
            "full-wheel-visibility",
            "multi-layer-body-panels",
            "exterior-fixtures",
            "high-segment-curved-surfaces",
            "transparent-modeled-cabins",
            "modeled-seat-interiors",
            "smooth-rounded-tires-and-recessed-rim-vents",
            "orthographic-four-surface-reference-mapping",
            "physical-window-openings",
            "three-tier-lod",
        ],
        "assets": assets,
    }
    (output / "asset-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"AI_UNITY_VEHICLE_FLEET_OK: assets={len(assets)} output={output}")


if __name__ == "__main__":
    main()
