# RoomTrace Processor

`roomtrace` is the Windows-side processor for `.roomcap` captures. It has no GPU requirement and produces Blender-ready GLB files from synchronized RGB, ARCore pose, and Raw Depth frames. For normal use, double-click the repository's `RoomTrace.cmd`; the Tk UI automatically chooses a safe output folder and can open Blender after processing.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

roomtrace sample .\sample.roomcap
roomtrace inspect .\sample.roomcap
roomtrace process .\sample.roomcap --output .\room-output
```

The normal output is:

- `room_reference_raw.glb`: textured, higher-density reference mesh.
- `room_reference_clean.glb`: voxel-reduced, colored mesh for modeling and measurements.
- `pointcloud.ply`: integrated colored point cloud.
- `cameras.json`: selected camera poses and quality scores.
- `measurements.csv`: real-scale bounds and dominant planes.
- `quality_report.html` / `quality_report.json`: warnings, frame selection and processing metrics.

Open the GLB files directly in Blender with **File → Import → glTF 2.0**. The processor maps the ARCore Y-up capture coordinates to Blender X/Y/Z, places the estimated floor at Z=0, and embeds texture images in the GLB.

External COLMAP/Open3D are optional. The built-in path is deliberately complete for depth-backed captures; those tools can be added later for higher-fidelity pose refinement or meshing.
