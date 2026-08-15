package com.roomtrace.capture

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.Surface
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import com.google.ar.core.ArCoreApk
import com.google.ar.core.CameraConfig
import com.google.ar.core.CameraConfigFilter
import com.google.ar.core.Config
import com.google.ar.core.Session
import com.google.ar.core.exceptions.UnavailableException
import java.util.EnumSet

class MainActivity : Activity() {
    private lateinit var glView: android.opengl.GLSurfaceView
    private lateinit var overlay: CaptureOverlayView
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var note: TextView
    private var session: Session? = null
    private var writer: CaptureWriter? = null
    private var renderer: RoomTraceRenderer? = null
    private var installRequested = false
    private var started = false
    private var lifecycleResumed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        buildUi()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA)
        } else {
            initializeAr()
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_CAMERA && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) initializeAr()
        else note.text = "Camera permission is required to capture a room"
    }

    private fun buildUi() {
        val root = FrameLayout(this)
        glView = android.opengl.GLSurfaceView(this).apply {
            setEGLContextClientVersion(2)
            preserveEGLContextOnPause = true
        }
        root.addView(glView, FrameLayout.LayoutParams(-1, -1))
        overlay = CaptureOverlayView(this)
        root.addView(overlay, FrameLayout.LayoutParams(-1, -1))

        val controls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(20, 12, 20, 12)
            setBackgroundColor(Color.argb(225, 16, 20, 24))
        }
        startButton = Button(this).apply { text = "Start capture"; isEnabled = false; setOnClickListener { startCapture() } }
        stopButton = Button(this).apply { text = "Stop & share"; isEnabled = false; setOnClickListener { stopCapture() } }
        note = TextView(this).apply { text = "Checking ARCore…"; setTextColor(Color.WHITE); setPadding(14, 0, 14, 0); textSize = 14f }
        controls.addView(startButton, LinearLayout.LayoutParams(0, -2, 1f))
        controls.addView(stopButton, LinearLayout.LayoutParams(0, -2, 1f))
        controls.addView(note, LinearLayout.LayoutParams(0, -2, 1.2f))
        val controlsParams = FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM)
        root.addView(controls, controlsParams)
        setContentView(root)
    }

    private fun initializeAr() {
        try {
            val installStatus = ArCoreApk.getInstance().requestInstall(this, !installRequested)
            if (installStatus == ArCoreApk.InstallStatus.INSTALL_REQUESTED) {
                installRequested = true
                note.text = "Install ARCore, then return to RoomTrace"
                return
            }
            val arSession = Session(this)
            val depthSupported = configureSession(arSession)
            session = arSession
            writer = CaptureWriter(this, depthSupported, arSession.cameraConfig) { stats ->
                runOnUiThread { overlay.update(RenderStats("WRITER", "NONE", stats.saved, stats.dropped, stats.depthFrames > 0, if (stats.saved == 0) 0f else stats.depthFrames.toFloat() / stats.saved.toFloat(), 0f, 0f, stats.error ?: "Saving")) }
            }
            renderer = RoomTraceRenderer(arSession, requireNotNull(writer), { displayRotation() }) { stats ->
                runOnUiThread { overlay.update(stats) }
            }
            glView.setRenderer(requireNotNull(renderer))
            glView.renderMode = android.opengl.GLSurfaceView.RENDERMODE_CONTINUOUSLY
            if (lifecycleResumed) {
                arSession.resume()
                glView.onResume()
            }
            startButton.isEnabled = true
            note.text = if (depthSupported) "Raw Depth supported" else "RGB + pose only on this device"
        } catch (error: UnavailableException) {
            note.text = "ARCore unavailable: ${error.javaClass.simpleName}"
        } catch (error: Exception) {
            note.text = "Cannot start ARCore: ${error.message ?: error.javaClass.simpleName}"
        }
    }

    private fun configureSession(arSession: Session): Boolean {
        val filter = CameraConfigFilter(arSession)
            .setFacingDirection(CameraConfig.FacingDirection.BACK)
            .setTargetFps(EnumSet.of(CameraConfig.TargetFps.TARGET_FPS_30))
        val configs = arSession.getSupportedCameraConfigs(filter)
        val best = configs.maxByOrNull { it.imageSize.width * it.imageSize.height }
        if (best != null) arSession.cameraConfig = best
        val config = arSession.config
        config.updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
        config.focusMode = Config.FocusMode.AUTO
        val depthMode = when {
            arSession.isDepthModeSupported(Config.DepthMode.RAW_DEPTH_ONLY) -> Config.DepthMode.RAW_DEPTH_ONLY
            arSession.isDepthModeSupported(Config.DepthMode.AUTOMATIC) -> Config.DepthMode.AUTOMATIC
            else -> Config.DepthMode.DISABLED
        }
        config.depthMode = depthMode
        arSession.configure(config)
        return depthMode != Config.DepthMode.DISABLED
    }

    private fun startCapture() {
        if (started) return
        requireNotNull(writer).start()
        requireNotNull(renderer).setCapturing(true)
        started = true
        startButton.isEnabled = false
        stopButton.isEnabled = true
        note.text = "Capturing… move slowly around the room"
    }

    private fun stopCapture() {
        if (!started) return
        started = false
        requireNotNull(renderer).setCapturing(false)
        startButton.isEnabled = false
        stopButton.isEnabled = false
        note.text = "Finalizing capture…"
        requireNotNull(writer).finishAsync { zip, error ->
            runOnUiThread {
                startButton.isEnabled = error == null
                note.text = error?.let { "Capture failed: $it" } ?: "Capture ready to share"
                if (error == null && zip != null) share(zip)
            }
        }
    }

    private fun share(file: java.io.File) {
        val uri: Uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "application/zip"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(intent, "Send RoomTrace capture"))
    }

    private fun displayRotation(): Int = if (android.os.Build.VERSION.SDK_INT >= 30) display?.rotation ?: Surface.ROTATION_0 else legacyDisplayRotation()

    @Suppress("DEPRECATION")
    private fun legacyDisplayRotation(): Int = windowManager.defaultDisplay.rotation

    override fun onResume() {
        super.onResume()
        lifecycleResumed = true
        var initializedDuringResume = false
        if (session == null && installRequested && ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            initializeAr()
            initializedDuringResume = true
        }
        if (!initializedDuringResume) session?.let {
            try {
                it.resume()
                glView.onResume()
            } catch (error: Exception) {
                note.text = "ARCore resume failed: ${error.message ?: error.javaClass.simpleName}"
            }
        }
    }

    override fun onPause() {
        lifecycleResumed = false
        if (started) stopCapture()
        glView.onPause()
        session?.pause()
        super.onPause()
    }

    override fun onDestroy() {
        session?.close()
        super.onDestroy()
    }

    companion object {
        private const val REQUEST_CAMERA = 1101
    }
}
