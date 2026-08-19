from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from roomtrace.io import load_capture, validate_capture
from roomtrace.fusion import _rectify_depth_to_view
from roomtrace.pipeline import ProcessOptions, process_capture
from roomtrace.sample import create_sample_capture


class RoomTraceEndToEndTests(unittest.TestCase):
    def test_depth_rectification_keeps_nearest_collision_without_python_loop(self) -> None:
        depth = np.array([[1000, 900], [800, 700]], dtype=np.uint16)
        valid = np.ones((2, 2), dtype=bool)
        collapse_to_first_pixel = np.array(
            [[0.0, 0.0, 0.0, 0.1], [0.0, 0.0, 0.0, 0.1], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]],
            dtype=float,
        )
        result = _rectify_depth_to_view(depth, valid, collapse_to_first_pixel)
        self.assertEqual(int(result[0, 0]), 700)
        self.assertEqual(int(np.count_nonzero(result)), 1)

    def test_identity_depth_rectification_preserves_pixels(self) -> None:
        depth = np.array([[1000, 900], [800, 700]], dtype=np.uint16)
        result = _rectify_depth_to_view(depth, depth > 0, np.eye(4))
        np.testing.assert_array_equal(result, depth)

    def test_sample_capture_inspect_process_and_glb(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = create_sample_capture(root / "capture", frame_count=5)
            with load_capture(capture) as loaded:
                report = validate_capture(loaded, verify_checksums=True, inspect_images=True)
                self.assertTrue(report.ok, report.as_json())
                self.assertEqual(report.depth_frames, 5)
            result = process_capture(
                capture,
                ProcessOptions(output_dir=root / "output", depth_step=5, max_frames=20, reference_width_m=5.0),
            )
            self.assertTrue(result.raw_glb.exists())
            self.assertTrue(result.clean_glb.exists())
            self.assertGreater(result.summary["raw_triangles"], 0)
            self.assertGreater(result.summary["clean_triangles"], 0)
            self.assertEqual(result.summary["reconstruction"], "open3d_scalable_tsdf")
            self.assertGreaterEqual(result.summary["preprocess_workers"], 1)
            self.assertIn("tsdf_integration", result.summary["timings_seconds"])
            self.assertGreater(result.summary["scale_factor"], 1.0)
            width = result.summary["bounds_max_m"][0] - result.summary["bounds_min_m"][0]
            self.assertAlmostEqual(width, 5.0, places=3)
            self.assert_glb(result.raw_glb, expected_images=0)
            self.assert_glb(result.clean_glb, expected_images=0)

    def test_zip_capture_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = create_sample_capture(root / "capture", frame_count=3)
            archive = root / "capture.roomcap"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for path in capture.rglob("*"):
                    if path.is_file():
                        output.write(path, path.relative_to(capture).as_posix())
            with load_capture(archive) as loaded:
                report = validate_capture(loaded, verify_checksums=True)
                self.assertTrue(report.ok, report.as_json())
                self.assertEqual(len(loaded.frames), 3)
            result = process_capture(archive, ProcessOptions(output_dir=root / "zip-output", depth_step=6))
            self.assertTrue(result.raw_glb.exists())

    def test_parallel_processing_preserves_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = create_sample_capture(root / "capture", frame_count=8)
            serial = process_capture(
                capture,
                ProcessOptions(output_dir=root / "serial", max_frames=8, preprocess_workers=1),
            )
            parallel = process_capture(
                capture,
                ProcessOptions(output_dir=root / "parallel", max_frames=8, preprocess_workers=4),
            )
            self.assertEqual(parallel.summary["raw_vertices"], serial.summary["raw_vertices"])
            self.assertEqual(parallel.summary["raw_triangles"], serial.summary["raw_triangles"])
            self.assertEqual(parallel.summary["icp_refined_frames"], serial.summary["icp_refined_frames"])
            self.assertEqual(parallel.summary["preprocess_workers"], 4)

    def test_legacy_browser_capture_is_rejected_before_meshing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = create_sample_capture(root / "capture", frame_count=3)
            manifest_path = capture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["device"] = {"source": "browser-spa"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with load_capture(capture) as loaded:
                report = validate_capture(loaded)
            self.assertFalse(report.ok)
            self.assertTrue(any(issue.code == "legacy_browser_depth_geometry" for issue in report.errors))

    def test_browser_depth_view_geometry_processes_without_texture_atlas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = create_sample_capture(root / "capture", frame_count=3)
            identity = np.eye(4).reshape(-1).tolist()
            projection = np.array(
                [[2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0], [0.0, 0.0, -1.0, -1.0], [0.0, 0.0, -1.0, 0.0]],
                dtype=float,
            ).reshape(-1).tolist()
            frames_path = capture / "frames.jsonl"
            frames = [json.loads(line) for line in frames_path.read_text(encoding="utf-8").splitlines()]
            for frame in frames:
                frame["metadata"] = {
                    "depth_pose_c2w": frame["pose_c2w"],
                    "depth_projection_matrix": projection,
                    "norm_depth_buffer_from_norm_view": identity,
                    "depth_coordinate_system": "webxr-depth-view-v1",
                    "rgb_registration": "unregistered_getUserMedia",
                }
            frames_path.write_text("\n".join(json.dumps(frame) for frame in frames) + "\n", encoding="utf-8")
            manifest_path = capture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["device"] = {"source": "browser-spa"}
            manifest["capabilities"]["rgb_registered_to_depth"] = False
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = process_capture(capture, ProcessOptions(output_dir=root / "output", depth_step=6))
            self.assertTrue(result.raw_glb.exists())
            self.assert_glb(result.raw_glb, expected_images=0)

    def assert_glb(self, path: Path, expected_images: int) -> None:
        data = path.read_bytes()
        magic, version, total_length = struct.unpack_from("<4sII", data, 0)
        self.assertEqual(magic, b"glTF")
        self.assertEqual(version, 2)
        self.assertEqual(total_length, len(data))
        json_length, chunk_type = struct.unpack_from("<I4s", data, 12)
        self.assertEqual(chunk_type, b"JSON")
        document = json.loads(data[20 : 20 + json_length])
        self.assertEqual(len(document.get("images", [])), expected_images)
        self.assertGreater(len(document["meshes"][0]["primitives"]), 0)


if __name__ == "__main__":
    unittest.main()
