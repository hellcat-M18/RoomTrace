package com.roomtrace.capture

import java.io.File
import java.io.ByteArrayOutputStream
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.zip.Deflater
import java.util.zip.CRC32

object PngWriter {
    private val signature = byteArrayOf(137.toByte(), 80, 78, 71, 13, 10, 26, 10)

    fun writeGray8(file: File, width: Int, height: Int, pixels: ByteArray) {
        require(pixels.size >= width * height)
        val rows = ByteArray((width + 1) * height)
        for (y in 0 until height) {
            rows[y * (width + 1)] = 0
            System.arraycopy(pixels, y * width, rows, y * (width + 1) + 1, width)
        }
        write(file, width, height, 8, rows)
    }

    fun writeGray16(file: File, width: Int, height: Int, pixelsLittleEndian: ByteArray) {
        require(pixelsLittleEndian.size >= width * height * 2)
        val rowBytes = width * 2
        val rows = ByteArray((rowBytes + 1) * height)
        for (y in 0 until height) {
            val row = y * (rowBytes + 1)
            rows[row] = 0
            for (x in 0 until width) {
                val source = (y * width + x) * 2
                // PNG stores multi-byte samples big-endian; ARCore depth is LE.
                rows[row + 1 + x * 2] = pixelsLittleEndian[source + 1]
                rows[row + 1 + x * 2 + 1] = pixelsLittleEndian[source]
            }
        }
        write(file, width, height, 16, rows)
    }

    private fun write(file: File, width: Int, height: Int, bitDepth: Int, scanlines: ByteArray) {
        val compressed = Deflater(Deflater.BEST_SPEED).run {
            setInput(scanlines)
            finish()
            val output = ByteArrayOutputStream(scanlines.size / 2)
            val buffer = ByteArray(16 * 1024)
            while (!finished()) output.write(buffer, 0, deflate(buffer))
            end()
            output.toByteArray()
        }
        FileOutputStream(file).use { stream ->
            stream.write(signature)
            val header = ByteBuffer.allocate(13).order(ByteOrder.BIG_ENDIAN)
                .putInt(width).putInt(height).put(bitDepth.toByte()).put(0.toByte()).put(0.toByte()).put(0.toByte()).put(0.toByte()).array()
            chunk(stream, "IHDR", header)
            chunk(stream, "IDAT", compressed)
            chunk(stream, "IEND", ByteArray(0))
        }
    }

    private fun chunk(stream: FileOutputStream, type: String, data: ByteArray) {
        val typeBytes = type.toByteArray(Charsets.US_ASCII)
        val length = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(data.size).array()
        stream.write(length)
        stream.write(typeBytes)
        stream.write(data)
        val crc = CRC32()
        crc.update(typeBytes)
        crc.update(data)
        stream.write(ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(crc.value.toInt()).array())
    }
}
