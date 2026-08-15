import { buildZip, encodePngGray16, encodePngGray8, sha256Hex, utf8Bytes } from "./format.js";
import { CaptureStore } from "./store.js";

const ui = {
  camera: document.querySelector("#camera"),
  preview: document.querySelector("#preview"),
  xrCanvas: document.querySelector("#xr-canvas"),
  status: document.querySelector("#status"),
  error: document.querySelector("#error"),
  cameraCapability: document.querySelector("#camera-capability"),
  xrCapability: document.querySelector("#xr-capability"),
  depthCapability: document.querySelector("#depth-capability"),
  frameCount: document.querySelector("#frame-count"),
  depthCount: document.querySelector("#depth-count"),
  elapsed: document.querySelector("#elapsed"),
  startCamera: document.querySelector("#start-camera"),
  startCapture: document.querySelector("#start-capture"),
  stopCapture: document.querySelector("#stop-capture"),
  download: document.querySelector("#download"),
  instructions: document.querySelector("#instructions"),
};

const state = {
  cameraStream: null,
  cameraTrack: null,
  session: null,
  referenceSpace: null,
  gl: null,
  store: null,
  captureId: null,
  recording: false,
  captureBusy: false,
  nextFrameId: 1,
  depthFrames: 0,
  lastCaptureTime: Number.NEGATIVE_INFINITY,
  lastPose: null,
  startedAt: 0,
  lastStatusTick: 0,
  intrinsics: null,
  depthUsage: "cpu-optimized",
  depthDataFormat: "unknown",
  depthType: "unknown",
  previousDownloadUrl: null,
  xrSupported: false,
};

function setStatus(message, kind = "info") {
  ui.status.textContent = message;
  ui.status.dataset.kind = kind;
}

function setError(message = "") {
  ui.error.textContent = message;
  ui.error.hidden = !message;
}

function setCapability(element, label, stateName) {
  element.textContent = label;
  element.dataset.state = stateName;
}

function updateCounters() {
  ui.frameCount.textContent = String(state.nextFrameId - 1);
  ui.depthCount.textContent = String(state.depthFrames);
  if (state.recording) {
    ui.elapsed.textContent = `${Math.max(0, (performance.now() - state.startedAt) / 1000).toFixed(1)} s`;
  }
}

async function detectCapabilities() {
  if (window.isSecureContext && navigator.mediaDevices?.getUserMedia) {
    setCapability(ui.cameraCapability, "カメラ: 利用可能", "ok");
  } else {
    setCapability(ui.cameraCapability, "カメラ: HTTPSが必要", "error");
  }

  if (!navigator.xr) {
    setCapability(ui.xrCapability, "WebXR: 非対応", "error");
    setCapability(ui.depthCapability, "Depth: 判定不能", "error");
    setStatus("ChromeのWebXR対応環境で開いてください", "error");
    return;
  }

  try {
    state.xrSupported = await navigator.xr.isSessionSupported("immersive-ar");
  } catch (error) {
    state.xrSupported = false;
    console.warn("WebXR support check failed", error);
  }
  if (state.xrSupported) {
    setCapability(ui.xrCapability, "WebXR AR: 利用可能", "ok");
    setCapability(ui.depthCapability, "Depth: 収録開始時に確認", "pending");
    setStatus("カメラを準備してからAR収録を開始してください");
  } else {
    setCapability(ui.xrCapability, "WebXR AR: この端末では非対応", "error");
    setCapability(ui.depthCapability, "Depth: 利用不可", "error");
    setStatus("この端末のブラウザではAR収録を開始できません", "error");
  }
}

async function startCamera() {
  setError();
  if (state.cameraStream) return;
  if (!window.isSecureContext) {
    throw new Error("カメラはHTTPS上でのみ利用できます。GitHub PagesのURLで開いてください。");
  }
  state.cameraStream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: {
      facingMode: { ideal: "environment" },
      width: { ideal: 1280 },
      height: { ideal: 720 },
      frameRate: { ideal: 30, max: 30 },
    },
  });
  state.cameraTrack = state.cameraStream.getVideoTracks()[0] || null;
  ui.preview.srcObject = state.cameraStream;
  await ui.preview.play();
  if (!ui.preview.videoWidth || !ui.preview.videoHeight) {
    await new Promise((resolve) => {
      ui.preview.addEventListener("loadedmetadata", resolve, { once: true });
    });
  }
  const width = Math.min(1280, ui.preview.videoWidth);
  const height = Math.round((ui.preview.videoHeight / ui.preview.videoWidth) * width);
  ui.camera.width = width;
  ui.camera.height = height;
  ui.cameraCapability.textContent = `カメラ: ${ui.preview.videoWidth}×${ui.preview.videoHeight}`;
  ui.startCamera.disabled = true;
  if (state.xrSupported) ui.startCapture.disabled = false;
  setStatus("カメラ準備完了。部屋を一周できる場所でAR収録を開始してください");
}

function rowMajorMatrix(columnMajor) {
  const result = [];
  for (let row = 0; row < 4; row += 1) {
    for (let column = 0; column < 4; column += 1) {
      result.push(Number(columnMajor[column * 4 + row]));
    }
  }
  return result;
}

function posePosition(matrix) {
  return [matrix[3], matrix[7], matrix[11]];
}

function distance3(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

function rotationAngleDegrees(first, second) {
  let trace = 0;
  for (let row = 0; row < 3; row += 1) {
    for (let column = 0; column < 3; column += 1) {
      trace += first[row * 4 + column] * second[column * 4 + row];
    }
  }
  const cosine = Math.max(-1, Math.min(1, (trace - 1) / 2));
  return (Math.acos(cosine) * 180) / Math.PI;
}

function shouldCapture(time, matrix) {
  if (!state.lastPose) return true;
  const elapsed = time - state.lastCaptureTime;
  const translation = distance3(posePosition(matrix), posePosition(state.lastPose));
  const rotation = rotationAngleDegrees(matrix, state.lastPose);
  return elapsed >= 500 || translation >= 0.03 || rotation >= 3;
}

function deriveIntrinsics(view) {
  const width = ui.camera.width;
  const height = ui.camera.height;
  const projection = view.projectionMatrix || [];
  const fx = Math.abs(Number(projection[0])) * width / 2;
  const fy = Math.abs(Number(projection[5])) * height / 2;
  const cxFromProjection = (1 - Number(projection[8])) * width / 2;
  const cyFromProjection = (1 + Number(projection[9])) * height / 2;
  return {
    width,
    height,
    fx: Number.isFinite(fx) && fx > 0 ? fx : width * 0.9,
    fy: Number.isFinite(fy) && fy > 0 ? fy : height * 0.9,
    cx: Number.isFinite(cxFromProjection) ? cxFromProjection : width / 2,
    cy: Number.isFinite(cyFromProjection) ? cyFromProjection : height / 2,
    distortion_model: "none",
    distortion: [],
    source: "WebXR projection matrix scaled to captured video",
  };
}

function depthToMillimeters(depthInfo, dataFormat) {
  if (!depthInfo?.data) throw new Error("ブラウザからCPU深度バッファを取得できませんでした");
  const raw = dataFormat === "float32" ? new Float32Array(depthInfo.data) : new Uint16Array(depthInfo.data);
  const values = new Uint16Array(depthInfo.width * depthInfo.height);
  const scale = Number(depthInfo.rawValueToMeters || 1);
  for (let index = 0; index < values.length; index += 1) {
    const meters = Number(raw[index]) * scale;
    values[index] = Number.isFinite(meters) && meters > 0 ? Math.min(65535, Math.round(meters * 1000)) : 0;
  }
  return values;
}

function derivedConfidence(depthMillimeters) {
  const confidence = new Uint8Array(depthMillimeters.length);
  for (let index = 0; index < depthMillimeters.length; index += 1) {
    confidence[index] = depthMillimeters[index] > 0 ? 255 : 0;
  }
  return confidence;
}

function canvasBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("画像をJPEGへ変換できませんでした"));
    }, type, quality);
  });
}

async function captureFrame(time, pose, view, depthInfo) {
  const frameId = state.nextFrameId;
  const padded = String(frameId).padStart(8, "0");
  const rowMajorPose = rowMajorMatrix(pose.transform.matrix);
  const depthMillimeters = depthToMillimeters(depthInfo, state.depthDataFormat);
  const confidence = derivedConfidence(depthMillimeters);
  if (!state.intrinsics) state.intrinsics = deriveIntrinsics(view);

  const context = ui.camera.getContext("2d", { alpha: false });
  context.drawImage(ui.preview, 0, 0, ui.camera.width, ui.camera.height);
  const imageBlob = await canvasBlob(ui.camera, "image/jpeg", 0.88);
  const depthBytes = encodePngGray16(depthMillimeters, depthInfo.width, depthInfo.height);
  const confidenceBytes = encodePngGray8(confidence, depthInfo.width, depthInfo.height);
  const record = {
    frame_id: frameId,
    timestamp_ns: Math.max(0, Math.round((time - state.startedAt) * 1_000_000)),
    image: `images/${padded}.jpg`,
    depth: `depth/${padded}.png`,
    confidence: `confidence/${padded}.png`,
    pose_c2w: rowMajorPose,
    tracking_state: "TRACKING",
    image_timestamp_ns: Math.max(0, Math.round((time - state.startedAt) * 1_000_000)),
    depth_timestamp_ns: Math.max(0, Math.round((time - state.startedAt) * 1_000_000)),
    metadata: {
      source: "webxr-depth-sensing",
      depth_width: depthInfo.width,
      depth_height: depthInfo.height,
      depth_data_format: state.depthDataFormat,
      depth_type: state.depthType,
      confidence_source: "derived_from_depth_validity",
    },
  };
  await state.store.queueFrame(
    record,
    imageBlob,
    new Blob([depthBytes], { type: "image/png" }),
    new Blob([confidenceBytes], { type: "image/png" }),
  );
  state.nextFrameId += 1;
  state.depthFrames += 1;
  state.lastCaptureTime = time;
  state.lastPose = rowMajorPose;
  updateCounters();
}

function onXRFrame(time, frame) {
  if (!state.session || !state.recording) return;
  state.session.requestAnimationFrame(onXRFrame);
  const layer = state.session.renderState.baseLayer;
  if (layer && state.gl) {
    state.gl.bindFramebuffer(state.gl.FRAMEBUFFER, layer.framebuffer);
    state.gl.clearColor(0, 0, 0, 0);
    state.gl.clear(state.gl.COLOR_BUFFER_BIT | state.gl.DEPTH_BUFFER_BIT);
  }
  const pose = frame.getViewerPose(state.referenceSpace);
  if (!pose || !pose.views.length) return;
  const view = pose.views[0];
  let depthInfo;
  try {
    depthInfo = frame.getDepthInformation(view);
  } catch (error) {
    setError(`Depth取得に失敗しました: ${error.message || error}`);
    return;
  }
  if (!depthInfo) {
    if (state.depthFrames === 0) setStatus("Depthを待っています。床や壁が見えるようにゆっくり動かしてください", "pending");
    return;
  }
  const matrix = rowMajorMatrix(pose.transform.matrix);
  if (!state.captureBusy && shouldCapture(time, matrix)) {
    state.captureBusy = true;
    captureFrame(time, pose, view, depthInfo)
      .catch((error) => setError(error.message || String(error)))
      .finally(() => {
        state.captureBusy = false;
      });
  }
  if (time - state.lastStatusTick > 1000) {
    state.lastStatusTick = time;
    setStatus("収録中…部屋の外周・家具・床・天井をゆっくり撮影してください", "recording");
    updateCounters();
  }
}

async function beginXRSession() {
  if (!state.cameraStream) await startCamera();
  if (!state.xrSupported) throw new Error("このブラウザはimmersive-arに対応していません");
  if (!window.XRWebGLLayer) throw new Error("WebXRのWebGLレイヤーが利用できません");

  const sessionInit = {
    requiredFeatures: ["depth-sensing"],
    optionalFeatures: ["local-floor", "dom-overlay"],
    depthSensing: {
      usagePreference: ["cpu-optimized"],
      dataFormatPreference: ["luminance-alpha", "unsigned-short", "float32"],
      depthTypeRequest: ["raw", "smooth"],
      matchDepthView: true,
    },
  };
  sessionInit.domOverlay = { root: document.body };
  const session = await navigator.xr.requestSession("immersive-ar", sessionInit);
  const gl = ui.xrCanvas.getContext("webgl", { xrCompatible: true, preserveDrawingBuffer: false });
  if (!gl) {
    await session.end();
    throw new Error("WebGLが利用できません");
  }
  await gl.makeXRCompatible();
  session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl) });
  let referenceSpace;
  try {
    referenceSpace = await session.requestReferenceSpace("local-floor");
  } catch {
    referenceSpace = await session.requestReferenceSpace("local");
  }

  state.session = session;
  state.referenceSpace = referenceSpace;
  state.gl = gl;
  state.depthUsage = session.depthUsage || "cpu-optimized";
  state.depthDataFormat = session.depthDataFormat || "unknown";
  state.depthType = session.depthType || "unknown";
  state.captureId = crypto.randomUUID ? crypto.randomUUID() : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  state.store = new CaptureStore(state.captureId);
  await state.store.begin({
    captureId: state.captureId,
    complete: false,
    created_at: new Date().toISOString(),
  });
  state.recording = true;
  state.captureBusy = false;
  state.nextFrameId = 1;
  state.depthFrames = 0;
  state.lastPose = null;
  state.lastCaptureTime = Number.NEGATIVE_INFINITY;
  state.startedAt = performance.now();
  state.lastStatusTick = 0;
  state.intrinsics = null;
  session.addEventListener("end", () => {
    state.session = null;
    if (state.recording) setStatus("ARセッションが終了しました。停止ボタンでZIPを確定してください", "pending");
  });
  setCapability(ui.depthCapability, `Depth: ${state.depthDataFormat} / ${state.depthType}`, "ok");
  ui.startCamera.disabled = true;
  ui.startCapture.disabled = true;
  ui.stopCapture.disabled = false;
  ui.instructions.textContent = "収録中は画面を見ながらゆっくり歩き、同じ場所を複数方向から撮影してください。ブラウザを閉じないでください。";
  setStatus("収録中…Depthが安定するまで数秒待ってから歩き始めてください", "recording");
  session.requestAnimationFrame(onXRFrame);
}

async function exportCapture() {
  if (!state.store) throw new Error("収録データがありません");
  await state.store.flush();
  const storedFrames = await state.store.getFrames();
  if (storedFrames.length < 2) {
    throw new Error("有効なフレームが2枚未満です。カメラを部屋へ向けてもう少し長く撮影してください");
  }
  const entries = [];
  const addEntry = (name, data) => entries.push({ name, data });
  for (const stored of storedFrames) {
    addEntry(stored.record.image, new Uint8Array(await stored.imageBlob.arrayBuffer()));
    addEntry(stored.record.depth, new Uint8Array(await stored.depthBlob.arrayBuffer()));
    addEntry(stored.record.confidence, new Uint8Array(await stored.confidenceBlob.arrayBuffer()));
  }

  const framesText = `${storedFrames.map((frame) => JSON.stringify(frame.record)).join("\n")}\n`;
  const manifest = {
    format: "roomcap",
    format_version: 1,
    capture_id: state.captureId,
    created_at: new Date().toISOString(),
    complete: true,
    device: {
      user_agent: navigator.userAgent,
      platform: navigator.userAgentData?.platform || navigator.platform || "unknown",
      source: "browser-spa",
    },
    capabilities: {
      rgb: true,
      pose: true,
      raw_depth: true,
      depth_confidence: true,
    },
    coordinate_system: {
      pose: "camera_to_world",
      camera_forward: "-Z",
      up_axis: "Y",
      units: "m",
    },
    source: {
      transport: "WebXR immersive-ar + getUserMedia",
      depth_usage: state.depthUsage,
      depth_data_format: state.depthDataFormat,
      depth_type: state.depthType,
      confidence_source: "derived_from_depth_validity",
    },
    files: {
      frames: "frames.jsonl",
      intrinsics: "intrinsics.json",
      checksums: "checksums.sha256",
    },
    capture_stats: {
      frames: storedFrames.length,
      depth_frames: storedFrames.length,
    },
  };
  addEntry("manifest.json", utf8Bytes(`${JSON.stringify(manifest, null, 2)}\n`));
  addEntry("intrinsics.json", utf8Bytes(`${JSON.stringify(state.intrinsics, null, 2)}\n`));
  addEntry("frames.jsonl", utf8Bytes(framesText));

  const checksums = [];
  for (const entry of entries) {
    checksums.push(`${await sha256Hex(entry.data)}  ${entry.name}`);
  }
  addEntry("checksums.sha256", utf8Bytes(`${checksums.join("\n")}\n`));
  return buildZip(entries);
}

async function stopCapture() {
  if (!state.store) return;
  state.recording = false;
  ui.stopCapture.disabled = true;
  setStatus("収録を確定しています…端末内データをZIPにまとめています", "pending");
  try {
    if (state.session) await state.session.end();
  } catch (error) {
    console.warn("XR session end failed", error);
  }
  try {
    const zip = await exportCapture();
    if (state.previousDownloadUrl) URL.revokeObjectURL(state.previousDownloadUrl);
    state.previousDownloadUrl = URL.createObjectURL(zip);
    ui.download.href = state.previousDownloadUrl;
    ui.download.download = `roomtrace-${state.captureId}.roomcap.zip`;
    ui.download.hidden = false;
    setStatus(`ZIP作成完了: ${state.nextFrameId - 1}フレーム。リンクを押してPCへ転送してください`, "ok");
    ui.startCapture.disabled = !state.xrSupported;
  } catch (error) {
    setError(error.message || String(error));
    setStatus("ZIP作成に失敗しました", "error");
    ui.startCapture.disabled = !state.xrSupported;
  }
  state.session = null;
  state.referenceSpace = null;
}

ui.startCamera.addEventListener("click", () => {
  startCamera().catch((error) => {
    setError(error.message || String(error));
    setStatus("カメラを準備できませんでした", "error");
  });
});

ui.startCapture.addEventListener("click", () => {
  beginXRSession().catch((error) => {
    setError(`${error.message || error}\n\nこのページはカメラだけの収録には切り替えません。Depthが取れないデータはPC側でメッシュ化できないためです。`);
    setStatus("このブラウザでは実用収録を開始できません", "error");
    setCapability(ui.depthCapability, "Depth: この環境では利用不可", "error");
  });
});

ui.stopCapture.addEventListener("click", () => {
  stopCapture();
});

window.addEventListener("beforeunload", (event) => {
  if (state.recording) {
    event.preventDefault();
    event.returnValue = "収録中です。ページを閉じるとZIPを作成できません。";
  }
});

if ("serviceWorker" in navigator && (window.isSecureContext || location.hostname === "localhost")) {
  navigator.serviceWorker.register("./sw.js").catch((error) => console.warn("service worker registration failed", error));
}

detectCapabilities();
updateCounters();
