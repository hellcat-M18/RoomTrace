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
  captureHud: document.querySelector("#capture-hud"),
  captureHudStatus: document.querySelector("#capture-hud-status"),
  coverageOverlay: document.querySelector("#coverage-overlay"),
  coverageMap: document.querySelector("#coverage-map"),
  coverageSummary: document.querySelector("#coverage-summary"),
  finishCapture: document.querySelector("#finish-capture"),
  cancelCapture: document.querySelector("#cancel-capture"),
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
  captureTrail: [],
  coveragePoints: [],
  coverageVoxels: new Set(),
  lastCoverageRenderTime: 0,
};

const COVERAGE_POINT_COLUMNS = 12;
const COVERAGE_POINT_ROWS = 9;
const MAX_COVERAGE_POINTS_PER_RENDER = 5000;
const COVERAGE_POINT_SIZE_CSS_PIXELS = 3;

function setStatus(message, kind = "info") {
  ui.status.textContent = message;
  ui.status.dataset.kind = kind;
  ui.captureHudStatus.textContent = message;
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
  const frames = String(state.nextFrameId - 1);
  const depth = String(state.depthFrames);
  ui.frameCount.textContent = frames;
  ui.depthCount.textContent = depth;
  if (state.recording) {
    const elapsed = `${Math.max(0, (performance.now() - state.startedAt) / 1000).toFixed(1)} s`;
    ui.elapsed.textContent = elapsed;
    ui.coverageSummary.textContent = state.coveragePoints.length
      ? `緑の点群: ${state.coveragePoints.length}点・${frames}枚`
      : `Depthを待っています・${elapsed}`;
  }
}

function setCaptureMode(active) {
  document.body.classList.toggle("capture-mode", active);
  ui.captureHud.hidden = !active;
  if (active) {
    resizeCoverageOverlay();
    drawCoverageMap();
  } else {
    clearCoverageOverlay();
  }
}

function resizeCoverageOverlay() {
  const scale = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(window.innerWidth * scale));
  const height = Math.max(1, Math.round(window.innerHeight * scale));
  if (ui.coverageOverlay.width !== width || ui.coverageOverlay.height !== height) {
    ui.coverageOverlay.width = width;
    ui.coverageOverlay.height = height;
  }
  return scale;
}

function clearCoverageOverlay() {
  const context = ui.coverageOverlay.getContext("2d");
  context.clearRect(0, 0, ui.coverageOverlay.width, ui.coverageOverlay.height);
}

function rememberCoveragePoints(depthMillimeters, depthInfo, cameraToWorld, view) {
  const projection = view.projectionMatrix;
  if (!projection?.length) return;
  const width = depthInfo.width;
  const height = depthInfo.height;
  const fx = Math.abs(Number(projection[0])) * width / 2;
  const fy = Math.abs(Number(projection[5])) * height / 2;
  const cx = (1 - Number(projection[8])) * width / 2;
  const cy = (1 + Number(projection[9])) * height / 2;
  if (![fx, fy, cx, cy].every(Number.isFinite) || fx <= 0 || fy <= 0) return;

  const columns = COVERAGE_POINT_COLUMNS;
  const rows = COVERAGE_POINT_ROWS;
  const voxelSize = 0.09;
  for (let row = 0; row < rows; row += 1) {
    const y = Math.min(height - 1, Math.floor((row + 0.5) * height / rows));
    for (let column = 0; column < columns; column += 1) {
      const x = Math.min(width - 1, Math.floor((column + 0.5) * width / columns));
      const meters = depthMillimeters[y * width + x] / 1000;
      if (!Number.isFinite(meters) || meters < 0.18 || meters > 8) continue;
      const cameraX = ((x + 0.5 - cx) / fx) * meters;
      const cameraY = ((y + 0.5 - cy) / fy) * meters;
      const cameraZ = -meters;
      const worldX = cameraToWorld[0] * cameraX + cameraToWorld[4] * cameraY + cameraToWorld[8] * cameraZ + cameraToWorld[12];
      const worldY = cameraToWorld[1] * cameraX + cameraToWorld[5] * cameraY + cameraToWorld[9] * cameraZ + cameraToWorld[13];
      const worldZ = cameraToWorld[2] * cameraX + cameraToWorld[6] * cameraY + cameraToWorld[10] * cameraZ + cameraToWorld[14];
      const voxelKey = `${Math.round(worldX / voxelSize)},${Math.round(worldY / voxelSize)},${Math.round(worldZ / voxelSize)}`;
      if (state.coverageVoxels.has(voxelKey)) continue;
      state.coverageVoxels.add(voxelKey);
      state.coveragePoints.push({ x: worldX, y: worldY, z: worldZ });
    }
  }
}

function renderCoverageOverlay(time, pose, view) {
  if (time - state.lastCoverageRenderTime < 90) return;
  state.lastCoverageRenderTime = time;
  const scale = resizeCoverageOverlay();
  const canvas = ui.coverageOverlay;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  if (!state.coveragePoints.length) return;
  const worldToCamera = pose.transform.inverse?.matrix;
  const projection = view.projectionMatrix;
  if (!worldToCamera?.length || !projection?.length) return;

  context.save();
  context.globalCompositeOperation = "screen";
  context.fillStyle = "rgb(110 231 183 / 72%)";
  const stride = Math.max(1, Math.ceil(state.coveragePoints.length / MAX_COVERAGE_POINTS_PER_RENDER));
  const pointSize = scale * COVERAGE_POINT_SIZE_CSS_PIXELS;
  for (let index = 0; index < state.coveragePoints.length; index += stride) {
    const point = state.coveragePoints[index];
    const cameraX = worldToCamera[0] * point.x + worldToCamera[4] * point.y + worldToCamera[8] * point.z + worldToCamera[12];
    const cameraY = worldToCamera[1] * point.x + worldToCamera[5] * point.y + worldToCamera[9] * point.z + worldToCamera[13];
    const cameraZ = worldToCamera[2] * point.x + worldToCamera[6] * point.y + worldToCamera[10] * point.z + worldToCamera[14];
    const clipW = projection[3] * cameraX + projection[7] * cameraY + projection[11] * cameraZ + projection[15];
    if (!Number.isFinite(clipW) || clipW <= 0) continue;
    const normalizedX = (projection[0] * cameraX + projection[4] * cameraY + projection[8] * cameraZ + projection[12]) / clipW;
    const normalizedY = (projection[1] * cameraX + projection[5] * cameraY + projection[9] * cameraZ + projection[13]) / clipW;
    if (Math.abs(normalizedX) > 1 || Math.abs(normalizedY) > 1) continue;
    const screenX = (normalizedX * 0.5 + 0.5) * canvas.width;
    const screenY = (0.5 - normalizedY * 0.5) * canvas.height;
    context.fillRect(screenX - pointSize / 2, screenY - pointSize / 2, pointSize, pointSize);
  }
  context.restore();
}

function trailPoint(matrix) {
  const [x, , z] = posePosition(matrix);
  return { x, z, dx: -matrix[2], dz: -matrix[10] };
}

function drawCoverageMap() {
  const canvas = ui.coverageMap;
  const context = canvas.getContext("2d");
  const size = canvas.width;
  context.clearRect(0, 0, size, size);
  context.fillStyle = "#101821";
  context.fillRect(0, 0, size, size);
  context.strokeStyle = "rgb(148 163 184 / 16%)";
  context.lineWidth = 1;
  for (let offset = 20; offset < size; offset += 20) {
    context.beginPath();
    context.moveTo(offset, 0);
    context.lineTo(offset, size);
    context.moveTo(0, offset);
    context.lineTo(size, offset);
    context.stroke();
  }

  const points = state.captureTrail;
  if (!points.length) {
    context.fillStyle = "#8ea1b5";
    context.font = "11px system-ui";
    context.textAlign = "center";
    context.fillText("Depth待機中", size / 2, size / 2 + 4);
    return;
  }

  const xs = points.map((point) => point.x);
  const zs = points.map((point) => point.z);
  const minX = Math.min(...xs, 0);
  const maxX = Math.max(...xs, 0);
  const minZ = Math.min(...zs, 0);
  const maxZ = Math.max(...zs, 0);
  const span = Math.max(maxX - minX, maxZ - minZ, 0.8);
  const padding = 20;
  const scale = (size - padding * 2) / span;
  const centerX = (minX + maxX) / 2;
  const centerZ = (minZ + maxZ) / 2;
  const project = (point) => [
    size / 2 + (point.x - centerX) * scale,
    size / 2 - (point.z - centerZ) * scale,
  ];

  context.strokeStyle = "#7dd3fc";
  context.lineWidth = 2;
  context.lineJoin = "round";
  context.beginPath();
  points.forEach((point, index) => {
    const [x, y] = project(point);
    if (index) context.lineTo(x, y);
    else context.moveTo(x, y);
  });
  context.stroke();

  const [startX, startY] = project(points[0]);
  context.fillStyle = "#6ee7b7";
  context.beginPath();
  context.arc(startX, startY, 4, 0, Math.PI * 2);
  context.fill();
  context.fillStyle = "#d1fae5";
  context.font = "bold 10px system-ui";
  context.textAlign = "center";
  context.fillText("S", startX, startY - 7);

  const current = points[points.length - 1];
  const [currentX, currentY] = project(current);
  const heading = Math.atan2(-current.dz, current.dx);
  context.save();
  context.translate(currentX, currentY);
  context.rotate(heading);
  context.fillStyle = "#f8fafc";
  context.beginPath();
  context.moveTo(8, 0);
  context.lineTo(-6, 5);
  context.lineTo(-3, 0);
  context.lineTo(-6, -5);
  context.closePath();
  context.fill();
  context.restore();
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

function depthFrameGeometry(depthInfo, pose, view) {
  const depthTransform = depthInfo.transform?.matrix || pose.transform.matrix;
  const depthProjection = depthInfo.projectionMatrix || view.projectionMatrix;
  const depthBufferFromView = depthInfo.normDepthBufferFromNormView?.matrix || [
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1,
  ];
  return {
    depth_pose_c2w: rowMajorMatrix(depthTransform),
    depth_projection_matrix: rowMajorMatrix(depthProjection),
    norm_depth_buffer_from_norm_view: rowMajorMatrix(depthBufferFromView),
    depth_coordinate_system: "webxr-depth-view-v1",
    rgb_registration: "unregistered_getUserMedia",
  };
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
  const store = state.store;
  if (!store || !state.recording) return;
  const frameId = state.nextFrameId;
  const padded = String(frameId).padStart(8, "0");
  const rowMajorPose = rowMajorMatrix(pose.transform.matrix);
  const depthMillimeters = depthToMillimeters(depthInfo, state.depthDataFormat);
  const confidence = derivedConfidence(depthMillimeters);
  const depthGeometry = depthFrameGeometry(depthInfo, pose, view);
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
      ...depthGeometry,
    },
  };
  if (!state.recording || state.store !== store) return;
  await store.queueFrame(
    record,
    imageBlob,
    new Blob([depthBytes], { type: "image/png" }),
    new Blob([confidenceBytes], { type: "image/png" }),
  );
  if (!state.recording || state.store !== store) return;
  state.nextFrameId += 1;
  state.depthFrames += 1;
  state.lastCaptureTime = time;
  state.lastPose = rowMajorPose;
  state.captureTrail.push(trailPoint(rowMajorPose));
  if (state.captureTrail.length > 1200) state.captureTrail.shift();
  rememberCoveragePoints(depthMillimeters, depthInfo, pose.transform.matrix, view);
  drawCoverageMap();
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
  renderCoverageOverlay(time, pose, view);
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
  state.captureTrail = [];
  state.coveragePoints = [];
  state.coverageVoxels.clear();
  state.lastCoverageRenderTime = 0;
  session.addEventListener("end", () => {
    state.session = null;
    if (state.recording) {
      setCaptureMode(false);
      setStatus("ARセッションが終了しました。停止してZIP化を押すと保存できます", "pending");
    }
  });
  setCapability(ui.depthCapability, `Depth: ${state.depthDataFormat} / ${state.depthType}`, "ok");
  ui.startCamera.disabled = true;
  ui.startCapture.disabled = true;
  ui.stopCapture.disabled = false;
  ui.instructions.textContent = "収録中は画面を見ながらゆっくり歩き、同じ場所を複数方向から撮影してください。ブラウザを閉じないでください。";
  setStatus("収録中…Depthが安定するまで数秒待ってから歩き始めてください", "recording");
  setCaptureMode(true);
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
      depth_view_geometry: true,
      rgb_registered_to_depth: false,
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
      depth_coordinate_system: "webxr-depth-view-v1",
      rgb_registration: "unregistered_getUserMedia",
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
  setCaptureMode(false);
}

async function cancelCapture() {
  if (!state.store || !state.recording) {
    setCaptureMode(false);
    return;
  }
  if (!window.confirm("今回の収録を破棄して通常画面へ戻ります。保存していないフレームは失われます。")) return;
  state.recording = false;
  ui.stopCapture.disabled = true;
  try {
    if (state.session) await state.session.end();
  } catch (error) {
    console.warn("XR session end failed", error);
  }
  try {
    await state.store.discard();
  } catch (error) {
    console.warn("discarding capture data failed", error);
  }
  state.store = null;
  state.session = null;
  state.referenceSpace = null;
  state.captureTrail = [];
  state.coveragePoints = [];
  state.coverageVoxels.clear();
  ui.startCapture.disabled = !state.xrSupported;
  setCaptureMode(false);
  setStatus("収録を中止しました。保存前のデータは破棄しました");
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

ui.finishCapture.addEventListener("click", () => {
  stopCapture();
});

ui.cancelCapture.addEventListener("click", () => {
  cancelCapture();
});

window.addEventListener("resize", () => {
  if (state.recording) resizeCoverageOverlay();
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
