from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .model import Capture, FrameRecord, MeshData
from .quality import read_rgb


@dataclass
class AtlasTile:
    atlas_index: int
    offset_x: int
    offset_y: int
    width: int
    height: int
    atlas_width: int
    atlas_height: int

    def map_uv(self, uv: np.ndarray) -> np.ndarray:
        # Input UV is normalized with V already pointing upward.
        mapped = uv.astype(np.float32, copy=True)
        mapped[:, 0] = (self.offset_x + mapped[:, 0] * self.width) / self.atlas_width
        mapped[:, 1] = (self.offset_y + (1.0 - mapped[:, 1]) * self.height) / self.atlas_height
        return mapped


@dataclass
class TextureAtlas:
    images: list[bytes]
    tiles: dict[int, AtlasTile]
    names: list[str]
    mime_type: str = "image/jpeg"


def build_atlases(
    capture: Capture,
    frames: list[FrameRecord],
    output_dir: Path,
    *,
    tile_size: int = 512,
    tiles_per_side: int = 8,
) -> TextureAtlas:
    """Pack source RGB frames into embedded-ready PNG texture atlases."""
    tile_size = max(64, int(tile_size))
    tiles_per_side = max(1, int(tiles_per_side))
    atlas_size = tile_size * tiles_per_side
    tiles: dict[int, AtlasTile] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    images: list[bytes] = []
    names: list[str] = []
    capacity = tiles_per_side * tiles_per_side
    for atlas_index, start in enumerate(range(0, len(frames), capacity)):
        group = frames[start : start + capacity]
        image = Image.new("RGB", (atlas_size, atlas_size), (32, 32, 32))
        for slot, frame in enumerate(group):
            source = Image.fromarray(read_rgb(capture, frame), mode="RGB")
            source.thumbnail((tile_size, tile_size), Image.Resampling.LANCZOS)
            tile_x = (slot % tiles_per_side) * tile_size
            tile_y = (slot // tiles_per_side) * tile_size
            paste_x = tile_x + (tile_size - source.width) // 2
            paste_y = tile_y + (tile_size - source.height) // 2
            image.paste(source, (paste_x, paste_y))
            tiles[frame.frame_id] = AtlasTile(
                atlas_index=atlas_index,
                offset_x=paste_x,
                offset_y=paste_y,
                width=source.width,
                height=source.height,
                atlas_width=atlas_size,
                atlas_height=atlas_size,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92, optimize=True)
        data = buffer.getvalue()
        images.append(data)
        name = f"texture_atlas_{atlas_index:03d}.jpg"
        names.append(name)
        (output_dir / name).write_bytes(data)
        image.close()
    return TextureAtlas(images=images, tiles=tiles, names=names, mime_type="image/jpeg")


def apply_atlas_uv(mesh: MeshData, tile: AtlasTile) -> MeshData:
    if mesh.uvs is None:
        return mesh
    result = MeshData(mesh.positions.copy(), mesh.indices.copy(), tile.map_uv(mesh.uvs), mesh.colors, mesh.frame_id, tile.atlas_index)
    return result
