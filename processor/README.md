# RoomTrace Processor

`roomtrace` is the Windows-side processor for `.roomcap` captures. It has no GPU requirement and produces Blender-ready GLB files from synchronized RGB, pose, and depth frames. For normal use, double-click the repository's `RoomTrace.cmd`; the Tk UI automatically chooses a safe output folder, shows stage-by-stage progress, and can open Blender after processing.

Browser captures use the stored WebXR depth sensor pose, projection matrix, and depth-buffer coordinate transform for reconstruction. A browser capture made before those fields existed is rejected rather than generating a geometrically corrupt GLB; update the GitHub Pages app and record again. Browser RGB is not assumed to be calibrated to the WebXR depth sensor, so browser output uses vertex colors instead of texture atlases.

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
