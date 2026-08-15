from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .model import FrameRecord, Intrinsics, MeshData


@dataclass
class AlignedScene:
    meshes: list[MeshData]
    transform: np.ndarray
    floor_height_world: float
    bounds: dict[str, list[float]]


def depth_mesh(
    depth_mm: np.ndarray,
    rgb: np.ndarray,
    confidence: np.ndarray,
    intrinsics: Intrinsics,
    pose_c2w: np.ndarray,
    *,
    frame_id: int,
    sample_step: int = 4,
    confidence_threshold: int = 96,
    min_depth_m: float = 0.15,
    max_depth_m: float = 12.0,
    edge_threshold_m: float = 0.35,
) -> MeshData | None:
    """Convert one ARCore depth image into a triangle mesh in world coordinates.

    The capture contract uses camera-forward = -Z, matching ARCore's camera
    coordinate convention. Depth values are millimetres along the optical axis.
    """
    depth = np.asarray(depth_mm, dtype=np.float32) / 1000.0
    if depth.ndim != 2 or depth.size == 0:
        return None
    h, w = depth.shape
    if confidence.shape != (h, w):
        confidence = _resize_nearest(confidence, (h, w))
    rgb_h, rgb_w = rgb.shape[:2]
    depth_intrinsics = intrinsics.scaled(w, h)
    step = max(1, int(sample_step))
    ys = np.arange(0, h, step, dtype=np.int32)
    xs = np.arange(0, w, step, dtype=np.int32)
    if ys[-1] != h - 1:
        ys = np.append(ys, h - 1)
    if xs[-1] != w - 1:
        xs = np.append(xs, w - 1)
    grid_h, grid_w = len(ys), len(xs)
    valid = np.zeros((grid_h, grid_w), dtype=bool)
    positions = np.zeros((grid_h, grid_w, 3), dtype=np.float32)
    colors = np.zeros((grid_h, grid_w, 4), dtype=np.uint8)
    uvs = np.zeros((grid_h, grid_w, 2), dtype=np.float32)
    for gy, y in enumerate(ys):
        z_values = depth[y, xs]
        conf_values = confidence[y, xs]
        for gx, (x, z, conf) in enumerate(zip(xs, z_values, conf_values)):
            if not (min_depth_m <= z <= max_depth_m and conf >= confidence_threshold and np.isfinite(z)):
                continue
            camera = np.array(
                [
                    (float(x) - depth_intrinsics.cx) * float(z) / depth_intrinsics.fx,
                    (float(y) - depth_intrinsics.cy) * float(z) / depth_intrinsics.fy,
                    -float(z),
                    1.0,
                ],
                dtype=np.float64,
            )
            world = pose_c2w @ camera
            positions[gy, gx] = world[:3].astype(np.float32)
            rgb_x = min(rgb_w - 1, max(0, int(round(float(x) * rgb_w / w))))
            rgb_y = min(rgb_h - 1, max(0, int(round(float(y) * rgb_h / h))))
            colors[gy, gx, :3] = rgb[rgb_y, rgb_x, :3]
            colors[gy, gx, 3] = 255
            uvs[gy, gx] = [rgb_x / max(1, rgb_w - 1), 1.0 - rgb_y / max(1, rgb_h - 1)]
            valid[gy, gx] = True

    vertex_count = int(valid.sum())
    if vertex_count < 3:
        return None
    vertex_index = np.full((grid_h, grid_w), -1, dtype=np.int64)
    vertex_index[valid] = np.arange(vertex_count, dtype=np.int64)
    flat_positions = positions[valid]
    flat_colors = colors[valid]
    flat_uvs = uvs[valid]
    faces: list[tuple[int, int, int]] = []
    for gy in range(grid_h - 1):
        for gx in range(grid_w - 1):
            corners = vertex_index[gy : gy + 2, gx : gx + 2].reshape(-1)
            if np.any(corners < 0):
                continue
            a, b, c, d = (int(value) for value in corners)
            local = flat_positions[[a, b, c, d]]
            if _too_large(local, edge_threshold_m):
                continue
            # The winding is chosen for a camera-facing surface; normals are
            # recomputed later and can be flipped by the exporter when needed.
            faces.append((a, c, b))
            faces.append((b, c, d))
    if not faces:
        return None
    return MeshData(flat_positions, np.asarray(faces, dtype=np.uint32), flat_uvs, flat_colors, frame_id)


def _too_large(points: np.ndarray, threshold: float) -> bool:
    max_distance = float(np.max(np.linalg.norm(points[1:] - points[0], axis=1)))
    return max_distance > threshold


def _resize_nearest(array: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    y = np.minimum(array.shape[0] - 1, (np.arange(h) * array.shape[0] / h).astype(int))
    x = np.minimum(array.shape[1] - 1, (np.arange(w) * array.shape[1] / w).astype(int))
    return array[np.ix_(y, x)]


def transform_mesh(mesh: MeshData, matrix: np.ndarray) -> MeshData:
    positions_h = np.concatenate([mesh.positions, np.ones((len(mesh.positions), 1), dtype=np.float32)], axis=1)
    positions = (positions_h @ matrix.T)[:, :3]
    return MeshData(positions, mesh.indices.copy(), None if mesh.uvs is None else mesh.uvs.copy(), None if mesh.colors is None else mesh.colors.copy(), mesh.frame_id, mesh.material_index)


def estimate_floor_y(meshes: Iterable[MeshData]) -> float:
    points = np.concatenate([mesh.positions for mesh in meshes if len(mesh.positions)], axis=0)
    if len(points) == 0:
        return 0.0
    y = points[:, 1]
    # The ARCore world is gravity-aligned. A low percentile is more stable than
    # the absolute minimum when a few depth pixels are noisy.
    return float(np.percentile(y, 2.0))


def blender_alignment(meshes: list[MeshData]) -> AlignedScene:
    if not meshes:
        raise ValueError("cannot align an empty scene")
    floor_y = estimate_floor_y(meshes)
    # ARCore: X right, Y up, camera-forward -Z. Blender: X right, Z up.
    # Input is column-vector homogeneous; this maps world [x,y,z] to [x,z,y-floor].
    matrix = np.array(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 1.0, 0.0, -floor_y],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    aligned = [transform_mesh(mesh, matrix) for mesh in meshes]
    points = np.concatenate([mesh.positions for mesh in aligned], axis=0)
    bounds = {"min": points.min(axis=0).astype(float).tolist(), "max": points.max(axis=0).astype(float).tolist()}
    return AlignedScene(aligned, matrix, floor_y, bounds)


def scale_scene(scene: AlignedScene, *, reference_width_m: float | None = None, reference_depth_m: float | None = None) -> tuple[AlignedScene, float]:
    dimensions = np.asarray(scene.bounds["max"]) - np.asarray(scene.bounds["min"])
    factors: list[float] = []
    if reference_width_m is not None:
        factors.append(float(reference_width_m) / max(1e-6, float(dimensions[0])))
    if reference_depth_m is not None:
        factors.append(float(reference_depth_m) / max(1e-6, float(dimensions[1])))
    if not factors:
        return scene, 1.0
    if len(factors) == 2 and abs(factors[0] - factors[1]) > 0.05 * max(factors):
        raise ValueError("reference width and depth imply conflicting scale factors")
    factor = float(sum(factors) / len(factors))
    scale = np.diag([factor, factor, factor, 1.0])
    meshes = [transform_mesh(mesh, scale) for mesh in scene.meshes]
    points = np.concatenate([mesh.positions for mesh in meshes], axis=0)
    bounds = {"min": points.min(axis=0).astype(float).tolist(), "max": points.max(axis=0).astype(float).tolist()}
    return AlignedScene(meshes, scale @ scene.transform, scene.floor_height_world, bounds), factor


def merged_point_cloud(meshes: Iterable[MeshData]) -> tuple[np.ndarray, np.ndarray]:
    meshes = list(meshes)
    chunks = [mesh.positions for mesh in meshes if len(mesh.positions)]
    if not chunks:
        return np.empty((0, 3), dtype=np.float32), np.empty((0, 4), dtype=np.uint8)
    colors = [mesh.colors if mesh.colors is not None else np.full((len(mesh.positions), 4), 255, dtype=np.uint8) for mesh in meshes if len(mesh.positions)]
    return np.concatenate(chunks, axis=0), np.concatenate(colors, axis=0)


def voxel_reduce(meshes: Iterable[MeshData], voxel_size: float = 0.025) -> MeshData:
    """Reduce a collection of textured frame meshes into a colored clean mesh."""
    source = list(meshes)
    all_positions, all_colors = merged_point_cloud(source)
    if len(all_positions) == 0:
        return MeshData(np.empty((0, 3)), np.empty((0, 3), dtype=np.uint32))
    keys = np.floor(all_positions / max(1e-4, voxel_size)).astype(np.int64)
    unique, inverse = np.unique(keys, axis=0, return_inverse=True)
    positions = np.zeros((len(unique), 3), dtype=np.float64)
    colors = np.zeros((len(unique), 4), dtype=np.float64)
    counts = np.bincount(inverse, minlength=len(unique)).astype(np.float64)
    np.add.at(positions, inverse, all_positions)
    np.add.at(colors, inverse, all_colors)
    positions /= counts[:, None]
    colors /= counts[:, None]
    # Preserve triangle connectivity by remapping source triangles into the
    # voxel representatives. Duplicate/degenerate faces are discarded.
    remapped_faces: list[tuple[int, int, int]] = []
    offset = 0
    seen: set[tuple[int, int, int]] = set()
    for mesh in source:
        mapping = inverse[offset : offset + len(mesh.positions)]
        offset += len(mesh.positions)
        for face in mesh.indices:
            mapped = tuple(int(mapping[int(index)]) for index in face)
            if len(set(mapped)) < 3:
                continue
            canonical = tuple(sorted(mapped))
            if canonical in seen:
                continue
            seen.add(canonical)
            remapped_faces.append(mapped)
    if not remapped_faces:
        return MeshData(positions.astype(np.float32), np.empty((0, 3), dtype=np.uint32), colors=np.rint(colors).astype(np.uint8))
    return MeshData(positions.astype(np.float32), np.asarray(remapped_faces, dtype=np.uint32), colors=np.rint(colors).astype(np.uint8))


def compute_normals(mesh: MeshData) -> np.ndarray:
    normals = np.zeros_like(mesh.positions, dtype=np.float64)
    if len(mesh.indices):
        tri = mesh.positions[mesh.indices]
        face_normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        lengths = np.linalg.norm(face_normals, axis=1, keepdims=True)
        face_normals = face_normals / np.maximum(lengths, 1e-12)
        for column in range(3):
            np.add.at(normals, mesh.indices[:, column], face_normals)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = normals / np.maximum(lengths, 1e-12)
    return normals.astype(np.float32)
