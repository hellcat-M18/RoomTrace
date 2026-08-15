from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def create_sample_capture(destination: str | Path, *, frame_count: int = 8, force: bool = False) -> Path:
    root = Path(destination).expanduser().resolve()
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"sample destination is not empty: {root}")
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "depth").mkdir(parents=True, exist_ok=True)
    (root / "confidence").mkdir(parents=True, exist_ok=True)
    rgb_w, rgb_h = 320, 240
    depth_w, depth_h = 80, 60
    intrinsics = {"width": rgb_w, "height": rgb_h, "fx": 260.0, "fy": 260.0, "cx": rgb_w / 2, "cy": rgb_h / 2, "distortion_model": "none", "distortion": []}
    frames: list[dict[str, object]] = []
    for index in range(frame_count):
        image = Image.new("RGB", (rgb_w, rgb_h), (30, 36, 44))
        draw = ImageDraw.Draw(image)
        for y in range(0, rgb_h, 20):
            for x in range(0, rgb_w, 20):
                color = (50 + ((x // 20 + y // 20 + index) % 2) * 50, 95, 140 + (index * 7) % 80)
                draw.rectangle((x, y, x + 19, y + 19), fill=color)
        draw.rectangle((24 + index * 4, 36, 118 + index * 4, 142), outline=(245, 200, 85), width=4)
        draw.ellipse((190, 78, 258, 146), fill=(210, 90, 80), outline=(255, 245, 220), width=3)
        image_path = root / "images" / f"{index + 1:08d}.jpg"
        image.save(image_path, quality=95, subsampling=0)

        yy, xx = np.mgrid[0:depth_h, 0:depth_w]
        # A gently sloped wall gives the test mesh non-zero triangles without
        # making the sample dependent on any external reconstruction package.
        depth = (2100.0 + xx * 2.0 + yy * 0.8 + index * 4.0).astype(np.uint16)
        depth_path = root / "depth" / f"{index + 1:08d}.png"
        Image.fromarray(depth).save(depth_path)
        confidence = np.full((depth_h, depth_w), 220, dtype=np.uint8)
        confidence[:2, :] = 40
        Image.fromarray(confidence, mode="L").save(root / "confidence" / f"{index + 1:08d}.png")
        tx = (index - (frame_count - 1) / 2.0) * 0.06
        pose = [1, 0, 0, tx, 0, 1, 0, 1.6, 0, 0, 1, 0, 0, 0, 0, 1]
        frames.append({
            "frame_id": index + 1,
            "timestamp_ns": (index + 1) * 500_000_000,
            "image": f"images/{index + 1:08d}.jpg",
            "depth": f"depth/{index + 1:08d}.png",
            "confidence": f"confidence/{index + 1:08d}.png",
            "pose_c2w": pose,
            "tracking_state": "TRACKING",
            "image_timestamp_ns": (index + 1) * 500_000_000,
            "depth_timestamp_ns": (index + 1) * 500_000_000,
        })
    (root / "intrinsics.json").write_text(json.dumps(intrinsics, indent=2), encoding="utf-8")
    with (root / "frames.jsonl").open("w", encoding="utf-8") as stream:
        for frame in frames:
            stream.write(json.dumps(frame, separators=(",", ":")) + "\n")
    manifest = {
        "format": "roomcap",
        "format_version": 1,
        "capture_id": "sample-room",
        "created_at": "1970-01-01T00:00:00Z",
        "device": {"manufacturer": "RoomTrace", "model": "Synthetic fixture"},
        "capabilities": {"rgb": True, "pose": True, "raw_depth": True, "depth_confidence": True},
        "coordinate_system": {"pose": "camera_to_world", "camera_forward": "-Z", "up_axis": "Y", "units": "m"},
        "files": {"frames": "frames.jsonl", "intrinsics": "intrinsics.json", "checksums": "checksums.sha256"},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    checksum_lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "checksums.sha256":
            continue
        checksum_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}")
    (root / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return root
