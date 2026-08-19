# RoomTrace Processor

`roomtrace` is the Windows-side processor for `.roomcap` captures. It runs completely on the local PC and produces Blender-ready GLB files from synchronized pose and depth frames. Its standard reconstruction is Open3D's scalable TSDF volume fusion with conservative adjacent-frame ICP; it has no GPU requirement. For normal use, double-click the repository's `RoomTrace.cmd`; the Tk UI automatically chooses a safe output folder, shows stage-by-stage progress, and can open Blender after processing.

Browser captures use the stored WebXR depth sensor pose, projection matrix, and depth-buffer coordinate transform for reconstruction. A browser capture made before those fields existed is rejected rather than generating a geometrically corrupt GLB; update the GitHub Pages app and record again. Browser RGB is not assumed to be calibrated to the WebXR depth sensor, so browser output intentionally has no photo texture or false vertex colors. It produces geometry only; calibrated native RGB-D capture is required for reliable texture output.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

roomtrace sample .\sample.roomcap
roomtrace inspect .\sample.roomcap
roomtrace process .\sample.roomcap --output .\room-output
```

The normal output is:

- `room_reference_raw.glb`: high-density locally fused TSDF reference mesh.
- `room_reference_clean.glb`: locally simplified mesh for modeling and measurements.
- `pointcloud.ply`: vertices from the clean fused mesh.
- `cameras.json`: selected camera poses and quality scores.
- `measurements.csv`: real-scale bounds and dominant planes.
- `quality_report.html` / `quality_report.json`: warnings, frame selection and processing metrics.

Open the GLB files directly in Blender with **File → Import → glTF 2.0**. The processor maps the ARCore Y-up capture coordinates to Blender X/Y/Z, places the estimated floor at Z=0, and embeds texture images in the GLB.

Open3D is a normal dependency of the built-in processor. CUDA is optional: the supported default is CPU processing on Windows.
