"""Open3D based, pose-aware TSDF reconstruction for RoomTrace captures.

This module deliberately keeps all work on the local computer.  It turns each
captured depth image into the calibrated virtual camera image expected by
Open3D, then integrates the images into one signed-distance volume.  This is
fundamentally different from placing one triangle grid per frame: overlapping
observations reinforce a surface while incompatible observations are rejected
by the truncation band.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .errors import ProcessingError
from .model import Capture, FrameRecord, Intrinsics, MeshData
from .quality import read_confidence, read_depth, read_rgb


ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class FusionOptions:
    voxel_size_m: float = 0.025
    sdf_trunc_m: float = 0.10
    confidence_threshold: int = 96
    max_depth_m: float = 8.0
    clean_voxel_m: float = 0.04
    refine_poses: bool = True


@dataclass(frozen=True)
class FusionResult:
    raw_mesh: MeshData
    clean_mesh: MeshData
    integrated_frames: list[FrameRecord]
    frame_errors: list[dict[str, str | int]]
    icp_refined_frames: int = 0
    method: str = "open3d_scalable_tsdf"


def fuse_capture(
    capture: Capture,
    frames: list[FrameRecord],
    options: FusionOptions,
    *,
    progress: ProgressCallback | None = None,
) -> FusionResult:
    """Fuse calibrated frames into a single mesh using Open3D ScalableTSDF."""
    try:
        import open3d as o3d
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise ProcessingError(
            "Open3D is required for reconstruction. Re-run Setup-RoomTrace.ps1 "
            "or install the processor package with its Open3D dependency."
        ) from exc

    browser_capture = capture.manifest.get("device", {}).get("source") == "browser-spa"
    registered_rgb = bool(capture.capabilities.get("rgb_registered_to_depth", not browser_capture))
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=max(0.005, float(options.voxel_size_m)),
        sdf_trunc=max(float(options.sdf_trunc_m), float(options.voxel_size_m) * 2.0),
        color_type=(
            o3d.pipelines.integration.TSDFVolumeColorType.RGB8
            if registered_rgb
            else o3d.pipelines.integration.TSDFVolumeColorType.NoColor
        ),
    )

    integrated: list[FrameRecord] = []
    errors: list[dict[str, str | int]] = []
    previous_cloud = None
    previous_pose = None
    icp_refined_frames = 0
    total = max(1, len(frames))
    for index, frame in enumerate(frames, start=1):
        if not frame.depth_path or not capture.exists(frame.depth_path):
            errors.append({"frame_id": frame.frame_id, "error": "depth_missing"})
            continue
        try:
            depth_mm = read_depth(capture, frame)
            confidence = read_confidence(capture, frame, depth_mm.shape)
            depth_image, intrinsic, camera_to_world = _open3d_frame(
                depth_mm,
                confidence,
                capture.intrinsics,
                frame,
                confidence_threshold=options.confidence_threshold,
                max_depth_m=options.max_depth_m,
                o3d=o3d,
            )
            if depth_image is None:
                errors.append({"frame_id": frame.frame_id, "error": "no_valid_depth"})
                continue
            if options.refine_poses and previous_cloud is not None and previous_pose is not None:
                cloud = o3d.geometry.PointCloud.create_from_depth_image(
                    o3d.geometry.Image(depth_image),
                    intrinsic,
                    depth_scale=1000.0,
                    depth_trunc=float(options.max_depth_m),
                    stride=2,
                ).voxel_down_sample(max(0.025, float(options.voxel_size_m) * 2.0))
                refined = _refine_adjacent_pose(o3d, cloud, previous_cloud, camera_to_world, previous_pose)
                if refined is not None:
                    camera_to_world = refined
                    icp_refined_frames += 1
            else:
                cloud = o3d.geometry.PointCloud.create_from_depth_image(
                    o3d.geometry.Image(depth_image),
                    intrinsic,
                    depth_scale=1000.0,
                    depth_trunc=float(options.max_depth_m),
                    stride=2,
                ).voxel_down_sample(max(0.025, float(options.voxel_size_m) * 2.0))
            if registered_rgb:
                rgb = _registered_color(read_rgb(capture, frame), depth_image.shape)
                color_image = o3d.geometry.Image(rgb)
            else:
                # WebXR's getUserMedia image has no calibrated relation to its
                # depth sensor.  Supplying it would paint the mesh incorrectly.
                color_image = o3d.geometry.Image(np.zeros((*depth_image.shape, 3), dtype=np.uint8))
            rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                color_image,
                o3d.geometry.Image(depth_image),
                depth_scale=1000.0,
                depth_trunc=float(options.max_depth_m),
                convert_rgb_to_intensity=False,
            )
            volume.integrate(rgbd, intrinsic, np.linalg.inv(camera_to_world))
            integrated.append(frame)
            previous_cloud = cloud
            previous_pose = camera_to_world
        except Exception as exc:
            errors.append({"frame_id": frame.frame_id, "error": str(exc)})
        _progress(progress, f"TSDFへ深度を融合しています（{index}/{len(frames)}）", 0.26 + 0.52 * index / total)

    if len(integrated) < 2:
        raise ProcessingError("Open3D TSDF integration needs at least two calibrated depth frames")
    _progress(progress, "TSDFから連続メッシュを抽出しています", 0.80)
    mesh = volume.extract_triangle_mesh()
    _clean_open3d_mesh(mesh)
    raw = _mesh_data(mesh, with_colors=registered_rgb)
    if len(raw.indices) == 0:
        raise ProcessingError("TSDF fusion produced no surfaces; move slowly and keep walls/floor in range")
    _progress(progress, "軽量版メッシュを最適化しています", 0.86)
    clean = _clean_mesh(mesh, clean_voxel_m=options.clean_voxel_m, with_colors=registered_rgb)
    if len(clean.indices) == 0:
        clean = raw
    return FusionResult(raw, clean, integrated, errors, icp_refined_frames)


def _open3d_frame(
    depth_mm: np.ndarray,
    confidence: np.ndarray,
    fallback_intrinsics: Intrinsics,
    frame: FrameRecord,
    *,
    confidence_threshold: int,
    max_depth_m: float,
    o3d: object,
) -> tuple[np.ndarray | None, object, np.ndarray]:
    depth = np.asarray(depth_mm, dtype=np.uint16)
    if depth.ndim != 2 or not depth.size:
        return None, None, np.eye(4)
    if confidence.shape != depth.shape:
        confidence = _resize_nearest(confidence, depth.shape)
    valid = (depth > 0) & (depth.astype(np.float32) <= max_depth_m * 1000.0) & (confidence >= confidence_threshold)
    if not np.any(valid):
        return None, None, np.eye(4)
    projection = _matrix4(frame.metadata.get("depth_projection_matrix"))
    view_from_depth = _inverse_matrix4(frame.metadata.get("norm_depth_buffer_from_norm_view"))
    pose = _matrix4(frame.metadata.get("depth_pose_c2w"))
    if pose is None:
        pose = frame.pose_c2w

    if projection is not None and view_from_depth is not None:
        rectified = _rectify_depth_to_view(depth, valid, view_from_depth)
        h, w = rectified.shape
        fx = abs(float(projection[0, 0])) * w * 0.5
        fy = abs(float(projection[1, 1])) * h * 0.5
        cx = (1.0 - float(projection[0, 2])) * w * 0.5
        cy = (1.0 + float(projection[1, 2])) * h * 0.5
    else:
        rectified = depth.copy()
        rectified[~valid] = 0
        h, w = rectified.shape
        intrinsics = fallback_intrinsics.scaled(w, h)
        fx, fy, cx, cy = intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy
    if fx <= 1e-5 or fy <= 1e-5 or not np.any(rectified):
        return None, None, np.eye(4)
    intrinsic = o3d.camera.PinholeCameraIntrinsic(w, h, float(fx), float(fy), float(cx), float(cy))
    # WebXR: +X right, +Y up, camera looks down -Z.
    # Open3D: +X right, +Y down, camera looks down +Z.
    open3d_to_webxr = np.diag([1.0, -1.0, -1.0, 1.0])
    return rectified, intrinsic, pose @ open3d_to_webxr


def _refine_adjacent_pose(o3d: object, source: object, target: object, source_pose: np.ndarray, target_pose: np.ndarray) -> np.ndarray | None:
    """Use conservative local ICP to correct small tracking drift only.

    The WebXR pose remains the prior.  Large corrections are deliberately
    rejected because a featureless wall can otherwise cause ICP to snap a room
    onto itself.
    """
    if len(source.points) < 80 or len(target.points) < 80:
        return None
    target.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.12, max_nn=30))
    initial = np.linalg.inv(target_pose) @ source_pose
    result = o3d.pipelines.registration.registration_icp(
        source,
        target,
        0.12,
        initial,
        o3d.pipelines.registration.TransformationEstimationPointToPlane(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=20),
    )
    if result.fitness < 0.30 or result.inlier_rmse > 0.06:
        return None
    correction = result.transformation @ np.linalg.inv(initial)
    shift = float(np.linalg.norm(correction[:3, 3]))
    angle = _rotation_angle_deg(correction[:3, :3])
    if shift > 0.10 or angle > 6.0:
        return None
    return target_pose @ result.transformation


def _rotation_angle_deg(rotation: np.ndarray) -> float:
    trace = float(np.trace(rotation))
    cosine = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
    return float(np.degrees(np.arccos(cosine)))


def _rectify_depth_to_view(depth: np.ndarray, valid: np.ndarray, view_from_depth: np.ndarray) -> np.ndarray:
    """Map WebXR depth-buffer texels into the projection's normalized view plane."""
    h, w = depth.shape
    y, x = np.nonzero(valid)
    source = np.stack(((x + 0.5) / w, (y + 0.5) / h, np.zeros(len(x)), np.ones(len(x))), axis=1)
    mapped = source @ view_from_depth.T
    mapped /= np.maximum(np.abs(mapped[:, 3:4]), 1e-12)
    view_x, view_y = mapped[:, 0], mapped[:, 1]
    inside = (view_x >= 0.0) & (view_x < 1.0) & (view_y >= 0.0) & (view_y < 1.0)
    output = np.zeros((h, w), dtype=np.uint16)
    if not np.any(inside):
        return output
    ox = np.clip(np.floor(view_x[inside] * w).astype(np.int32), 0, w - 1)
    oy = np.clip(np.floor(view_y[inside] * h).astype(np.int32), 0, h - 1)
    values = depth[y[inside], x[inside]]
    flat = output.reshape(-1)
    targets = oy * w + ox
    # Keeping the nearest surface protects the TSDF from depth discontinuities.
    for target, value in zip(targets, values):
        current = flat[target]
        if current == 0 or value < current:
            flat[target] = value
    return output


def _registered_color(rgb: np.ndarray, depth_shape: tuple[int, int]) -> np.ndarray:
    h, w = depth_shape
    if rgb.shape[:2] == (h, w):
        return np.asarray(rgb, dtype=np.uint8).copy()
    y = np.minimum(rgb.shape[0] - 1, (np.arange(h) * rgb.shape[0] / h).astype(np.int32))
    x = np.minimum(rgb.shape[1] - 1, (np.arange(w) * rgb.shape[1] / w).astype(np.int32))
    return np.asarray(rgb[np.ix_(y, x)], dtype=np.uint8).copy()


def _clean_open3d_mesh(mesh: object) -> None:
    mesh.remove_duplicated_vertices()
    mesh.remove_duplicated_triangles()
    mesh.remove_degenerate_triangles()
    mesh.remove_non_manifold_edges()
    mesh.remove_unreferenced_vertices()
    if len(mesh.triangles):
        mesh.compute_vertex_normals()


def _clean_mesh(mesh: object, *, clean_voxel_m: float, with_colors: bool) -> MeshData:
    simplified = mesh.simplify_vertex_clustering(voxel_size=max(0.008, float(clean_voxel_m)))
    _clean_open3d_mesh(simplified)
    return _mesh_data(simplified, with_colors=with_colors)


def _mesh_data(mesh: object, *, with_colors: bool) -> MeshData:
    positions = np.asarray(mesh.vertices, dtype=np.float32)
    triangles = np.asarray(mesh.triangles, dtype=np.uint32)
    colors = None
    if with_colors and len(mesh.vertex_colors) == len(positions):
        rgb = np.clip(np.rint(np.asarray(mesh.vertex_colors) * 255.0), 0, 255).astype(np.uint8)
        colors = np.concatenate((rgb, np.full((len(rgb), 1), 255, dtype=np.uint8)), axis=1)
    return MeshData(positions, triangles, colors=colors)


def _matrix4(value: object) -> np.ndarray | None:
    if value is None:
        return None
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.size != 16 or not np.all(np.isfinite(matrix)):
        return None
    return matrix.reshape(4, 4)


def _inverse_matrix4(value: object) -> np.ndarray | None:
    matrix = _matrix4(value)
    if matrix is None:
        return None
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError:
        return None
    return inverse if np.all(np.isfinite(inverse)) else None


def _resize_nearest(array: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    y = np.minimum(array.shape[0] - 1, (np.arange(h) * array.shape[0] / h).astype(int))
    x = np.minimum(array.shape[1] - 1, (np.arange(w) * array.shape[1] / w).astype(int))
    return array[np.ix_(y, x)]


def _progress(callback: ProgressCallback | None, message: str, fraction: float) -> None:
    if callback:
        callback(message, max(0.0, min(1.0, fraction)))
