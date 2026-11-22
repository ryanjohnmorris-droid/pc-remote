package com.clutch.clips

import android.media.MediaCodec
import java.nio.ByteBuffer

/**
 * The replay buffer: encoded video and audio samples kept in one fixed block of
 * memory outside the Java heap. New samples overwrite the oldest ones, so the
 * buffer always holds the most recent stretch of gameplay.
 */
class SampleRing(private val capacity: Int) {

    class Meta(
        val seq: Long,
        val offset: Int,
        val size: Int,
        val ptsUs: Long,
        val flags: Int,
        val track: Int,
    ) {
        val isKey get() = flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
    }

    private val buf: ByteBuffer = ByteBuffer.allocateDirect(capacity)
    private val samples = ArrayDeque<Meta>()
    private var writePos = 0
    private var nextSeq = 0L

    @Synchronized
    fun add(src: ByteBuffer, ptsUs: Long, flags: Int, track: Int) {
        val size = src.remaining()
        if (size <= 0 || size > capacity / 4) return
        var pos = writePos
        if (pos + size > capacity) {
            // Not enough room before the end: the tail holds the oldest data, drop it and wrap.
            while (samples.isNotEmpty() && samples.first().offset >= pos) samples.removeFirst()
            pos = 0
        }
        while (samples.isNotEmpty()) {
            val head = samples.first()
            val overlaps = head.offset < pos + size && head.offset + head.size > pos
            if (!overlaps) break
            samples.removeFirst()
        }
        val dst = buf.duplicate()
        dst.position(pos)
        dst.put(src)
        samples.addLast(Meta(nextSeq++, pos, size, ptsUs, flags, track))
        writePos = pos + size
    }

    @Synchronized
    fun clear() {
        samples.clear()
        writePos = 0
    }

    @Synchronized
    fun snapshot(): List<Meta> = samples.toList()

    /** Copies one sample into [dst]. Returns false if it was already overwritten. */
    @Synchronized
    fun copy(meta: Meta, dst: ByteBuffer): Boolean {
        val head = samples.firstOrNull() ?: return false
        if (meta.seq < head.seq) return false
        val src = buf.duplicate()
        src.limit(meta.offset + meta.size)
        src.position(meta.offset)
        dst.clear()
        if (dst.capacity() < meta.size) return false
        dst.put(src)
        dst.flip()
        return true
    }

    companion object {
        const val TRACK_VIDEO = 0
        const val TRACK_AUDIO = 1
    }
}
