from __future__ import annotations

import io
import math
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

import numpy as np
from PIL import Image

from .model import Capture, FrameQuality, FrameRecord


def read_rgb(capture: Capture, frame: FrameRecord) -> np.ndarray:
    with Image.open(io.BytesIO(capture.read_bytes(frame.image_path))) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8).copy()


def read_depth(capture: Capture, frame: FrameRecord) -> np.ndarray:
    if not frame.depth_path:
        raise ValueError(f"frame {frame.frame_id} has no depth")
    with Image.open(io.BytesIO(capture.read_bytes(frame.depth_path))) as image:
        # Pillow exposes ARCore's little-endian 16-bit PNG as I;16 on most hosts.
        array = np.asarray(image, dtype=np.uint16)
        return array.copy()


def read_confidence(capture: Capture, frame: FrameRecord, shape: tuple[int, int]) -> np.ndarray:
    if not frame.confidence_path or not capture.exists(frame.confidence_path):
        return np.full(shape, 255, dtype=np.uint8)
    with Image.open(io.BytesIO(capture.read_bytes(frame.confidence_path))) as image:
        array = np.asarray(image.convert("L"), dtype=np.uint8)
        if array.shape != shape:
            resized = image.convert("L").resize((shape[1], shape[0]), Image.Resampling.NEAREST)
            array = np.asarray(resized, dtype=np.uint8)
        return array.copy()


def score_frame(capture: Capture, frame: FrameRecord, *, min_blur: float = 12.0) -> FrameQuality:
    try:
        image = read_rgb(capture, frame)
    except Exception as exc:
        return FrameQuality(frame.frame_id, 0.0, 0.0, 0.0, 1.0, 0.0, False, f"RGB decode failed: {exc}")
    gray = image.astype(np.float32).mean(axis=2) / 255.0
    if min(gray.shape) < 3:
        return FrameQuality(frame.frame_id, 0.0, float(gray.mean()), 0.0, 1.0, 0.0, False, "image is too small")
    laplacian = (
        gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
        - 4.0 * gray[1:-1, 1:-1]
    )
    blur_score = float(laplacian.var() * 10_000.0)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    clipped_ratio = float(((gray <= 0.01) | (gray >= 0.99)).mean())
    blur_component = min(1.0, blur_score / max(1.0, min_blur * 6.0))
    exposure_component = max(0.0, 1.0 - max(0.0, 0.16 - brightness) / 0.16 - max(0.0, brightness - 0.90) / 0.10)
    contrast_component = min(1.0, contrast / 0.18)
    clipping_component = max(0.0, 1.0 - clipped_ratio * 4.0)
    quality_score = float(0.45 * blur_component + 0.25 * exposure_component + 0.20 * contrast_component + 0.10 * clipping_component)
    usable = bool(blur_score >= min_blur and 0.02 <= brightness <= 0.98 and clipped_ratio < 0.35)
    reason = "" if usable else _quality_reason(blur_score, brightness, clipped_ratio)
    return FrameQuality(frame.frame_id, blur_score, brightness, contrast, clipped_ratio, quality_score, usable, reason)


def _quality_reason(blur: float, brightness: float, clipped: float) -> str:
    reasons: list[str] = []
    if blur < 12.0:
        reasons.append("blur")
    if brightness < 0.02:
        reasons.append("too_dark")
    elif brightness > 0.98:
        reasons.append("too_bright")
    if clipped >= 0.35:
        reasons.append("clipped")
    return ",".join(reasons) or "low_quality"


def score_frames(
    capture: Capture,
    frames: Iterable[FrameRecord] | None = None,
    *,
    workers: int = 0,
) -> dict[int, FrameQuality]:
    selected = list(frames if frames is not None else capture.frames)
    if not selected:
        return {}
    worker_count = _quality_worker_count(workers, len(selected))
    if worker_count == 1:
        results = [score_frame(capture, frame) for frame in selected]
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="RoomTraceQuality") as executor:
            results = list(executor.map(lambda frame: score_frame(capture, frame), selected))
    return {quality.frame_id: quality for quality in results}


def _quality_worker_count(requested: int, frame_count: int) -> int:
    if requested > 0:
        return max(1, min(int(requested), frame_count))
    cpu_count = os.cpu_count() or 2
    return max(1, min(8, max(2, cpu_count // 2), frame_count))


def select_keyframes(
    frames: Iterable[FrameRecord],
    qualities: dict[int, FrameQuality],
    *,
    min_translation_m: float = 0.03,
    min_rotation_deg: float = 3.0,
    min_interval_s: float = 0.5,
    max_frames: int = 600,
) -> list[FrameRecord]:
    ordered = sorted(frames, key=lambda frame: (frame.timestamp_ns, frame.frame_id))
    if not ordered:
        return []
    candidates = [frame for frame in ordered if qualities.get(frame.frame_id, FrameQuality(frame.frame_id, 0, 0, 0, 1, 0, False)).usable and frame.tracking_state.upper() == "TRACKING"]
    if not candidates:
        candidates = ordered
    selected: list[FrameRecord] = [candidates[0]]
    last = candidates[0]
    for frame in candidates[1:]:
        elapsed = max(0.0, (frame.timestamp_ns - last.timestamp_ns) / 1_000_000_000.0)
        translation = float(np.linalg.norm(frame.position - last.position))
        rotation = rotation_angle_deg(frame.pose_c2w[:3, :3] @ last.pose_c2w[:3, :3].T)
        if elapsed >= min_interval_s or translation >= min_translation_m or rotation >= min_rotation_deg:
            selected.append(frame)
            last = frame
    if selected[-1].frame_id != candidates[-1].frame_id:
        selected.append(candidates[-1])
    if len(selected) <= max_frames:
        return selected
    # Keep temporal coverage uniform, while always retaining the endpoints.
    indices = np.linspace(0, len(selected) - 1, max_frames, dtype=np.int64)
    unique = sorted(set(int(index) for index in indices))
    return [selected[index] for index in unique]


def rotation_angle_deg(rotation: np.ndarray) -> float:
    trace = float(np.trace(rotation))
    cosine = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    return math.degrees(math.acos(cosine))
