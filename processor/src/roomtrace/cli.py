from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import RoomTraceError
from .pipeline import ProcessOptions, inspect_capture, process_capture
from .sample import create_sample_capture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roomtrace", description="Inspect RoomTrace captures and create locally fused Blender GLBs")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="validate a .roomcap directory or ZIP")
    inspect.add_argument("capture", type=Path)
    inspect.add_argument("--verify-checksums", action="store_true")
    inspect.add_argument("--json", action="store_true", dest="as_json")
    process = sub.add_parser("process", help="fuse depth locally and create Raw/Clean Blender GLBs")
    process.add_argument("capture", type=Path)
    process.add_argument("--output", type=Path, required=True)
    process.add_argument("--confidence-threshold", type=int, default=96)
    process.add_argument("--depth-step", type=int, default=1, help="compatibility option; TSDF uses all valid depth pixels")
    process.add_argument("--clean-voxel", type=float, default=0.025, help="clean mesh voxel size in metres")
    process.add_argument("--max-frames", type=int, default=600)
    process.add_argument("--max-depth", type=float, default=12.0, help="ignore depth beyond this distance in metres")
    process.add_argument("--tsdf-voxel", type=float, default=0.025, help="TSDF resolution in metres (smaller is finer and slower)")
    process.add_argument("--tsdf-trunc", type=float, default=0.10, help="TSDF truncation distance in metres")
    process.add_argument("--workers", type=int, default=0, help="parallel preprocessing/ICP workers; 0 selects automatically")
    process.add_argument("--reference-width", type=float, help="set the final Blender X width in metres")
    process.add_argument("--reference-depth", type=float, help="set the final Blender Y depth in metres")
    process.add_argument("--no-icp", action="store_true", help="disable conservative adjacent-frame pose refinement")
    process.add_argument("--verify-checksums", action="store_true")
    process.add_argument("--force", action="store_true", help="allow writing into a non-empty output directory")
    sample = sub.add_parser("sample", help="generate a small deterministic capture for testing")
    sample.add_argument("destination", type=Path)
    sample.add_argument("--frames", type=int, default=8)
    sample.add_argument("--force", action="store_true")
    gui = sub.add_parser("gui", help="open the desktop processor")
    gui.add_argument("capture", type=Path, nargs="?", help="optional capture path; used by drag-and-drop launch")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            capture, report = inspect_capture(args.capture, verify_checksums=args.verify_checksums, inspect_images=True)
            try:
                if args.as_json:
                    print(json.dumps(report.as_json(), ensure_ascii=False, indent=2))
                else:
                    print(f"capture: {args.capture}")
                    print(f"frames: {report.frame_count}  RGB: {report.rgb_frames}  depth: {report.depth_frames}  confidence: {report.confidence_frames}")
                    print(f"status: {'OK' if report.ok else 'ERROR'}  errors: {len(report.errors)}  warnings: {len(report.warnings)}")
                    for issue in report.issues:
                        location = f" frame={issue.frame_id}" if issue.frame_id is not None else ""
                        print(f"[{issue.severity}] {issue.code}{location}: {issue.message}")
            finally:
                capture.close()
            return 0 if report.ok else 2
        if args.command == "sample":
            path = create_sample_capture(args.destination, frame_count=max(2, args.frames), force=args.force)
            print(path)
            return 0
        if args.command == "gui":
            from .gui import launch

            launch(Path(args.capture).expanduser() if args.capture else None)
            return 0
        if args.command == "process":
            last_progress = -1

            def show_progress(message: str, fraction: float) -> None:
                nonlocal last_progress
                percent = int(round(fraction * 100))
                if percent == last_progress and percent not in {0, 100}:
                    return
                last_progress = percent
                print(f"\r[{percent:3d}%] {message}", end="", file=sys.stderr, flush=True)

            result = process_capture(
                args.capture,
                ProcessOptions(
                    output_dir=args.output,
                    confidence_threshold=max(0, min(255, args.confidence_threshold)),
                    depth_step=max(1, args.depth_step),
                    clean_voxel_m=max(0.001, args.clean_voxel),
                    max_frames=max(2, args.max_frames),
                    max_depth_m=max(0.5, args.max_depth),
                    tsdf_voxel_m=max(0.005, args.tsdf_voxel),
                    tsdf_trunc_m=max(0.01, args.tsdf_trunc),
                    preprocess_workers=max(0, args.workers),
                    reference_width_m=args.reference_width if args.reference_width and args.reference_width > 0 else None,
                    reference_depth_m=args.reference_depth if args.reference_depth and args.reference_depth > 0 else None,
                    refine_poses=not args.no_icp,
                    verify_checksums=args.verify_checksums,
                    force=args.force,
                ),
                progress=show_progress,
            )
            print(file=sys.stderr)
            print(json.dumps({"output": str(result.output_dir), **result.summary}, ensure_ascii=False, indent=2))
            return 0
        return 1
    except (RoomTraceError, FileNotFoundError, ValueError) as exc:
        print(f"roomtrace: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
