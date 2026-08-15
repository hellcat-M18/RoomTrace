package com.roomtrace.capture

import android.media.Image
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import com.google.ar.core.Camera
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Frame
import com.google.ar.core.ImageMetadata
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.NotYetAvailableException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.concurrent.atomic.AtomicBoolean
import org.json.JSONObject
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10
import kotlin.math.acos
import kotlin.math.max
import kotlin.math.min

data class RenderStats(
    val trackingState: String,
    val trackingReason: String,
    val saved: Int,
    val dropped: Int,
    val depthAvailable: Boolean,
    val depthRatio: Float,
    val positionX: Float,
    val positionZ: Float,
    val message: String,
)

class RoomTraceRenderer(
    private val session: Session,
    private val writer: CaptureWriter,
    private val displayRotation: () -> Int,
    private val onStats: (RenderStats) -> Unit,
) : GLSurfaceView.Renderer {
    private val capturing = AtomicBoolean(false)
    private var cameraTextureId = 0
    private var program = 0
    private var positionHandle = 0
    private var texCoordHandle = 0
    private var textureHandle = 0
    private var viewWidth = 1
    private var viewHeight = 1
    private val ndcCoordinates = floatArrayOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f)
    private val textureCoordinates = FloatArray(8)
    private var positions: FloatBuffer = floatBuffer(ndcCoordinates)
    private var texCoords: FloatBuffer = floatBuffer(textureCoordinates)
    private var lastSavedTimestamp = 0L
    private var lastSavedPose: FloatArray? = null
    private var lastDepthTimestamp = -1L
    private var lastDepthAvailable = false
    private var currentMessage = "Move slowly so ARCore can track the room"
    private var textureCoordinatesReady = false

    fun setCapturing(value: Boolean) {
        capturing.set(value)
        if (!value) {
            lastSavedTimestamp = 0L
            lastSavedPose = null
        }
    }

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        GLES20.glClearColor(0f, 0f, 0f, 1f)
        cameraTextureId = createExternalTexture()
        session.setCameraTextureName(cameraTextureId)
        program = createProgram(VERTEX_SHADER, FRAGMENT_SHADER)
        positionHandle = GLES20.glGetAttribLocation(program, "aPosition")
        texCoordHandle = GLES20.glGetAttribLocation(program, "aTexCoord")
        textureHandle = GLES20.glGetUniformLocation(program, "sTexture")
    }

    override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
        viewWidth = max(1, width)
        viewHeight = max(1, height)
        GLES20.glViewport(0, 0, viewWidth, viewHeight)
        session.setDisplayGeometry(displayRotation(), viewWidth, viewHeight)
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)
        val frame = try {
            session.update()
        } catch (error: Exception) {
            currentMessage = error.message ?: "ARCore update failed"
            publishStats(null, TrackingState.PAUSED)
            return
        }
        if (!textureCoordinatesReady || frame.hasDisplayGeometryChanged()) {
            session.setDisplayGeometry(displayRotation(), viewWidth, viewHeight)
            frame.transformCoordinates2d(
                Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES,
                ndcCoordinates,
                Coordinates2d.TEXTURE_NORMALIZED,
                textureCoordinates,
            )
            texCoords = floatBuffer(textureCoordinates)
            textureCoordinatesReady = true
        }
        drawCamera()
        val camera = frame.camera
        if (capturing.get() && camera.trackingState == TrackingState.TRACKING && shouldSave(camera, frame.timestamp)) {
            captureFrame(frame, camera)
        }
        publishStats(camera, camera.trackingState)
    }

    private fun shouldSave(camera: Camera, timestampNs: Long): Boolean {
        val pose = poseToRowMajor(camera.pose)
        if (lastSavedPose == null) return true
        val elapsed = (timestampNs - lastSavedTimestamp) / 1_000_000_000.0
        val translation = distance3(pose, lastSavedPose!!)
        val rotation = rotationDegrees(pose, lastSavedPose!!)
        return elapsed >= 0.5 || translation >= 0.03f || rotation >= 3.0f
    }

    private fun captureFrame(frame: Frame, camera: Camera) {
        var cameraImage: Image? = null
        var depthImage: Image? = null
        var confidenceImage: Image? = null
        try {
            cameraImage = frame.acquireCameraImage()
            val planes = cameraImage.planes.map { copyPlane(it, cameraImage!!.width, cameraImage!!.height) }.toTypedArray()
            var depthTimestampForFrame: Long? = null
            val depth = try {
                depthImage = frame.acquireRawDepthImage16Bits()
                val value = copyDepth(depthImage!!)
                lastDepthTimestamp = depthImage!!.timestamp
                depthTimestampForFrame = lastDepthTimestamp
                lastDepthAvailable = true
                value
            } catch (_: NotYetAvailableException) {
                lastDepthAvailable = false
                null
            } catch (_: IllegalStateException) {
                lastDepthAvailable = false
                null
            } catch (_: Exception) {
                lastDepthAvailable = false
                null
            }
            val confidence = if (depth != null) {
                try {
                    confidenceImage = frame.acquireRawDepthConfidenceImage()
                    copyConfidence(confidenceImage!!)
                } catch (_: Exception) {
                    null
                }
            } else null
            val intrinsics = camera.imageIntrinsics
            val dimensions = intrinsics.imageDimensions
            val focal = intrinsics.focalLength
            val principal = intrinsics.principalPoint
            val timestamp = frame.timestamp
            val payload = CapturePayload(
                timestampNs = timestamp,
                imageTimestampNs = frame.androidCameraTimestamp,
                poseRowMajor = poseToRowMajor(camera.pose),
                trackingState = camera.trackingState.name,
                image = planes,
                intrinsics = IntrinsicsPayload(dimensions[0], dimensions[1], focal[0], focal[1], principal[0], principal[1]),
                depth = depth,
                confidence = confidence,
                depthTimestampNs = depthTimestampForFrame,
                metadata = readMetadata(frame),
            )
            if (writer.enqueue(payload)) {
                lastSavedTimestamp = timestamp
                lastSavedPose = payload.poseRowMajor
            }
            currentMessage = if (depth == null) "RGB saved; waiting for Raw Depth" else "Capturing RGB + Raw Depth"
        } catch (error: NotYetAvailableException) {
            currentMessage = "Camera image is not ready; slowing capture"
        } catch (error: Exception) {
            currentMessage = error.message ?: "Frame capture failed"
        } finally {
            cameraImage?.close()
            depthImage?.close()
            confidenceImage?.close()
        }
    }

    private fun drawCamera() {
        if (program == 0 || cameraTextureId == 0) return
        GLES20.glUseProgram(program)
        positions.position(0)
        texCoords.position(0)
        GLES20.glEnableVertexAttribArray(positionHandle)
        GLES20.glVertexAttribPointer(positionHandle, 2, GLES20.GL_FLOAT, false, 0, positions)
        GLES20.glEnableVertexAttribArray(texCoordHandle)
        GLES20.glVertexAttribPointer(texCoordHandle, 2, GLES20.GL_FLOAT, false, 0, texCoords)
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, cameraTextureId)
        GLES20.glUniform1i(textureHandle, 0)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
        GLES20.glDisableVertexAttribArray(positionHandle)
        GLES20.glDisableVertexAttribArray(texCoordHandle)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, 0)
    }

    private fun readMetadata(frame: Frame): JSONObject? {
        return try {
            val metadata = frame.imageMetadata
            val keys = metadata.keys.toSet()
            val value = JSONObject()
            if (ImageMetadata.SENSOR_EXPOSURE_TIME.toLong() in keys) value.put("sensor_exposure_time_ns", metadata.getLong(ImageMetadata.SENSOR_EXPOSURE_TIME))
            if (ImageMetadata.SENSOR_SENSITIVITY.toLong() in keys) value.put("sensor_sensitivity", metadata.getInt(ImageMetadata.SENSOR_SENSITIVITY))
            if (ImageMetadata.LENS_FOCAL_LENGTH.toLong() in keys) value.put("lens_focal_length_mm", metadata.getFloat(ImageMetadata.LENS_FOCAL_LENGTH))
            if (ImageMetadata.LENS_FOCUS_DISTANCE.toLong() in keys) value.put("lens_focus_distance", metadata.getFloat(ImageMetadata.LENS_FOCUS_DISTANCE))
            if (ImageMetadata.JPEG_ORIENTATION.toLong() in keys) value.put("jpeg_orientation", metadata.getInt(ImageMetadata.JPEG_ORIENTATION))
            if (value.length() == 0) null else value
        } catch (_: Exception) {
            null
        }
    }

    private fun publishStats(camera: Camera?, state: TrackingState) {
        val stats = writer.currentStats()
        val position = camera?.pose?.translation ?: floatArrayOf(0f, 0f, 0f)
        onStats(
            RenderStats(
                trackingState = state.name,
                trackingReason = camera?.trackingFailureReason?.name ?: "NONE",
                saved = stats.saved,
                dropped = stats.dropped,
                depthAvailable = lastDepthAvailable,
                depthRatio = if (stats.saved == 0) 0f else stats.depthFrames.toFloat() / stats.saved.toFloat(),
                positionX = position[0],
                positionZ = position[2],
                message = stats.error ?: currentMessage,
            ),
        )
    }

    private fun createExternalTexture(): Int {
        val texture = IntArray(1)
        GLES20.glGenTextures(1, texture, 0)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, texture[0])
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)
        return texture[0]
    }

    companion object {
        private const val VERTEX_SHADER = """
            attribute vec4 aPosition;
            attribute vec2 aTexCoord;
            varying vec2 vTexCoord;
            void main() {
              gl_Position = aPosition;
              vTexCoord = aTexCoord;
            }
        """
        private const val FRAGMENT_SHADER = """
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            uniform samplerExternalOES sTexture;
            varying vec2 vTexCoord;
            void main() {
              gl_FragColor = texture2D(sTexture, vTexCoord);
            }
        """

        private fun floatBuffer(values: FloatArray): FloatBuffer = ByteBuffer.allocateDirect(values.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply {
            put(values)
            position(0)
        }

        private fun createProgram(vertex: String, fragment: String): Int {
            val vertexShader = compile(GLES20.GL_VERTEX_SHADER, vertex)
            val fragmentShader = compile(GLES20.GL_FRAGMENT_SHADER, fragment)
            val program = GLES20.glCreateProgram()
            GLES20.glAttachShader(program, vertexShader)
            GLES20.glAttachShader(program, fragmentShader)
            GLES20.glLinkProgram(program)
            val status = IntArray(1)
            GLES20.glGetProgramiv(program, GLES20.GL_LINK_STATUS, status, 0)
            check(status[0] == GLES20.GL_TRUE) { GLES20.glGetProgramInfoLog(program) }
            return program
        }

        private fun compile(type: Int, source: String): Int {
            val shader = GLES20.glCreateShader(type)
            GLES20.glShaderSource(shader, source)
            GLES20.glCompileShader(shader)
            val status = IntArray(1)
            GLES20.glGetShaderiv(shader, GLES20.GL_COMPILE_STATUS, status, 0)
            check(status[0] == GLES20.GL_TRUE) { GLES20.glGetShaderInfoLog(shader) }
            return shader
        }

        private fun distance3(a: FloatArray, b: FloatArray): Float = kotlin.math.sqrt(
            (a[3] - b[3]) * (a[3] - b[3]) + (a[7] - b[7]) * (a[7] - b[7]) + (a[11] - b[11]) * (a[11] - b[11]),
        )

        private fun rotationDegrees(a: FloatArray, b: FloatArray): Float {
            val trace = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[4] * b[4] + a[5] * b[5] + a[6] * b[6] + a[8] * b[8] + a[9] * b[9] + a[10] * b[10]
            return Math.toDegrees(acos(((trace - 1f) / 2f).coerceIn(-1f, 1f)).toDouble()).toFloat()
        }
    }
}
