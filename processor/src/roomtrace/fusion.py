"""Open3D based, pose-aware TSDF reconstruction for RoomTrace captures.

This module deliberately keeps all work on the local computer.  It turns each
captured depth image into the calibrated virtual camera image expected by
Open3D, then integrates the images into one signed-distance volume.  This is
fundamentally different from placing one triangle grid per frame: overlapping
observations reinforce a surface while incompatible observations are rejected
by the truncation band.
"""

from __future__ import annotations

import os
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from time import perf_counter
from typing import Callable, Iterator

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
    preprocess_workers: int = 0


@dataclass(frozen=True)
class FusionResult:
    raw_mesh: MeshData
    clean_mesh: MeshData
    integrated_frames: list[FrameRecord]
    frame_errors: list[dict[str, str | int]]
    icp_refined_frames: int = 0
    method: str = "open3d_scalable_tsdf"
    preprocess_workers: int = 1
    timings_seconds: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedFrame:
    frame: FrameRecord
    depth_image: np.ndarray
    intrinsic: tuple[int, int, float, float, float, float]
    camera_to_world: np.ndarray
    rgb: np.ndarray | None
    preparation_seconds: float


@dataclass(frozen=True)
class _FusionInput:
    index: int
    prepared: _PreparedFrame
    intrinsic: object
    cloud: object
    icp_future: Future[tuple[np.ndarray | None, float]] | None


def fuse_capture(
    capture: Capture,
    frames: list[FrameRecord],
    options: FusionOptions,
    *,
    progress: ProgressCallback | None = None,
    o3d_module: object | None = None,
    open3d_load_seconds: float | None = None,
) -> FusionResult:
    """Fuse calibrated frames into a single mesh using Open3D ScalableTSDF."""
    fusion_started = perf_counter()
    if o3d_module is None:
        open3d_load_started = perf_counter()
        o3d = load_open3d(progress=progress, fraction=0.245)
        open3d_load_seconds = perf_counter() - open3d_load_started
    else:
        o3d = o3d_module
        open3d_load_seconds = max(0.0, float(open3d_load_seconds or 0.0))

    browser_capture = capture.manifest.get("device", {}).get("source") == "browser-spa"
    registered_rgb = bool(capture.capabilities.get("rgb_registered_to_depth", not browser_capture))
    _progress(progress, "TSDFボリュームを初期化しています", 0.25)
    volume_started = perf_counter()
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=max(0.005, float(options.voxel_size_m)),
        sdf_trunc=max(float(options.sdf_trunc_m), float(options.voxel_size_m) * 2.0),
        color_type=(
            o3d.pipelines.integration.TSDFVolumeColorType.RGB8
            if registered_rgb
            else o3d.pipelines.integration.TSDFVolumeColorType.NoColor
        ),
    )
    volume_initialization_seconds = perf_counter() - volume_started

    integrated: list[FrameRecord] = []
    errors: list[dict[str, str | int]] = []
    last_cloud = None
    last_raw_pose = None
    previous_integrated_pose = None
    icp_refined_frames = 0
    preparation_worker_seconds = 0.0
    preparation_wait_seconds = 0.0
    pointcloud_seconds = 0.0
    icp_seconds = 0.0
    integration_seconds = 0.0
    worker_count = _preprocess_worker_count(options.preprocess_workers, len(frames))
    icp_worker_count = max(1, min(4, worker_count))
    ready_limit = max(2, icp_worker_count * 2)
    ready: deque[_FusionInput] = deque()
    total = max(1, len(frames))
    _progress(progress, f"深度フレームを並列準備しています（{worker_count}スレッド）", 0.255)
    icp_executor = ThreadPoolExecutor(max_workers=icp_worker_count, thread_name_prefix="RoomTraceICP")

    def integrate_one(item: _FusionInput) -> None:
        nonlocal previous_integrated_pose, icp_refined_frames, icp_seconds, integration_seconds
        prepared = item.prepared
        camera_to_world = prepared.camera_to_world.copy()
        if item.icp_future is not None:
            try:
                relative_pose, elapsed = item.icp_future.result()
                icp_seconds += elapsed
                if relative_pose is not None and previous_integrated_pose is not None:
                    camera_to_world = previous_integrated_pose @ relative_pose
                    icp_refined_frames += 1
            except Exception as exc:
                errors.append({"frame_id": prepared.frame.frame_id, "error": f"icp_failed: {exc}"})
        if registered_rgb:
            if prepared.rgb is None:
                raise ValueError("registered_rgb_missing")
            color_image = o3d.geometry.Image(prepared.rgb)
        else:
            # WebXR's getUserMedia image has no calibrated relation to its
            # depth sensor.  Supplying it would paint the mesh incorrectly.
            color_image = o3d.geometry.Image(np.zeros((*prepared.depth_image.shape, 3), dtype=np.uint8))
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            color_image,
            o3d.geometry.Image(prepared.depth_image),
            depth_scale=1000.0,
            depth_trunc=float(options.max_depth_m),
            convert_rgb_to_intensity=False,
        )
        integration_started = perf_counter()
        volume.integrate(rgbd, item.intrinsic, np.linalg.inv(camera_to_world))
        integration_seconds += perf_counter() - integration_started
        integrated.append(prepared.frame)
        previous_integrated_pose = camera_to_world
        _progress(
            progress,
            f"ICP・TSDF処理中（{item.index}/{len(frames)}）",
            0.26 + 0.52 * item.index / total,
        )

    try:
        for index, frame, future in _prepare_frames(
            capture,
            frames,
            options,
            registered_rgb=registered_rgb,
            workers=worker_count,
        ):
            wait_started = perf_counter()
            wait_recorded = False
            try:
                prepared = future.result()
                preparation_wait_seconds += perf_counter() - wait_started
                wait_recorded = True
                preparation_worker_seconds += prepared.preparation_seconds
                width, height, fx, fy, cx, cy = prepared.intrinsic
                intrinsic = o3d.camera.PinholeCameraIntrinsic(width, height, fx, fy, cx, cy)
                pointcloud_started = perf_counter()
                cloud = o3d.geometry.PointCloud.create_from_depth_image(
                    o3d.geometry.Image(prepared.depth_image),
                    intrinsic,
                    depth_scale=1000.0,
                    depth_trunc=float(options.max_depth_m),
                    stride=2,
                ).voxel_down_sample(max(0.025, float(options.voxel_size_m) * 2.0))
                if options.refine_poses:
                    cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.12, max_nn=30))
                pointcloud_seconds += perf_counter() - pointcloud_started
                icp_future = None
                if options.refine_poses and last_cloud is not None and last_raw_pose is not None:
                    icp_future = icp_executor.submit(
                        _timed_adjacent_icp,
                        o3d,
                        cloud,
                        last_cloud,
                        prepared.camera_to_world,
                        last_raw_pose,
                    )
                ready.append(_FusionInput(index, prepared, intrinsic, cloud, icp_future))
                last_cloud = cloud
                last_raw_pose = prepared.camera_to_world
                if len(ready) >= ready_limit:
                    integrate_one(ready.popleft())
            except Exception as exc:
                if not wait_recorded:
                    preparation_wait_seconds += perf_counter() - wait_started
                errors.append({"frame_id": frame.frame_id, "error": str(exc)})
                _progress(progress, f"無効フレームを除外しました（{index}/{len(frames)}）", 0.26 + 0.52 * index / total)
        while ready:
            integrate_one(ready.popleft())
    finally:
        icp_executor.shutdown(wait=True, cancel_futures=True)

    if len(integrated) < 2:
        raise ProcessingError("Open3D TSDF integration needs at least two calibrated depth frames")
    _progress(progress, "TSDFから連続メッシュを抽出しています", 0.80)
    extraction_started = perf_counter()
    mesh = volume.extract_triangle_mesh()
    extraction_seconds = perf_counter() - extraction_started
    cleanup_started = perf_counter()
    _clean_open3d_mesh(mesh)
    raw = _mesh_data(mesh, with_colors=registered_rgb)
    if len(raw.indices) == 0:
        raise ProcessingError("TSDF fusion produced no surfaces; move slowly and keep walls/floor in range")
    _progress(progress, "軽量版メッシュを最適化しています", 0.86)
    clean = _clean_mesh(mesh, clean_voxel_m=options.clean_voxel_m, with_colors=registered_rgb)
    cleanup_seconds = perf_counter() - cleanup_started
    if len(clean.indices) == 0:
        clean = raw
    timings = {
        "fusion_total": perf_counter() - fusion_started,
        "preprocess_worker_sum": preparation_worker_seconds,
        "preprocess_main_wait": preparation_wait_seconds,
        "pointcloud": pointcloud_seconds,
        "open3d_load": open3d_load_seconds,
        "tsdf_initialization": volume_initialization_seconds,
        "icp_worker_sum": icp_seconds,
        "tsdf_integration": integration_seconds,
        "mesh_extraction": extraction_seconds,
        "mesh_cleanup": cleanup_seconds,
    }
    return FusionResult(
        raw,
        clean,
        integrated,
        errors,
        icp_refined_frames,
        preprocess_workers=worker_count,
        timings_seconds={key: round(value, 3) for key, value in timings.items()},
    )


def load_open3d(*, progress: ProgressCallback | None = None, fraction: float = 0.04) -> object:
    """Load Open3D early while emitting a heartbeat for slow Windows DLL loads."""
    started = perf_counter()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RoomTraceOpen3D")
    future = executor.submit(_import_open3d)
    try:
        while True:
            try:
                return future.result(timeout=0.5)
            except FutureTimeoutError:
                elapsed = perf_counter() - started
                heartbeat_fraction = min(0.09, fraction + elapsed * 0.004)
                _progress(progress, f"Open3Dを初期化しています（{elapsed:.0f}秒経過）", heartbeat_fraction)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _import_open3d() -> object:
    try:
        import open3d as o3d
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on installation
        detail = f"{type(exc).__name__}: {exc}"
        raise ProcessingError(
            "Open3D could not be loaded. Re-run Setup-RoomTrace.ps1; if the problem "
            "continues, restart Windows and check whether security software blocked an Open3D DLL. "
            f"Underlying error: {detail}"
        ) from exc
    return o3d


def _prepare_frames(
    capture: Capture,
    frames: list[FrameRecord],
    options: FusionOptions,
    *,
    registered_rgb: bool,
    workers: int,
) -> Iterator[tuple[int, FrameRecord, Future[_PreparedFrame]]]:
    """Yield a bounded, ordered stream of concurrently prepared frames."""
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="RoomTraceDepth")
    pending: deque[tuple[int, FrameRecord, Future[_PreparedFrame]]] = deque()
    source = iter(enumerate(frames, start=1))

    def submit_next() -> bool:
        try:
            index, frame = next(source)
        except StopIteration:
            return False
        future = executor.submit(
            _prepare_frame,
            capture,
            frame,
            options,
            registered_rgb=registered_rgb,
        )
        pending.append((index, frame, future))
        return True

    for _ in range(min(len(frames), workers * 2)):
        submit_next()
    try:
        while pending:
            item = pending.popleft()
            submit_next()
            yield item
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _prepare_frame(
    capture: Capture,
    frame: FrameRecord,
    options: FusionOptions,
    *,
    registered_rgb: bool,
) -> _PreparedFrame:
    started = perf_counter()
    if not frame.depth_path or not capture.exists(frame.depth_path):
        raise ValueError("depth_missing")
    depth_mm = read_depth(capture, frame)
    confidence = read_confidence(capture, frame, depth_mm.shape)
    prepared = _prepare_depth_frame(
        depth_mm,
        confidence,
        capture.intrinsics,
        frame,
        confidence_threshold=options.confidence_threshold,
        max_depth_m=options.max_depth_m,
    )
    if prepared is None:
        raise ValueError("no_valid_depth")
    depth_image, intrinsic, camera_to_world = prepared
    rgb = _registered_color(read_rgb(capture, frame), depth_image.shape) if registered_rgb else None
    return _PreparedFrame(frame, depth_image, intrinsic, camera_to_world, rgb, perf_counter() - started)


def _preprocess_worker_count(requested: int, frame_count: int) -> int:
    if requested > 0:
        return max(1, min(int(requested), max(1, frame_count)))
    cpu_count = os.cpu_count() or 2
    return max(1, min(8, max(2, cpu_count // 2), max(1, frame_count)))


def _prepare_depth_frame(
    depth_mm: np.ndarray,
    confidence: np.ndarray,
    fallback_intrinsics: Intrinsics,
    frame: FrameRecord,
    *,
    confidence_threshold: int,
    max_depth_m: float,
) -> tuple[np.ndarray, tuple[int, int, float, float, float, float], np.ndarray] | None:
    depth = np.asarray(depth_mm, dtype=np.uint16)
    if depth.ndim != 2 or not depth.size:
        return None
    if confidence.shape != depth.shape:
        confidence = _resize_nearest(confidence, depth.shape)
    valid = (depth > 0) & (depth.astype(np.float32) <= max_depth_m * 1000.0) & (confidence >= confidence_threshold)
    if not np.any(valid):
        return None
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
        return None
    # WebXR: +X right, +Y up, camera looks down -Z.
    # Open3D: +X right, +Y down, camera looks down +Z.
    open3d_to_webxr = np.diag([1.0, -1.0, -1.0, 1.0])
    intrinsic = (int(w), int(h), float(fx), float(fy), float(cx), float(cy))
    return rectified, intrinsic, pose @ open3d_to_webxr


def _timed_adjacent_icp(
    o3d: object,
    source: object,
    target: object,
    source_pose: np.ndarray,
    target_pose: np.ndarray,
) -> tuple[np.ndarray | None, float]:
    started = perf_counter()
    relative_pose = _refine_adjacent_transform(o3d, source, target, source_pose, target_pose)
    return relative_pose, perf_counter() - started


def _refine_adjacent_transform(
    o3d: object,
    source: object,
    target: object,
    source_pose: np.ndarray,
    target_pose: np.ndarray,
) -> np.ndarray | None:
    """Use conservative local ICP to correct small tracking drift only.

    The WebXR pose remains the prior.  Large corrections are deliberately
    rejected because a featureless wall can otherwise cause ICP to snap a room
    onto itself.
    """
    if len(source.points) < 80 or len(target.points) < 80:
        return None
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
    return result.transformation


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
    targets = oy * w + ox
    # NumPy executes the collision reduction in compiled code.  The old Python
    # loop processed every valid texel on one core and dominated long scans.
    empty = np.iinfo(np.uint32).max
    flat = np.full(h * w, empty, dtype=np.uint32)
    np.minimum.at(flat, targets, values.astype(np.uint32, copy=False))
    flat[flat == empty] = 0
    return flat.astype(np.uint16, copy=False).reshape((h, w))


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
