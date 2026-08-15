from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np

from .atlas import apply_atlas_uv, build_atlases
from .errors import ProcessingError
from .geometry import blender_alignment, depth_mesh, merged_point_cloud, scale_scene, voxel_reduce
from .gltf import GlbTexture, write_glb, write_ply
from .io import load_capture, validate_capture
from .model import Capture, FrameQuality, FrameRecord, MeshData, ValidationReport
from .pose import apply_loop_closure
from .quality import read_confidence, read_depth, read_rgb, score_frames, select_keyframes
from .report import write_quality_report


@dataclass
class ProcessOptions:
    output_dir: Path
    confidence_threshold: int = 96
    depth_step: int = 4
    clean_voxel_m: float = 0.025
    max_frames: int = 600
    max_depth_m: float = 12.0
    reference_width_m: float | None = None
    reference_depth_m: float | None = None
    loop_closure: bool = False
    verify_checksums: bool = False
    force: bool = False


@dataclass
class ProcessResult:
    output_dir: Path
    raw_glb: Path
    clean_glb: Path
    report_html: Path
    report_json: Path
    summary: dict[str, Any]


def inspect_capture(path: str | Path, *, verify_checksums: bool = False, inspect_images: bool = True) -> tuple[Capture, ValidationReport]:
    capture = load_capture(path)
    report = validate_capture(capture, verify_checksums=verify_checksums, inspect_images=inspect_images)
    return capture, report


def process_capture(path: str | Path, options: ProcessOptions) -> ProcessResult:
    output_dir = Path(options.output_dir).expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not options.force:
        raise ProcessingError(f"output directory is not empty: {output_dir} (use --force to replace generated files)")
    output_dir.mkdir(parents=True, exist_ok=True)
    if options.force:
        _remove_previous_generated_outputs(output_dir)
    with load_capture(path) as capture:
        validation = validate_capture(capture, verify_checksums=options.verify_checksums, inspect_images=True)
        if validation.errors:
            messages = "; ".join(issue.message for issue in validation.errors[:5])
            raise ProcessingError(f"capture validation failed: {messages}")
        if validation.depth_frames == 0:
            raise ProcessingError("no depth frames are available; RoomTrace needs Raw Depth or an imported MVS depth source to produce a mesh")
        qualities = score_frames(capture)
        selected = select_keyframes(capture.frames, qualities, max_frames=options.max_frames)
        if len(selected) < 2:
            raise ProcessingError("fewer than two usable keyframes remain after quality filtering")
        pose_refinement = apply_loop_closure(selected, enabled=options.loop_closure)
        world_meshes, mesh_frames, frame_errors = _build_meshes(capture, selected, options)
        if not world_meshes:
            raise ProcessingError("no valid triangle mesh could be built from the depth frames")
        aligned_scene = blender_alignment(world_meshes)
        aligned_scene, scale_factor = scale_scene(
            aligned_scene,
            reference_width_m=options.reference_width_m,
            reference_depth_m=options.reference_depth_m,
        )
        aligned_meshes = aligned_scene.meshes
        raw_frame_list = [frame for frame, mesh in zip(mesh_frames, aligned_meshes) if len(mesh.indices)]
        atlas = build_atlases(capture, raw_frame_list, output_dir / "textures")
        raw_meshes: list[MeshData] = []
        for frame, mesh in zip(raw_frame_list, aligned_meshes):
            tile = atlas.tiles.get(frame.frame_id)
            raw_meshes.append(apply_atlas_uv(mesh, tile) if tile else MeshData(mesh.positions, mesh.indices, None, mesh.colors, mesh.frame_id))
        textures = [GlbTexture(name=name, data=data, mime_type=atlas.mime_type) for name, data in zip(atlas.names, atlas.images)]
        raw_glb = write_glb(
            output_dir / "room_reference_raw.glb",
            raw_meshes,
            textures=textures,
            name="RoomTrace Raw Reference",
            extras={"roomtrace": {"capture_id": capture.capture_id, "coordinate_system": "blender_z_up_meters", "floor_height_world": aligned_scene.floor_height_world}},
        )
        clean_mesh = voxel_reduce(aligned_meshes, options.clean_voxel_m)
        if len(clean_mesh.indices) == 0:
            fallback = aligned_meshes[0]
            clean_mesh = MeshData(
                fallback.positions.copy(),
                fallback.indices.copy(),
                colors=fallback.colors.copy() if fallback.colors is not None else None,
            )
        clean_glb = write_glb(
            output_dir / "room_reference_clean.glb",
            [clean_mesh],
            name="RoomTrace Clean Reference",
            extras={"roomtrace": {"capture_id": capture.capture_id, "coordinate_system": "blender_z_up_meters", "voxel_size_m": options.clean_voxel_m}},
        )
        point_positions, point_colors = merged_point_cloud([clean_mesh])
        pointcloud = write_ply(output_dir / "pointcloud.ply", point_positions, point_colors)
        frame_errors_path = output_dir / "frame_errors.json"
        frame_errors_path.write_text(json.dumps(frame_errors, indent=2), encoding="utf-8")
        cameras_path = _write_cameras(output_dir / "cameras.json", capture, selected, aligned_scene.transform, qualities)
        measurements_path = _write_measurements(output_dir / "measurements.csv", aligned_scene.bounds, clean_mesh)
        cache_dir = output_dir / "cache"
        cache_dir.mkdir(exist_ok=True)
        (cache_dir / "selection.json").write_text(
            json.dumps({"selected_frame_ids": [frame.frame_id for frame in selected], "pose_refinement": asdict(pose_refinement)}, indent=2),
            encoding="utf-8",
        )
        summary = {
            "selected_frames": len(selected),
            "mesh_frames": len(raw_meshes),
            "raw_vertices": int(sum(len(mesh.positions) for mesh in raw_meshes)),
            "raw_triangles": int(sum(len(mesh.indices) for mesh in raw_meshes)),
            "clean_vertices": int(len(clean_mesh.positions)),
            "clean_triangles": int(len(clean_mesh.indices)),
            "texture_atlases": len(textures),
            "depth_frames": validation.depth_frames,
            "pose_refinement": pose_refinement.method,
            "loop_correction_m": round(pose_refinement.translation_correction_m, 5),
            "scale_factor": round(scale_factor, 6),
            "floor_height_capture_m": round(aligned_scene.floor_height_world, 5),
            "bounds_min_m": aligned_scene.bounds["min"],
            "bounds_max_m": aligned_scene.bounds["max"],
            "frame_errors": len(frame_errors),
            "frame_errors_file": str(frame_errors_path.name),
            "pointcloud": str(pointcloud.name),
            "cameras": str(cameras_path.name),
            "measurements": str(measurements_path.name),
        }
        report_html, report_json = write_quality_report(
            output_dir,
            capture_label=capture.source_label,
            validation=validation,
            frame_qualities=qualities,
            selected_ids=[frame.frame_id for frame in selected],
            summary=summary,
        )
        (output_dir / "processing_manifest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return ProcessResult(output_dir, raw_glb, clean_glb, report_html, report_json, summary)


def _remove_previous_generated_outputs(output_dir: Path) -> None:
    """Remove only files owned by RoomTrace when --force is explicit."""
    generated_names = {
        "room_reference_raw.glb",
        "room_reference_clean.glb",
        "pointcloud.ply",
        "cameras.json",
        "measurements.csv",
        "quality_report.html",
        "quality_report.json",
        "processing_manifest.json",
        "frame_errors.json",
    }
    for name in generated_names:
        path = output_dir / name
        if path.is_file():
            path.unlink()
    texture_dir = output_dir / "textures"
    if texture_dir.is_dir():
        for path in texture_dir.iterdir():
            if path.is_file() and path.name.startswith("texture_atlas_") and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                path.unlink()
    selection = output_dir / "cache" / "selection.json"
    if selection.is_file():
        selection.unlink()


def _build_meshes(capture: Capture, frames: list[FrameRecord], options: ProcessOptions) -> tuple[list[MeshData], list[FrameRecord], list[dict[str, Any]]]:
    meshes: list[MeshData] = []
    mesh_frames: list[FrameRecord] = []
    errors: list[dict[str, Any]] = []
    for frame in frames:
        if not frame.depth_path or not capture.exists(frame.depth_path):
            errors.append({"frame_id": frame.frame_id, "error": "depth_missing"})
            continue
        try:
            rgb = read_rgb(capture, frame)
            depth = read_depth(capture, frame)
            confidence = read_confidence(capture, frame, depth.shape)
            mesh = depth_mesh(
                depth,
                rgb,
                confidence,
                capture.intrinsics,
                frame.pose_c2w,
                frame_id=frame.frame_id,
                sample_step=options.depth_step,
                confidence_threshold=options.confidence_threshold,
                max_depth_m=options.max_depth_m,
            )
            if mesh is None:
                errors.append({"frame_id": frame.frame_id, "error": "no_valid_triangles"})
                continue
            meshes.append(mesh)
            mesh_frames.append(frame)
        except Exception as exc:
            errors.append({"frame_id": frame.frame_id, "error": str(exc)})
    return meshes, mesh_frames, errors


def _write_cameras(path: Path, capture: Capture, frames: list[FrameRecord], transform: np.ndarray, qualities: dict[int, FrameQuality]) -> Path:
    value = {
        "coordinate_system": "blender_z_up_meters",
        "transform_from_capture": transform.reshape(-1).astype(float).tolist(),
        "cameras": [],
    }
    for frame in frames:
        pose = transform @ frame.pose_c2w
        quality = qualities.get(frame.frame_id)
        value["cameras"].append({
            "frame_id": frame.frame_id,
            "timestamp_ns": frame.timestamp_ns,
            "image": frame.image_path,
            "pose_c2w": pose.reshape(-1).astype(float).tolist(),
            "quality": quality.as_json() if quality else {},
        })
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def _write_measurements(path: Path, bounds: dict[str, list[float]], mesh: MeshData) -> Path:
    minimum = np.asarray(bounds["min"], dtype=float)
    maximum = np.asarray(bounds["max"], dtype=float)
    rows = [
        ("bounds_min_x_m", minimum[0]), ("bounds_min_y_m", minimum[1]), ("bounds_min_z_m", minimum[2]),
        ("bounds_max_x_m", maximum[0]), ("bounds_max_y_m", maximum[1]), ("bounds_max_z_m", maximum[2]),
        ("width_x_m", maximum[0] - minimum[0]), ("depth_y_m", maximum[1] - minimum[1]), ("height_z_m", maximum[2] - minimum[2]),
        ("vertex_count", len(mesh.positions)), ("triangle_count", len(mesh.indices)),
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["measurement", "value"])
        writer.writerows(rows)
    return path
