from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .geometry import compute_normals
from .model import MeshData


@dataclass
class GlbTexture:
    name: str
    data: bytes
    mime_type: str = "image/png"


def write_glb(
    path: str | Path,
    meshes: Iterable[MeshData],
    *,
    textures: Iterable[GlbTexture] = (),
    name: str = "RoomTraceReference",
    extras: dict[str, Any] | None = None,
) -> Path:
    primitives = [mesh for mesh in meshes if len(mesh.positions) and len(mesh.indices)]
    if not primitives:
        raise ValueError("cannot write GLB without triangle primitives")
    texture_list = list(textures)
    builder = _GlbBuilder()
    gltf_primitives: list[dict[str, Any]] = []
    for mesh in primitives:
        normals = compute_normals(mesh)
        attributes: dict[str, int] = {
            "POSITION": builder.accessor(mesh.positions.astype(np.float32), "VEC3", 5126, target=34962, include_minmax=True),
            "NORMAL": builder.accessor(normals, "VEC3", 5126, target=34962),
        }
        if mesh.uvs is not None:
            attributes["TEXCOORD_0"] = builder.accessor(mesh.uvs.astype(np.float32), "VEC2", 5126, target=34962)
        if mesh.colors is not None:
            attributes["COLOR_0"] = builder.accessor(mesh.colors.astype(np.uint8), "VEC4", 5121, target=34962, normalized=True)
        primitive: dict[str, Any] = {
            "attributes": attributes,
            "indices": builder.accessor(mesh.indices.astype(np.uint32).reshape(-1), "SCALAR", 5125, target=34963),
            "mode": 4,
        }
        if mesh.uvs is not None:
            primitive["material"] = int(mesh.material_index)
        else:
            primitive["material"] = len(texture_list)
        gltf_primitives.append(primitive)

    image_indices: list[int] = []
    for texture in texture_list:
        view = builder.append_bytes(texture.data)
        image_indices.append(len(builder.images))
        builder.images.append({"name": texture.name, "bufferView": view, "mimeType": texture.mime_type})
    for index in range(len(texture_list)):
        builder.textures.append({"sampler": 0, "source": image_indices[index]})
    textured_materials = [
        {
            "name": f"RoomTraceTexture_{index:03d}",
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": index},
                "metallicFactor": 0.0,
                "roughnessFactor": 0.92,
            },
            "doubleSided": True,
        }
        for index in range(len(texture_list))
    ]
    textured_materials.append(
        {
            "name": "RoomTraceVertexColor",
            "pbrMetallicRoughness": {
                "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.95,
            },
            "doubleSided": True,
        }
    )
    document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "RoomTrace Processor"},
        "scene": 0,
        "scenes": [{"name": name, "nodes": [0]}],
        "nodes": [{"name": name, "mesh": 0}],
        "meshes": [{"name": name, "primitives": gltf_primitives}],
        "materials": textured_materials,
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
        "textures": builder.textures,
        "images": builder.images,
        "bufferViews": builder.buffer_views,
        "accessors": builder.accessors,
        "buffers": [{"byteLength": len(builder.binary)}],
    }
    if extras:
        document["extras"] = extras
    binary = bytes(builder.binary)
    json_chunk = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
    binary += b"\x00" * ((4 - len(binary) % 4) % 4)
    glb = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_chunk) + 8 + len(binary))
    glb += struct.pack("<I4s", len(json_chunk), b"JSON") + json_chunk
    glb += struct.pack("<I4s", len(binary), b"BIN\x00") + binary
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(glb)
    return destination


class _GlbBuilder:
    def __init__(self) -> None:
        self.binary = bytearray()
        self.buffer_views: list[dict[str, Any]] = []
        self.accessors: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self.textures: list[dict[str, Any]] = []

    def _align(self, alignment: int = 4) -> None:
        padding = (-len(self.binary)) % alignment
        if padding:
            self.binary.extend(b"\x00" * padding)

    def append_bytes(self, data: bytes, alignment: int = 4) -> int:
        self._align(alignment)
        offset = len(self.binary)
        self.binary.extend(data)
        self._align(4)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        self.buffer_views.append(view)
        return len(self.buffer_views) - 1

    def accessor(
        self,
        array: np.ndarray,
        type_name: str,
        component_type: int,
        *,
        target: int,
        normalized: bool = False,
        include_minmax: bool = False,
    ) -> int:
        data = np.ascontiguousarray(array).tobytes()
        view_index = self.append_bytes(data)
        accessor: dict[str, Any] = {
            "bufferView": view_index,
            "componentType": component_type,
            "count": int(len(array)),
            "type": type_name,
        }
        self.buffer_views[view_index]["target"] = target
        if normalized:
            accessor["normalized"] = True
        if include_minmax and len(array):
            accessor["min"] = np.min(array, axis=0).astype(float).tolist()
            accessor["max"] = np.max(array, axis=0).astype(float).tolist()
        self.accessors.append(accessor)
        return len(self.accessors) - 1


def write_ply(path: str | Path, positions: np.ndarray, colors: np.ndarray | None = None) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    positions = np.asarray(positions, dtype=np.float32).reshape((-1, 3))
    if colors is None:
        colors = np.full((len(positions), 4), 255, dtype=np.uint8)
    colors = np.asarray(colors, dtype=np.uint8).reshape((-1, 4))
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(positions)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\nproperty uchar alpha\n"
        "end_header\n"
    ).encode("ascii")
    record = np.empty(len(positions), dtype=[("p", "<f4", (3,)), ("c", "u1", (4,))])
    record["p"] = positions
    record["c"] = colors[: len(positions)]
    destination.write_bytes(header + record.tobytes())
    return destination

