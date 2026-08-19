from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .errors import ProcessingError
from .fusion import FusionOptions, fuse_capture
from .geometry import blender_alignment, merged_point_cloud, scale_scene
from .gltf import write_glb, write_ply
from .io import load_capture, validate_capture
from .model import Capture, FrameQuality, FrameRecord, ValidationReport
from .pose import PoseRefinement
from .quality import score_frames, select_keyframes
from .report import write_quality_report


ProgressCallback = Callable[[str, float], None]


@dataclass
class ProcessOptions:
    output_dir: Path
    confidence_threshold: int = 96
    depth_step: int = 4
    clean_voxel_m: float = 0.025
    max_frames: int = 600
    max_depth_m: float = 12.0
    tsdf_voxel_m: float = 0.025
    tsdf_trunc_m: float = 0.10
    refine_poses: bool = True
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


def process_capture(path: str | Path, options: ProcessOptions, *, progress: ProgressCallback | None = None) -> ProcessResult:
    _report_progress(progress, "撮影データを読み込んでいます", 0.02)
    output_dir = Path(options.output_dir).expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not options.force:
        raise ProcessingError(f"output directory is not empty: {output_dir} (use --force to replace generated files)")
    output_dir.mkdir(parents=True, exist_ok=True)
    if options.force:
        _remove_previous_generated_outputs(output_dir)
    with load_capture(path) as capture:
        _report_progress(progress, "撮影データを検証しています", 0.10)
        validation = validate_capture(capture, verify_checksums=options.verify_checksums, inspect_images=True)
        if validation.errors:
            messages = "; ".join(issue.message for issue in validation.errors[:5])
            raise ProcessingError(f"capture validation failed: {messages}")
        if validation.depth_frames == 0:
            raise ProcessingError("no depth frames are available; RoomTrace needs Raw Depth or an imported MVS depth source to produce a mesh")
        _report_progress(progress, "画像品質を確認しています", 0.18)
        qualities = score_frames(capture)
        selected = select_keyframes(capture.frames, qualities, max_frames=options.max_frames)
        if len(selected) < 2:
            raise ProcessingError("fewer than two usable keyframes remain after quality filtering")
        _report_progress(progress, f"使用フレームを選んでいます（{len(selected)}枚）", 0.24)
        # The former positional end-point correction was only meaningful for
        # independent frame meshes.  TSDF must use each depth camera pose as
        # recorded; altering only translations can make a stable scan worse.
        fusion = fuse_capture(
            capture,
            selected,
            FusionOptions(
                voxel_size_m=options.tsdf_voxel_m,
                sdf_trunc_m=options.tsdf_trunc_m,
                confidence_threshold=options.confidence_threshold,
                max_depth_m=options.max_depth_m,
                clean_voxel_m=options.clean_voxel_m,
                refine_poses=options.refine_poses,
            ),
            progress=progress,
        )
        pose_refinement = PoseRefinement(
            fusion.icp_refined_frames > 0,
            "open3d_local_icp" if fusion.icp_refined_frames else "webxr_depth_pose",
            note=(
                f"conservative local ICP accepted {fusion.icp_refined_frames} pose corrections"
                if fusion.icp_refined_frames
                else "TSDF used calibrated depth poses without an ICP correction"
            ),
        )
        _report_progress(progress, "座標系と床面を整えています", 0.88)
        aligned_scene = blender_alignment([fusion.raw_mesh, fusion.clean_mesh])
        aligned_scene, scale_factor = scale_scene(
            aligned_scene,
            reference_width_m=options.reference_width_m,
            reference_depth_m=options.reference_depth_m,
        )
        raw_mesh, clean_mesh = aligned_scene.meshes
        _report_progress(progress, "融合済みメッシュを書き出しています", 0.91)
        raw_glb = write_glb(
            output_dir / "room_reference_raw.glb",
            [raw_mesh],
            name="RoomTrace TSDF Reference",
            extras={"roomtrace": {"capture_id": capture.capture_id, "coordinate_system": "blender_z_up_meters", "floor_height_world": aligned_scene.floor_height_world, "reconstruction": fusion.method}},
        )
        _report_progress(progress, "Clean GLBを書き出しています", 0.97)
        clean_glb = write_glb(
            output_dir / "room_reference_clean.glb",
            [clean_mesh],
            name="RoomTrace Clean Reference",
            extras={"roomtrace": {"capture_id": capture.capture_id, "coordinate_system": "blender_z_up_meters", "voxel_size_m": options.clean_voxel_m, "reconstruction": fusion.method}},
        )
        point_positions, point_colors = merged_point_cloud([clean_mesh])
        pointcloud = write_ply(output_dir / "pointcloud.ply", point_positions, point_colors)
        frame_errors_path = output_dir / "frame_errors.json"
        frame_errors_path.write_text(json.dumps(fusion.frame_errors, indent=2), encoding="utf-8")
        cameras_path = _write_cameras(output_dir / "cameras.json", capture, fusion.integrated_frames, aligned_scene.transform, qualities)
        measurements_path = _write_measurements(output_dir / "measurements.csv", aligned_scene.bounds, clean_mesh)
        cache_dir = output_dir / "cache"
        cache_dir.mkdir(exist_ok=True)
        (cache_dir / "selection.json").write_text(
            json.dumps({"selected_frame_ids": [frame.frame_id for frame in selected], "integrated_frame_ids": [frame.frame_id for frame in fusion.integrated_frames], "reconstruction": fusion.method, "pose_refinement": asdict(pose_refinement)}, indent=2),
            encoding="utf-8",
        )
        summary = {
            "selected_frames": len(selected),
            "mesh_frames": len(fusion.integrated_frames),
            "raw_vertices": int(len(raw_mesh.positions)),
            "raw_triangles": int(len(raw_mesh.indices)),
            "clean_vertices": int(len(clean_mesh.positions)),
            "clean_triangles": int(len(clean_mesh.indices)),
            "texture_atlases": 0,
            "reconstruction": fusion.method,
            "tsdf_voxel_m": options.tsdf_voxel_m,
            "tsdf_trunc_m": options.tsdf_trunc_m,
            "depth_frames": validation.depth_frames,
            "pose_refinement": pose_refinement.method,
            "loop_correction_m": round(pose_refinement.translation_correction_m, 5),
            "icp_refined_frames": fusion.icp_refined_frames,
            "scale_factor": round(scale_factor, 6),
            "floor_height_capture_m": round(aligned_scene.floor_height_world, 5),
            "bounds_min_m": aligned_scene.bounds["min"],
            "bounds_max_m": aligned_scene.bounds["max"],
            "frame_errors": len(fusion.frame_errors),
            "frame_errors_file": str(frame_errors_path.name),
            "pointcloud": str(pointcloud.name),
            "cameras": str(cameras_path.name),
            "measurements": str(measurements_path.name),
        }
        _report_progress(progress, "品質レポートを作成しています", 0.99)
        report_html, report_json = write_quality_report(
            output_dir,
            capture_label=capture.source_label,
            validation=validation,
            frame_qualities=qualities,
            selected_ids=[frame.frame_id for frame in selected],
            summary=summary,
        )
        (output_dir / "processing_manifest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        _report_progress(progress, "完了", 1.0)
        return ProcessResult(output_dir, raw_glb, clean_glb, report_html, report_json, summary)


def _report_progress(progress: ProgressCallback | None, message: str, fraction: float) -> None:
    if progress:
        progress(message, max(0.0, min(1.0, fraction)))


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
