# RoomTrace Capture

This is the Android side of RoomTrace. It uses the rear ARCore camera, requests the largest device-provided CPU image configuration at 30fps, and saves synchronized data in the RoomTrace v1 format.

## Build

The shortest Windows route is:

1. Install Android Studio once and open this `android/` directory once so the Android SDK 35 and bundled JDK 17 are available.
2. Double-click `Build-APK.cmd` in this directory.
3. Copy the generated `RoomTrace-Capture.apk` from the project root to the phone and tap it to install.

`Build-APK.ps1` finds the Android SDK and bundled JDK automatically. If Gradle is not already available, it downloads Gradle 8.7 into the project `.tools` directory and reuses it on later builds. The build helper is intentionally separate from the capture app so the phone never needs Python, Blender, or a network connection while recording.

No storage permission is needed: captures are written to the app's external files directory and shared as a single ZIP through `FileProvider`.

## Capture behavior

- RGB is sampled from `Frame.acquireCameraImage()` and encoded as high-quality JPEG.
- Pose is written as a row-major camera-to-world 4×4 matrix in ARCore's gravity-aligned meter coordinate system.
- Raw depth is written as little-endian millimetre `uint16` PNG; confidence is written as `uint8` PNG.
- Frames are retained when at least 0.5 seconds elapsed, 3cm of translation occurred, or 3° of rotation occurred.
- A bounded writer queue prevents unbounded memory growth. If the phone cannot keep up, the UI shows dropped frames and the package remains internally consistent.
- An incomplete directory remains on disk if the process is interrupted. Only a completed package receives `complete=true` and `checksums.sha256`.

The CPU image, depth, and pose timestamps are stored separately. This is intentional: ARCore can return a reprojected Raw Depth image whose depth timestamp has not changed, and the processor can account for that later.
