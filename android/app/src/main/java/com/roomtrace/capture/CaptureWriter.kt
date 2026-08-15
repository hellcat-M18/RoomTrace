package com.roomtrace.capture

import android.content.Context
import android.os.Build
import android.os.StatFs
import android.util.Log
import com.google.ar.core.Pose
import com.google.ar.core.CameraConfig
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStreamWriter
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ArrayBlockingQueue
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

data class PlanePayload(
    val bytes: ByteArray,
    val rowStride: Int,
    val pixelStride: Int,
    val width: Int,
    val height: Int,
)

data class Gray16Payload(val bytesLittleEndian: ByteArray, val width: Int, val height: Int)
data class Gray8Payload(val bytes: ByteArray, val width: Int, val height: Int)

data class IntrinsicsPayload(
    val width: Int,
    val height: Int,
    val fx: Float,
    val fy: Float,
    val cx: Float,
    val cy: Float,
)

data class CapturePayload(
    val timestampNs: Long,
    val imageTimestampNs: Long,
    val poseRowMajor: FloatArray,
    val trackingState: String,
    val image: Array<PlanePayload>,
    val intrinsics: IntrinsicsPayload,
    val depth: Gray16Payload?,
    val confidence: Gray8Payload?,
    val depthTimestampNs: Long?,
    val metadata: JSONObject? = null,
)

data class WriterStats(
    val saved: Int,
    val dropped: Int,
    val depthFrames: Int,
    val queueSize: Int,
    val freeBytes: Long,
    val error: String? = null,
)

private sealed interface QueueItem {
    data class Frame(val payload: CapturePayload) : QueueItem
    data object Stop : QueueItem
}

/** Crash-tolerant writer for the RoomTrace v1 directory format. */
class CaptureWriter(
    private val context: Context,
    private val depthSupported: Boolean,
    private val cameraConfig: CameraConfig?,
    private val onStats: (WriterStats) -> Unit,
) {
    private val queue = ArrayBlockingQueue<QueueItem>(4)
    private var root: File? = null
    private var framesWriter: BufferedWriter? = null
    private var worker: Thread? = null
    private var accepting = false
    private var saved = 0
    private var dropped = 0
    private var depthFrames = 0
    private var writerError: String? = null
    private var captureId = ""

    fun start() {
        check(!accepting) { "capture is already running" }
        check(worker?.isAlive != true) { "previous capture is still finalizing" }
        queue.clear()
        saved = 0
        dropped = 0
        depthFrames = 0
        writerError = null
        val timestamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        val base = File(context.getExternalFilesDir(null), "roomtrace")
        var suffix = 0
        var directory: File
        do {
            captureId = "room-$timestamp" + if (suffix == 0) "" else "-$suffix"
            directory = File(base, "$captureId.roomcap")
            suffix += 1
        } while (directory.exists())
        directory.mkdirs()
        File(directory, "images").mkdirs()
        File(directory, "depth").mkdirs()
        File(directory, "confidence").mkdirs()
        root = directory
        framesWriter = BufferedWriter(OutputStreamWriter(FileOutputStream(File(directory, "frames.jsonl"), false), Charsets.UTF_8))
        writeManifest(directory, complete = false)
        accepting = true
        worker = Thread({ drainQueue() }, "RoomTraceCaptureWriter").also { it.start() }
        publishStats()
    }

    fun enqueue(payload: CapturePayload): Boolean {
        if (!accepting) return false
        val accepted = queue.offer(QueueItem.Frame(payload))
        if (!accepted) {
            dropped += 1
            publishStats()
        }
        return accepted
    }

    fun finishAsync(onFinished: (File?, String?) -> Unit) {
        if (!accepting) {
            onFinished(null, "capture is not running")
            return
        }
        accepting = false
        Thread({
            val activeWorker = worker
            if (activeWorker?.isAlive == true) {
                queue.put(QueueItem.Stop)
                activeWorker.join(120_000)
                if (activeWorker.isAlive) throw IllegalStateException("capture writer did not finish within 120 seconds")
            } else {
                queue.clear()
            }
            val result = try {
                finalizeCapture()
            } catch (error: Exception) {
                writerError = error.message ?: error.javaClass.simpleName
                null
            }
            onFinished(if (writerError == null) result else null, writerError)
        }, "RoomTraceCaptureFinalizer").start()
    }

    fun currentStats(): WriterStats = WriterStats(saved, dropped, depthFrames, queue.size, freeBytes(), writerError)

    private fun drainQueue() {
        try {
            while (true) {
                when (val item = queue.take()) {
                    is QueueItem.Frame -> writeFrame(item.payload)
                    QueueItem.Stop -> break
                }
            }
            framesWriter?.flush()
            framesWriter?.close()
        } catch (error: Exception) {
            writerError = error.message ?: error.javaClass.simpleName
            Log.e("RoomTrace", "capture writer failed", error)
            try {
                framesWriter?.close()
            } catch (_: Exception) {
            }
        } finally {
            publishStats()
        }
    }

    private fun writeFrame(payload: CapturePayload) {
        val directory = requireNotNull(root)
        val number = saved + 1
        val stem = "%08d".format(Locale.US, number)
        val imageFile = File(directory, "images/$stem.jpg")
        YuvConverter.writeJpeg(payload.image, imageFile)
        val depthFile = payload.depth?.let {
            val file = File(directory, "depth/$stem.png")
            PngWriter.writeGray16(file, it.width, it.height, it.bytesLittleEndian)
            file
        }
        val confidenceFile = payload.confidence?.let {
            val file = File(directory, "confidence/$stem.png")
            PngWriter.writeGray8(file, it.width, it.height, it.bytes)
            file
        }
        if (!File(directory, "intrinsics.json").exists()) {
            val intrinsics = JSONObject()
                .put("width", payload.intrinsics.width)
                .put("height", payload.intrinsics.height)
                .put("fx", payload.intrinsics.fx)
                .put("fy", payload.intrinsics.fy)
                .put("cx", payload.intrinsics.cx)
                .put("cy", payload.intrinsics.cy)
                .put("distortion_model", "none")
                .put("distortion", JSONArray())
            File(directory, "intrinsics.json").writeText(intrinsics.toString(2), Charsets.UTF_8)
        }
        val poseArray = JSONArray()
        payload.poseRowMajor.forEach { poseArray.put(it.toDouble()) }
        val frame = JSONObject()
            .put("frame_id", number)
            .put("timestamp_ns", payload.timestampNs)
            .put("image_timestamp_ns", payload.imageTimestampNs)
            .put("image", "images/$stem.jpg")
            .put("pose_c2w", poseArray)
            .put("tracking_state", payload.trackingState)
        depthFile?.let { frame.put("depth", "depth/$stem.png") }
        confidenceFile?.let { frame.put("confidence", "confidence/$stem.png") }
        payload.depthTimestampNs?.let { frame.put("depth_timestamp_ns", it) }
        payload.metadata?.let { frame.put("metadata", it) }
        framesWriter?.apply {
            write(frame.toString())
            newLine()
            flush()
        }
        saved += 1
        if (payload.depth != null) depthFrames += 1
        publishStats()
    }

    private fun writeManifest(directory: File, complete: Boolean) {
        val manifest = JSONObject()
            .put("format", "roomcap")
            .put("format_version", 1)
            .put("capture_id", captureId)
            .put("created_at", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).format(Date()))
            .put("complete", complete)
            .put("device", JSONObject().put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL).put("android_api", Build.VERSION.SDK_INT))
            .put("camera_config", cameraConfig?.let { config ->
                JSONObject()
                    .put("camera_id", config.cameraId)
                    .put("cpu_width", config.imageSize.width)
                    .put("cpu_height", config.imageSize.height)
                    .put("texture_width", config.textureSize.width)
                    .put("texture_height", config.textureSize.height)
                    .put("fps_min", config.fpsRange.lower)
                    .put("fps_max", config.fpsRange.upper)
            } ?: JSONObject())
            .put("capabilities", JSONObject().put("rgb", true).put("pose", true).put("raw_depth", depthSupported).put("depth_confidence", depthSupported))
            .put("coordinate_system", JSONObject().put("pose", "camera_to_world").put("camera_forward", "-Z").put("up_axis", "Y").put("units", "m"))
            .put("files", JSONObject().put("frames", "frames.jsonl").put("intrinsics", "intrinsics.json").put("checksums", "checksums.sha256"))
        File(directory, "manifest.json").writeText(manifest.toString(2), Charsets.UTF_8)
    }

    private fun finalizeCapture(): File {
        val directory = requireNotNull(root)
        writeManifest(directory, complete = true)
        val checksums = StringBuilder()
        directory.walkTopDown().filter { it.isFile && it.name != "checksums.sha256" }.sortedBy { it.relativeTo(directory).path }.forEach { file ->
            checksums.append(sha256(file)).append("  ").append(file.relativeTo(directory).path.replace(File.separatorChar, '/')).append('\n')
        }
        File(directory, "checksums.sha256").writeText(checksums.toString(), Charsets.UTF_8)
        val zipFile = File(directory.parentFile, "$captureId.roomcap.zip")
        ZipOutputStream(FileOutputStream(zipFile)).use { zip ->
            directory.walkTopDown().filter { it.isFile }.forEach { file ->
                val entry = ZipEntry(file.relativeTo(directory).path.replace(File.separatorChar, '/'))
                zip.putNextEntry(entry)
                file.inputStream().use { it.copyTo(zip) }
                zip.closeEntry()
            }
        }
        publishStats()
        return zipFile
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { stream ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = stream.read(buffer)
                if (count <= 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun freeBytes(): Long {
        val path = root ?: context.getExternalFilesDir(null) ?: return 0L
        return StatFs(path.absolutePath).availableBytes
    }

    private fun publishStats() = onStats(currentStats())
}

fun copyPlane(plane: android.media.Image.Plane, width: Int, height: Int): PlanePayload {
    val source = plane.buffer.duplicate()
    source.position(0)
    val bytes = ByteArray(source.remaining())
    source.get(bytes)
    return PlanePayload(bytes, plane.rowStride, plane.pixelStride, width, height)
}

fun copyDepth(image: android.media.Image): Gray16Payload {
    val plane = image.planes[0]
    val source = plane.buffer.duplicate()
    source.position(0)
    val raw = ByteArray(source.remaining())
    source.get(raw)
    val output = ByteArray(image.width * image.height * 2)
    for (y in 0 until image.height) {
        for (x in 0 until image.width) {
            val from = y * plane.rowStride + x * plane.pixelStride
            val to = (y * image.width + x) * 2
            if (from + 1 < raw.size) {
                output[to] = raw[from]
                output[to + 1] = raw[from + 1]
            }
        }
    }
    return Gray16Payload(output, image.width, image.height)
}

fun copyConfidence(image: android.media.Image): Gray8Payload {
    val plane = image.planes[0]
    val source = plane.buffer.duplicate()
    source.position(0)
    val raw = ByteArray(source.remaining())
    source.get(raw)
    val output = ByteArray(image.width * image.height)
    for (y in 0 until image.height) {
        for (x in 0 until image.width) {
            val from = y * plane.rowStride + x * plane.pixelStride
            output[y * image.width + x] = if (from in raw.indices) raw[from] else 0
        }
    }
    return Gray8Payload(output, image.width, image.height)
}

fun poseToRowMajor(pose: Pose): FloatArray {
    val columnMajor = FloatArray(16)
    pose.toMatrix(columnMajor, 0)
    return FloatArray(16) { index -> columnMajor[(index % 4) * 4 + index / 4] }
}
