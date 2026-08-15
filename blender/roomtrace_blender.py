"""Blender helper for RoomTrace outputs.

Run from Blender's Scripting workspace or Blender's Python console:

    exec(compile(open(r"C:/path/to/roomtrace_blender.py", "rb").read(), "roomtrace_blender.py", "exec"))
    import_roomtrace(r"C:/path/to/room-output")

The GLB remains the source of truth. This helper only organizes the imported
reference collection, sets metric units, and adds camera markers from
``cameras.json`` when it is available.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def import_roomtrace(output_dir: str, *, use_clean: bool = True, hide_select: bool = True) -> dict[str, Any]:
    try:
        import bpy
        from mathutils import Matrix
    except ImportError as exc:  # pragma: no cover - only executed outside Blender
        raise RuntimeError("run this helper inside Blender") from exc

    root = Path(output_dir).expanduser().resolve()
    glb = root / ("room_reference_clean.glb" if use_clean else "room_reference_raw.glb")
    if not glb.exists():
        raise FileNotFoundError(glb)
    collection_name = "ROOMTRACE_REFERENCE_CLEAN" if use_clean else "ROOMTRACE_REFERENCE_RAW"
    collection = bpy.data.collections.get(collection_name)
    if collection is None:
        collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(collection)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(glb))
    imported = [obj for obj in bpy.data.objects if obj not in before]
    for obj in imported:
        for old_collection in list(obj.users_collection):
            old_collection.objects.unlink(obj)
        collection.objects.link(obj)
        if hide_select:
            obj.hide_select = True
    collection.hide_viewport = False
    collection.hide_render = True
    for obj in imported:
        if hasattr(obj, "color"):
            obj.color = (1.0, 1.0, 1.0, 0.62 if use_clean else 0.42)
    bpy.context.scene.unit_settings.system = "METRIC"
    bpy.context.scene.unit_settings.length_unit = "METERS"
    bpy.context.scene.unit_settings.scale_length = 1.0
    _add_measurement_text(root, bpy, collection)
    _add_camera_markers(root, bpy, Matrix)
    return {"collection": collection_name, "objects": len(imported), "file": str(glb)}


def _add_measurement_text(root: Path, bpy: Any, collection: Any) -> None:
    path = root / "measurements.csv"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    name = "ROOMTRACE_MEASUREMENTS"
    existing = bpy.data.objects.get(name)
    if existing:
        bpy.data.objects.remove(existing, do_unlink=True)
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = "RoomTrace measurements\n" + "\n".join(text.splitlines()[:12])
    curve.size = 0.08
    obj = bpy.data.objects.new(name, curve)
    collection.objects.link(obj)
    obj.location = (0.0, 0.0, 0.02)


def _add_camera_markers(root: Path, bpy: Any, Matrix: Any) -> None:
    path = root / "cameras.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    collection = bpy.data.collections.get("ROOMTRACE_CAMERAS")
    if collection is None:
        collection = bpy.data.collections.new("ROOMTRACE_CAMERAS")
        bpy.context.scene.collection.children.link(collection)
    for item in list(collection.objects):
        bpy.data.objects.remove(item, do_unlink=True)
    for camera_data in data.get("cameras", []):
        camera = bpy.data.cameras.new(f"RoomTraceCamera_{camera_data['frame_id']:08d}")
        camera.type = "PERSP"
        camera.lens = 35.0
        obj = bpy.data.objects.new(camera.name, camera)
        collection.objects.link(obj)
        values = camera_data.get("pose_c2w", [])
        if len(values) == 16:
            obj.matrix_world = Matrix((tuple(values[0:4]), tuple(values[4:8]), tuple(values[8:12]), tuple(values[12:16])))
        obj.hide_render = True


def _run_from_blender_command_line() -> int:
    """Import a result directory when Blender is launched by the RoomTrace GUI."""
    try:
        separator = sys.argv.index("--")
    except ValueError:
        separator = len(sys.argv)
    args = sys.argv[separator + 1 :]
    if not args:
        print("RoomTrace: pass an output directory after --")
        return 2
    output_dir = args[0]
    results = [import_roomtrace(output_dir, use_clean=True)]
    if "--raw" in args:
        results.append(import_roomtrace(output_dir, use_clean=False))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - executed by Blender
    _run_from_blender_command_line()
