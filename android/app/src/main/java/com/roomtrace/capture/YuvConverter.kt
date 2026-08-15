package com.roomtrace.capture

import android.graphics.Bitmap
import java.io.File
import java.io.FileOutputStream
import kotlin.math.max
import kotlin.math.min

object YuvConverter {
    fun writeJpeg(planes: Array<PlanePayload>, file: File, quality: Int = 94) {
        require(planes.size >= 3) { "YUV_420_888 requires three planes" }
        val yPlane = planes[0]
        val uPlane = planes[1]
        val vPlane = planes[2]
        val width = yPlane.width
        val height = yPlane.height
        val pixels = IntArray(width * height)
        for (y in 0 until height) {
            for (x in 0 until width) {
                val yValue = yPlane.sample(x, y)
                val uValue = uPlane.sample(x / 2, y / 2)
                val vValue = vPlane.sample(x / 2, y / 2)
                val yy = max(0, yValue - 16) * 1.164f
                val red = (yy + 1.596f * (vValue - 128)).toInt().coerceIn(0, 255)
                val green = (yy - 0.392f * (uValue - 128) - 0.813f * (vValue - 128)).toInt().coerceIn(0, 255)
                val blue = (yy + 2.017f * (uValue - 128)).toInt().coerceIn(0, 255)
                pixels[y * width + x] = (0xFF shl 24) or (red shl 16) or (green shl 8) or blue
            }
        }
        val bitmap = Bitmap.createBitmap(pixels, width, height, Bitmap.Config.ARGB_8888)
        FileOutputStream(file).use { stream -> bitmap.compress(Bitmap.CompressFormat.JPEG, quality, stream) }
        bitmap.recycle()
    }

    private fun PlanePayload.sample(x: Int, y: Int): Int {
        val px = min(width - 1, max(0, x))
        val py = min(height - 1, max(0, y))
        val index = py * rowStride + px * pixelStride
        return if (index in bytes.indices) bytes[index].toInt() and 0xFF else 128
    }
}

