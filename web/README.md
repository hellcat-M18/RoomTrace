# RoomTrace Web Capture

`web/` is a static, serverless capture SPA. It uses `getUserMedia()` for the RGB stream and requests WebXR `depth-sensing` with CPU access for the AR pose and depth buffer. At stop time it creates the existing RoomTrace `.roomcap.zip` in the browser and does not upload the capture to a server.

## GitHub Pages

The repository includes `.github/workflows/pages.yml`. Push the project to GitHub, enable **Settings → Pages → GitHub Actions**, and open the URL shown by GitHub Pages on the Android phone. GitHub Pages serves the files over HTTPS, which is required for camera and depth permissions.

The SPA intentionally requires a browser session that can provide WebXR CPU depth. A normal mobile camera permission alone is not sufficient to produce a usable RoomTrace mesh. If the capability check fails, use the native Android capture app or another supported device/browser instead of saving an incomplete capture.

## Capture contract

The browser exports the same v1 layout as the Android app:

```text
manifest.json
intrinsics.json
frames.jsonl
images/00000001.jpg
depth/00000001.png
confidence/00000001.png
checksums.sha256
```

The confidence image is derived from valid/invalid depth because the WebXR Depth Sensing API does not expose ARCore's separate confidence plane. The manifest records this fact. The browser records the session's selected depth format and depth type so the Windows quality report can distinguish the source.

