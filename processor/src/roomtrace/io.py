from __future__ import annotations

import hashlib
import io
import json
import posixpath
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .errors import CaptureFormatError
from .model import Capture, FrameRecord, ValidationIssue, ValidationReport, coerce_intrinsics, matrix4


FORMAT_VERSION = 1


class _Reader:
    def exists(self, name: str) -> bool:
        raise NotImplementedError

    def read_bytes(self, name: str) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        return None


def _safe_name(name: str) -> str:
    normalized = posixpath.normpath(name.replace("\\", "/")).lstrip("/")
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        raise CaptureFormatError(f"invalid capture path: {name!r}")
    return normalized


class DirectoryReader(_Reader):
    def __init__(self, root: Path):
        self.root = root

    def _path(self, name: str) -> Path:
        safe = _safe_name(name)
        path = (self.root / safe).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise CaptureFormatError(f"capture path escapes root: {name!r}")
        return path

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def read_bytes(self, name: str) -> bytes:
        path = self._path(name)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise CaptureFormatError(f"missing capture file: {name}") from exc


class ZipReader(_Reader):
    def __init__(self, path: Path):
        self.archive = zipfile.ZipFile(path, "r")
        self.names = {_safe_name(name) for name in self.archive.namelist() if not name.endswith("/")}

    def exists(self, name: str) -> bool:
        return _safe_name(name) in self.names

    def read_bytes(self, name: str) -> bytes:
        safe = _safe_name(name)
        try:
            return self.archive.read(safe)
        except KeyError as exc:
            raise CaptureFormatError(f"missing capture file: {name}") from exc

    def close(self) -> None:
        self.archive.close()


def _json(reader: _Reader, name: str) -> dict[str, Any]:
    try:
        value = json.loads(reader.read_bytes(name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureFormatError(f"invalid JSON in {name}") from exc
    if not isinstance(value, dict):
        raise CaptureFormatError(f"{name} must contain a JSON object")
    return value


def _jsonl(reader: _Reader, name: str) -> Iterator[dict[str, Any]]:
    try:
        text = reader.read_bytes(name).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaptureFormatError(f"invalid UTF-8 in {name}") from exc
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaptureFormatError(f"invalid JSON in {name}:{line_no}") from exc
        if not isinstance(value, dict):
            raise CaptureFormatError(f"{name}:{line_no} must be an object")
        yield value


def load_capture(path: str | Path) -> Capture:
    source = Path(path).expanduser().resolve()
    if source.is_dir():
        reader: _Reader = DirectoryReader(source)
        label = str(source)
    elif source.is_file() and source.suffix.lower() in {".zip", ".roomcap"}:
        reader = ZipReader(source)
        label = str(source)
    else:
        raise CaptureFormatError(f"capture path is not a directory or ZIP: {path}")

    try:
        manifest = _json(reader, "manifest.json")
        if manifest.get("format") not in (None, "roomcap"):
            raise CaptureFormatError("manifest.format is not 'roomcap'")
        version = int(manifest.get("format_version", 1))
        if version > FORMAT_VERSION:
            raise CaptureFormatError(
                f"capture format {version} is newer than supported format {FORMAT_VERSION}"
            )
        intrinsics_name = str(manifest.get("files", {}).get("intrinsics", "intrinsics.json"))
        intrinsics = coerce_intrinsics(_json(reader, intrinsics_name))
        frames_name = str(manifest.get("files", {}).get("frames", "frames.jsonl"))
        if not reader.exists(frames_name):
            frames_name = "poses.jsonl"
        if not reader.exists(frames_name):
            raise CaptureFormatError("manifest has no frames.jsonl or poses.jsonl")

        frames: list[FrameRecord] = []
        for raw in _jsonl(reader, frames_name):
            try:
                frame_id = int(raw["frame_id"])
                timestamp = int(raw.get("timestamp_ns", raw.get("timestamp", 0)))
                image_path = str(raw.get("image") or raw.get("rgb"))
                if not image_path or image_path == "None":
                    raise KeyError("image")
                pose_value = raw.get("pose_c2w", raw.get("pose", {}).get("matrix"))
                if pose_value is None:
                    raise KeyError("pose_c2w")
                pose = matrix4(pose_value)
            except (KeyError, TypeError, ValueError) as exc:
                raise CaptureFormatError(f"invalid frame record for frame {raw.get('frame_id')}") from exc
            frames.append(
                FrameRecord(
                    frame_id=frame_id,
                    timestamp_ns=timestamp,
                    image_path=_safe_name(image_path),
                    depth_path=_safe_optional_path(raw.get("depth")),
                    confidence_path=_safe_optional_path(raw.get("confidence")),
                    pose_c2w=pose,
                    tracking_state=str(raw.get("tracking_state", "TRACKING")),
                    image_timestamp_ns=_optional_int(raw.get("image_timestamp_ns")),
                    depth_timestamp_ns=_optional_int(raw.get("depth_timestamp_ns")),
                    metadata=dict(raw.get("metadata", {})),
                    quality=dict(raw.get("quality", {})),
                )
            )
        if not frames:
            raise CaptureFormatError("capture contains no frame records")
        frames.sort(key=lambda frame: (frame.timestamp_ns, frame.frame_id))
        return Capture(root=source, manifest=manifest, intrinsics=intrinsics, frames=frames, reader=reader, source_label=label)
    except Exception:
        reader.close()
        raise


def _safe_optional_path(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _safe_name(str(value))


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def validate_capture(capture: Capture, verify_checksums: bool = False, inspect_images: bool = False) -> ValidationReport:
    report = ValidationReport(frame_count=len(capture.frames))
    if capture.manifest.get("complete") is False:
        report.issues.append(ValidationIssue("error", "incomplete_capture", "manifest marks this capture as unfinished"))
    seen_ids: set[int] = set()
    last_timestamp = -1
    for frame in capture.frames:
        if frame.frame_id in seen_ids:
            report.issues.append(ValidationIssue("error", "duplicate_frame_id", "duplicate frame_id", frame.frame_id))
        seen_ids.add(frame.frame_id)
        if frame.timestamp_ns < last_timestamp:
            report.issues.append(ValidationIssue("warning", "timestamp_order", "timestamps are not monotonic", frame.frame_id))
        last_timestamp = max(last_timestamp, frame.timestamp_ns)
        if not capture.exists(frame.image_path):
            report.issues.append(ValidationIssue("error", "missing_rgb", "RGB image is missing", frame.frame_id, frame.image_path))
        else:
            report.rgb_frames += 1
            if inspect_images:
                _inspect_image(capture, frame, report)
        if frame.depth_path:
            if capture.exists(frame.depth_path):
                report.depth_frames += 1
                if inspect_images:
                    _inspect_depth(capture, frame, report)
            else:
                report.issues.append(ValidationIssue("warning", "missing_depth", "depth image is missing", frame.frame_id, frame.depth_path))
        if frame.confidence_path:
            if capture.exists(frame.confidence_path):
                report.confidence_frames += 1
            else:
                report.issues.append(ValidationIssue("warning", "missing_confidence", "confidence image is missing", frame.frame_id, frame.confidence_path))
        if frame.tracking_state.upper() not in {"TRACKING", "PAUSED", "STOPPED", "UNKNOWN"}:
            report.issues.append(ValidationIssue("warning", "unknown_tracking_state", f"unknown tracking state {frame.tracking_state!r}", frame.frame_id))
        if not _is_rigid_pose(frame.pose_c2w):
            report.issues.append(ValidationIssue("warning", "non_rigid_pose", "pose rotation is not close to orthonormal", frame.frame_id))

    if not capture.capabilities.get("rgb", True):
        report.issues.append(ValidationIssue("warning", "rgb_capability_false", "manifest says RGB is unavailable"))
    if not capture.capabilities.get("raw_depth", bool(report.depth_frames)):
        report.issues.append(ValidationIssue("warning", "depth_capability_false", "manifest says Raw Depth is unavailable; textured geometry cannot be generated without a depth source"))
    if report.depth_frames == 0:
        report.issues.append(ValidationIssue("error", "no_depth", "no usable depth images were found"))

    if verify_checksums:
        _verify_checksums(capture, report)
    return report


def _is_rigid_pose(pose: Any) -> bool:
    import numpy as np

    matrix = np.asarray(pose)
    rotation = matrix[:3, :3]
    return bool(np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-3) and np.allclose(rotation.T @ rotation, np.eye(3), atol=2e-2))


def _inspect_image(capture: Capture, frame: FrameRecord, report: ValidationReport) -> None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(capture.read_bytes(frame.image_path))) as image:
            if image.width < 320 or image.height < 240:
                report.issues.append(ValidationIssue("warning", "low_rgb_resolution", f"RGB is only {image.width}x{image.height}", frame.frame_id, frame.image_path))
    except Exception as exc:
        report.issues.append(ValidationIssue("error", "invalid_rgb", f"cannot decode RGB image: {exc}", frame.frame_id, frame.image_path))


def _inspect_depth(capture: Capture, frame: FrameRecord, report: ValidationReport) -> None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(capture.read_bytes(frame.depth_path or ""))) as image:
            if image.width < 32 or image.height < 24:
                report.issues.append(ValidationIssue("warning", "low_depth_resolution", f"depth is only {image.width}x{image.height}", frame.frame_id, frame.depth_path))
    except Exception as exc:
        report.issues.append(ValidationIssue("error", "invalid_depth", f"cannot decode depth image: {exc}", frame.frame_id, frame.depth_path))


def _verify_checksums(capture: Capture, report: ValidationReport) -> None:
    checksum_name = str(capture.manifest.get("files", {}).get("checksums", "checksums.sha256"))
    if not capture.exists(checksum_name):
        report.issues.append(ValidationIssue("warning", "checksums_missing", "checksum file was requested but is missing", path=checksum_name))
        return
    for line in capture.read_bytes(checksum_name).decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            report.issues.append(ValidationIssue("warning", "checksum_line", f"invalid checksum line: {line}"))
            continue
        expected, name = parts
        name = name.lstrip("*")
        if not capture.exists(name):
            report.issues.append(ValidationIssue("error", "checksum_file_missing", "checksum references missing file", path=name))
            continue
        actual = hashlib.sha256(capture.read_bytes(name)).hexdigest()
        report.checked_bytes += len(capture.read_bytes(name))
        if actual.lower() != expected.lower():
            report.issues.append(ValidationIssue("error", "checksum_mismatch", "SHA-256 checksum mismatch", path=name))
