# RoomTrace

RoomTrace is a practical capture-to-Blender workflow for rebuilding a hotel room from a walk-through scan.

```text
arrows We2 Plus / ARCore
        ↓  RGB + pose + Raw Depth + confidence
Windows RoomTrace Processor
        ↓  quality filtering + local ICP + Open3D TSDF fusion
Blender
        ↓
room_reference_raw.glb / room_reference_clean.glb
```

## What is included

- `android/`: Kotlin Android capture app for the rear ARCore camera.
- `web/`: serverless browser capture SPA that can be deployed to GitHub Pages.
- `processor/`: Windows-friendly Python CLI and small Tk desktop UI.
- `blender/roomtrace_blender.py`: optional Blender organization helper.
- `schemas/`: RoomTrace v1 data contract.
- `examples/`: deterministic synthetic capture and generated output for offline verification.
- `plans.md`: the fixed implementation and acceptance plan.

## ブラウザ版（GitHub Pages）

`web/` は静的HTML/CSS/JavaScriptだけで動くスマホ収録SPA。GitHubへpushしてPagesの公開元をGitHub Actionsにすると、スマホでURLを開いて使える。カメラ映像は `getUserMedia()`、AR姿勢とDepthはWebXR Depth Sensingから取得し、停止時に既存形式の `.roomcap.zip` を端末内で生成する。各DepthフレームにはWebXRのDepthセンサー姿勢・投影行列・Depth座標変換を保存し、PC側はそれをOpen3D TSDF融合の入力にする。撮影データをRoomTraceのサーバーへ送る機能は持たない。

ただし、ブラウザのWebXR Depth対応は広く均一ではない。Depthが取得できない端末では「カメラだけの収録」にフォールバックせず、PC側で処理できないZIPを作らない。まず `web/` の対応状況表示がすべてOKになるAndroid Chrome環境で実機確認する。WebXRのDepth Sensing API自体がSecure Context限定かつLimited availabilityである点は、[MDNのDepth API資料](https://developer.mozilla.org/en-US/docs/Web/API/XRFrame/getDepthInformation)にも記載されている。なお、この座標情報を含まない旧ブラウザ版のZIPはPC処理が明示的に拒否するため、ページ更新後に再撮影する。

## いちばん簡単な使い方（Windows）

1. `RoomTrace-delivery.zip` を好きな場所へ展開する。
2. 中にある `RoomTrace.cmd` をダブルクリックする。
3. 初回だけ、Python と必要なライブラリが自動で入り、デスクトップにショートカットが作られる。
4. RoomTrace画面で撮影した `.roomcap.zip` を選び、「Blender用に変換」を押す。
5. 完了後、「出力フォルダを開く」または「Blenderで開く」を押す。

撮影ZIPを `RoomTrace.cmd` の上へドラッグ＆ドロップして起動することもできる。出力先は撮影データの隣に自動作成され、既存の結果は上書きしない。

Blenderでは通常 `room_reference_clean.glb` を先に使い、細部確認が必要な時だけ `room_reference_raw.glb` を追加する。処理結果には `quality_report.html` も含まれるので、撮影の失敗箇所をブラウザで確認できる。

## 開発者向け：コマンドラインで実行

The processor requires Python 3.10+, NumPy, Pillow, and Open3D. It does not require CUDA, ROCm, or COLMAP; Open3D runs on CPU by default.

```bash
cd processor
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
pip install -e .

roomtrace sample ..\examples\sample.roomcap --force
roomtrace inspect ..\examples\sample.roomcap --verify-checksums
roomtrace process ..\examples\sample.roomcap --output ..\examples\sample-output --force
roomtrace gui
```

CPU並列数は通常自動設定される。負荷を抑えたい場合は `roomtrace process`
に `--workers 1` などを追加できる。変換後の `processing_manifest.json`
には工程別の `timings_seconds` が記録され、実データで遅い工程を切り分けられる。

The processor writes self-contained fused GLBs, so the primary files do not depend on the source folder. Browser WebXR scans are geometry-first because their regular camera image is not registered to the depth sensor; use **File → Import → glTF 2.0** and import `room_reference_clean.glb` first, then `room_reference_raw.glb` for detail.

## Android build

Android Studio と JDK 17 を一度インストールした後は、`android\Build-APK.cmd` をダブルクリックすれば APK を作成できる。完成した `RoomTrace-Capture.apk` を We2 Plus にコピーしてタップするとインストールできる。Gradle はプロジェクト内の `.tools` に自動取得されるため、毎回Android Studioでビルド設定を触る必要はない。

Android Studio未導入の場合だけ、先にAndroid Studioをインストールし、`android/` を一度開いてSDK 35とJDK 17を準備する。ARCore対応可否、端末のCPU画像解像度、Raw Depth対応は実機依存なので、We2 Plusでの最終確認は必要になる。

The app stores an unfinished directory while recording and creates a checksummed `.roomcap.zip` when the user taps **Stop & share**.

The capture app deliberately records the device-provided CPU image size rather than assuming a 50MP still stream. At runtime it selects the largest available rear-camera CPU configuration at 30fps and enables `RAW_DEPTH_ONLY` when the device supports it. ARCore documents that Raw Depth is sparse and should be used with its matching confidence image; the processor preserves that sparsity instead of filling unobserved surfaces silently. See the [ARCore Raw Depth guide](https://developers.google.com/ar/develop/java/depth/raw-depth), [Frame API](https://developers.google.com/ar/reference/java/com/google/ar/core/Frame), and [camera configuration guide](https://developers.google.com/ar/develop/java/camera-configs).

## Acceptance run on the real device

1. Install the APK and confirm ARCore starts.
2. Record a 60–90 second room walk with the phone moving slowly.
3. Share the `.roomcap.zip` to the Windows PC.
4. Run `roomtrace inspect capture.roomcap.zip --verify-checksums`.
5. Run `roomtrace process capture.roomcap.zip --output room-output`.
6. Open both GLBs in Blender and check floor orientation, scale, wall continuity, and texture seams.
7. Keep `quality_report.html` with the capture. It identifies underexposed, blurry, missing-depth, and low-confidence frames.

Physical We2 Plus behavior, available CPU resolution, tracking stability, and ARCore depth support cannot be verified inside this development container. The app records those capabilities in `manifest.json`; the Windows processor reports missing or sparse data explicitly.
