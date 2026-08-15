from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np


@dataclass(frozen=True)
class Intrinsics:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    distortion_model: str = "none"
    distortion: tuple[float, ...] = ()

    def scaled(self, width: int, height: int) -> "Intrinsics":
        if width == self.width and height == self.height:
            return self
        sx = width / self.width
        sy = height / self.height
        return Intrinsics(
            width=width,
            height=height,
            fx=self.fx * sx,
            fy=self.fy * sy,
            cx=self.cx * sx,
            cy=self.cy * sy,
            distortion_model=self.distortion_model,
            distortion=self.distortion,
        )


@dataclass
class FrameRecord:
    frame_id: int
    timestamp_ns: int
    image_path: str
    pose_c2w: np.ndarray
    depth_path: str | None = None
    confidence_path: str | None = None
    tracking_state: str = "TRACKING"
    image_timestamp_ns: int | None = None
    depth_timestamp_ns: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, float | str | bool] = field(default_factory=dict)

    @property
    def position(self) -> np.ndarray:
        return self.pose_c2w[:3, 3]

    def as_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "frame_id": self.frame_id,
            "timestamp_ns": self.timestamp_ns,
            "image": self.image_path,
            "pose_c2w": [float(x) for x in self.pose_c2w.reshape(-1)],
            "tracking_state": self.tracking_state,
        }
        if self.depth_path:
            result["depth"] = self.depth_path
        if self.confidence_path:
            result["confidence"] = self.confidence_path
        if self.image_timestamp_ns is not None:
            result["image_timestamp_ns"] = self.image_timestamp_ns
        if self.depth_timestamp_ns is not None:
            result["depth_timestamp_ns"] = self.depth_timestamp_ns
        if self.metadata:
            result["metadata"] = self.metadata
        if self.quality:
            result["quality"] = self.quality
        return result


@dataclass
class ValidationIssue:
    severity: str  # error | warning
    code: str
    message: str
    frame_id: int | None = None
    path: str | None = None

    def as_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.frame_id is not None:
            value["frame_id"] = self.frame_id
        if self.path:
            value["path"] = self.path
        return value


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    frame_count: int = 0
    rgb_frames: int = 0
    depth_frames: int = 0
    confidence_frames: int = 0
    checked_bytes: int = 0

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "frame_count": self.frame_count,
            "rgb_frames": self.rgb_frames,
            "depth_frames": self.depth_frames,
            "confidence_frames": self.confidence_frames,
            "checked_bytes": self.checked_bytes,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "issues": [issue.as_json() for issue in self.issues],
        }


@dataclass
class Capture:
    root: Path
    manifest: dict[str, Any]
    intrinsics: Intrinsics
    frames: list[FrameRecord]
    reader: Any
    source_label: str

    @property
    def capture_id(self) -> str:
        return str(self.manifest.get("capture_id") or self.root.stem)

    @property
    def capabilities(self) -> dict[str, bool]:
        return {str(k): bool(v) for k, v in self.manifest.get("capabilities", {}).items()}

    @property
    def coordinate_system(self) -> dict[str, Any]:
        return self.manifest.get("coordinate_system", {})

    def exists(self, path: str) -> bool:
        return self.reader.exists(path)

    def read_bytes(self, path: str) -> bytes:
        return self.reader.read_bytes(path)

    def close(self) -> None:
        close = getattr(self.reader, "close", None)
        if close:
            close()

    def __enter__(self) -> "Capture":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class FrameQuality:
    frame_id: int
    blur_score: float
    brightness: float
    contrast: float
    clipped_ratio: float
    quality_score: float
    usable: bool
    reason: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "blur_score": round(self.blur_score, 4),
            "brightness": round(self.brightness, 4),
            "contrast": round(self.contrast, 4),
            "clipped_ratio": round(self.clipped_ratio, 6),
            "quality_score": round(self.quality_score, 4),
            "usable": self.usable,
            "reason": self.reason,
        }


@dataclass
class MeshData:
    positions: np.ndarray
    indices: np.ndarray
    uvs: np.ndarray | None = None
    colors: np.ndarray | None = None
    frame_id: int | None = None
    material_index: int = 0

    def __post_init__(self) -> None:
        self.positions = np.asarray(self.positions, dtype=np.float32).reshape((-1, 3))
        self.indices = np.asarray(self.indices, dtype=np.uint32).reshape((-1, 3))
        if self.uvs is not None:
            self.uvs = np.asarray(self.uvs, dtype=np.float32).reshape((-1, 2))
        if self.colors is not None:
            value = np.asarray(self.colors)
            if value.ndim == 2 and value.shape[1] == 3:
                alpha = np.full((value.shape[0], 1), 255, dtype=value.dtype)
                value = np.concatenate([value, alpha], axis=1)
            self.colors = value.astype(np.uint8, copy=False).reshape((-1, 4))


def matrix4(value: Any) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.size != 16:
        raise ValueError("pose must contain 16 values")
    matrix = matrix.reshape((4, 4))
    if not np.all(np.isfinite(matrix)):
        raise ValueError("pose contains non-finite values")
    return matrix


def coerce_intrinsics(value: dict[str, Any]) -> Intrinsics:
    def number(name: str) -> float:
        raw = value.get(name)
        if raw is None:
            raise ValueError(f"intrinsics.{name} is required")
        return float(raw)

    return Intrinsics(
        width=int(value["width"]),
        height=int(value["height"]),
        fx=number("fx"),
        fy=number("fy"),
        cx=number("cx"),
        cy=number("cy"),
        distortion_model=str(value.get("distortion_model", "none")),
        distortion=tuple(float(x) for x in value.get("distortion", [])),
    )

