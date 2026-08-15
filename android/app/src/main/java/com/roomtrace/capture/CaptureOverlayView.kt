package com.roomtrace.capture

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Typeface
import android.util.AttributeSet
import android.view.View
import kotlin.math.max
import kotlin.math.min

class CaptureOverlayView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {
    private val panelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(195, 16, 20, 24) }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 30f; typeface = Typeface.create(Typeface.DEFAULT, Typeface.NORMAL) }
    private val smallPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.LTGRAY; textSize = 23f }
    private val pathPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.rgb(117, 227, 154); style = Paint.Style.STROKE; strokeWidth = 5f; strokeCap = Paint.Cap.ROUND }
    private var stats = RenderStats("PAUSED", "NONE", 0, 0, false, 0f, 0f, 0f, "Point the rear camera at the room")
    private val trajectory = ArrayDeque<Pair<Float, Float>>()

    fun update(value: RenderStats) {
        stats = value
        if (value.trackingState == "TRACKING") {
            if (trajectory.isEmpty() || distance(trajectory.last(), value.positionX to value.positionZ) > 0.015f) {
                trajectory.addLast(value.positionX to value.positionZ)
                while (trajectory.size > 600) trajectory.removeFirst()
            }
        }
        postInvalidateOnAnimation()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val panelHeight = 170f
        canvas.drawRect(0f, 0f, width.toFloat(), panelHeight, panelPaint)
        val trackingColor = when (stats.trackingState) {
            "TRACKING" -> Color.rgb(117, 227, 154)
            "PAUSED" -> Color.rgb(255, 184, 107)
            else -> Color.rgb(255, 118, 118)
        }
        textPaint.color = trackingColor
        canvas.drawText("${stats.trackingState}  ·  ${stats.saved} frames", 24f, 42f, textPaint)
        textPaint.color = Color.WHITE
        val depthLabel = if (stats.depthAvailable) "Raw Depth ready" else "Waiting for Raw Depth"
        canvas.drawText(depthLabel, 24f, 78f, smallPaint)
        canvas.drawText("${stats.message}  ·  dropped ${stats.dropped}", 24f, 110f, smallPaint)
        if (stats.trackingState == "PAUSED") {
            canvas.drawText("Tracking reason: ${stats.trackingReason}", 24f, 142f, smallPaint)
        }
        drawTrajectory(canvas)
    }

    private fun drawTrajectory(canvas: Canvas) {
        if (trajectory.size < 2) return
        val points = trajectory.toList()
        val minX = points.minOf { it.first }
        val maxX = points.maxOf { it.first }
        val minY = points.minOf { it.second }
        val maxY = points.maxOf { it.second }
        val scale = min(0.7f * width / max(0.2f, maxX - minX), 0.7f * height / max(0.2f, maxY - minY))
        val originX = width * 0.82f
        val originY = height * 0.72f
        val path = Path()
        points.forEachIndexed { index, point ->
            val x = originX + (point.first - (minX + maxX) / 2f) * scale
            val y = originY + (point.second - (minY + maxY) / 2f) * scale
            if (index == 0) path.moveTo(x, y) else path.lineTo(x, y)
        }
        canvas.drawPath(path, pathPaint)
    }

    private fun distance(a: Pair<Float, Float>, b: Pair<Float, Float>): Float {
        val dx = a.first - b.first
        val dy = a.second - b.second
        return kotlin.math.sqrt(dx * dx + dy * dy)
    }
}

