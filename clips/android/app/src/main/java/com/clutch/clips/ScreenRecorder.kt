package com.clutch.clips

import android.content.Context
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.HandlerThread
import android.util.DisplayMetrics
import android.util.Log
import android.view.Display
import android.view.Surface
import java.io.File
import java.nio.ByteBuffer
import kotlin.math.max
import kotlin.math.min

/**
 * Mirrors the screen into a video encoder and keeps the encoded output in a
 * [SampleRing], so the last N seconds can be written to an MP4 at any moment.
 */
class ScreenRecorder(
    private val context: Context,
    private val projection: MediaProjection,
    private val config: Config,
) {
    data class Config(
        val clipSeconds: Int,
        val quality: Int,
        val fps: Int,
        val gameAudio: Boolean,
        val mic: Boolean,
    )

    private val thread = HandlerThread("clutch-video").apply { start() }
    private val handler = Handler(thread.looper)
    private val displayManager = context.getSystemService(DisplayManager::class.java)

    private val bitrate = when {
        config.quality >= 1080 -> if (config.fps >= 60) 10_000_000 else 7_000_000
        else -> if (config.fps >= 60) 7_000_000 else 5_000_000
    }

    // Room for the clip length plus a few extra seconds, with headroom for busy scenes.
    private val ring = SampleRing(
        min(((bitrate / 8L + 30_000L) * (config.clipSeconds + 4) * 5 / 4), 400L * 1024 * 1024).toInt(),
    )

    @Volatile private var codec: MediaCodec? = null
    private var inputSurface: Surface? = null
    private var display: VirtualDisplay? = null
    private var audio: AudioPipeline? = null
    @Volatile private var videoFormat: MediaFormat? = null
    @Volatile private var audioFormat: MediaFormat? = null
    private var width = 0
    private var height = 0
    private var dpi = 0
    private val saveLock = Any()

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(displayId: Int) {}
        override fun onDisplayRemoved(displayId: Int) {}
        override fun onDisplayChanged(displayId: Int) {
            if (displayId != Display.DEFAULT_DISPLAY) return
            val (w, h) = targetSize()
            if ((w > h) != (width > height)) reconfigure(w, h)
        }
    }

    fun start() {
        val (w, h) = targetSize()
        val surface = createCodec(w, h)
        display = projection.createVirtualDisplay(
            "Clutch", w, h, dpi, DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, surface, null, handler,
        )
        displayManager.registerDisplayListener(displayListener, handler)
        audio = AudioPipeline(context, projection, config.gameAudio, config.mic, ring) { audioFormat = it }
            .also { it.start() }
    }

    fun stop() {
        runCatching { displayManager.unregisterDisplayListener(displayListener) }
        audio?.stop()
        handler.post {
            runCatching { display?.release() }
            releaseCodec()
            thread.quitSafely()
        }
    }

    /** The real screen size, scaled so the shorter side matches the chosen quality. */
    @Suppress("DEPRECATION")
    private fun targetSize(): Pair<Int, Int> {
        val metrics = DisplayMetrics()
        displayManager.getDisplay(Display.DEFAULT_DISPLAY).getRealMetrics(metrics)
        dpi = metrics.densityDpi
        var scale = min(1.0, config.quality.toDouble() / min(metrics.widthPixels, metrics.heightPixels))
        val caps = runCatching {
            MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC).let { c ->
                c.codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC).videoCapabilities.also { c.release() }
            }
        }.getOrNull()
        while (true) {
            val w = align(metrics.widthPixels * scale)
            val h = align(metrics.heightPixels * scale)
            if (caps == null || caps.isSizeSupported(w, h) || scale < 0.3) return w to h
            scale *= 0.9
        }
    }

    private fun align(v: Double) = max(16, (v.toInt() / 16) * 16)

    private fun createCodec(w: Int, h: Int): Surface {
        width = w
        height = h
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, config.fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1) // a keyframe every second, so clips start close to the cut
            setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 1_000_000L / config.fps)
        }
        val c = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        c.setCallback(object : MediaCodec.Callback() {
            override fun onInputBufferAvailable(codec: MediaCodec, index: Int) {}

            override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
                if (codec !== this@ScreenRecorder.codec) return
                try {
                    val out = codec.getOutputBuffer(index)
                    if (out != null && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        out.position(info.offset)
                        out.limit(info.offset + info.size)
                        ring.add(out, info.presentationTimeUs, info.flags, SampleRing.TRACK_VIDEO)
                    }
                    codec.releaseOutputBuffer(index, false)
                } catch (e: IllegalStateException) {
                    // The encoder was stopped while this callback was queued.
                }
            }

            override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
                Log.e(TAG, "Video encoder error", e)
            }

            override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
                if (codec === this@ScreenRecorder.codec) videoFormat = format
            }
        }, handler)
        c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        val surface = c.createInputSurface()
        c.start()
        codec = c
        inputSurface = surface
        return surface
    }

    private fun releaseCodec() {
        val c = codec
        codec = null
        runCatching { c?.stop() }
        runCatching { c?.release() }
        runCatching { inputSurface?.release() }
        inputSurface = null
    }

    /** The phone turned between portrait and landscape: restart the encoder at the new shape. */
    private fun reconfigure(w: Int, h: Int) {
        synchronized(saveLock) {
            display?.surface = null
            releaseCodec()
            ring.clear()
            videoFormat = null
            val surface = createCodec(w, h)
            display?.resize(w, h, dpi)
            display?.surface = surface
        }
    }

    /**
     * Writes the last [Config.clipSeconds] seconds to [file]. Call from a background thread.
     * Returns the clip length in seconds, or null if there was nothing to save yet.
     */
    fun saveClip(file: File): Int? = synchronized(saveLock) {
        val vFormat = videoFormat ?: return null
        val aFormat = audioFormat
        val all = ring.snapshot()
        val videos = all.filter { it.track == SampleRing.TRACK_VIDEO }
        if (videos.isEmpty()) return null

        val endPts = videos.last().ptsUs
        val cutoff = endPts - config.clipSeconds * 1_000_000L
        var start = videos.indexOfLast { it.isKey && it.ptsUs <= cutoff }
        if (start < 0) start = videos.indexOfFirst { it.isKey }
        if (start < 0) return null
        val first = videos[start]
        val startPts = first.ptsUs

        val chosen = all.filter {
            if (it.track == SampleRing.TRACK_VIDEO) it.seq >= first.seq
            else aFormat != null && it.ptsUs in startPts..endPts
        }.sortedBy { it.ptsUs }
        val hasAudio = aFormat != null && chosen.any { it.track == SampleRing.TRACK_AUDIO }

        val tmp = File(file.parentFile, ".${file.name}.part")
        val muxer = MediaMuxer(tmp.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        var ok = false
        try {
            val vTrack = muxer.addTrack(vFormat)
            val aTrack = if (hasAudio) muxer.addTrack(aFormat!!) else -1
            muxer.start()
            val data = ByteBuffer.allocateDirect(chosen.maxOf { it.size })
            val info = MediaCodec.BufferInfo()
            var wroteVideo = false
            for (m in chosen) {
                if (m.track == SampleRing.TRACK_AUDIO && aTrack < 0) continue
                if (!ring.copy(m, data)) continue
                info.set(0, m.size, m.ptsUs - startPts, m.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME)
                muxer.writeSampleData(if (m.track == SampleRing.TRACK_VIDEO) vTrack else aTrack, data, info)
                if (m.track == SampleRing.TRACK_VIDEO) wroteVideo = true
            }
            muxer.stop()
            ok = wroteVideo
        } catch (e: Exception) {
            Log.e(TAG, "Saving clip failed", e)
        } finally {
            runCatching { muxer.release() }
        }
        if (!ok || !tmp.renameTo(file)) {
            tmp.delete()
            return null
        }
        return ((endPts - startPts) / 1_000_000L).toInt()
    }

    companion object {
        private const val TAG = "ClutchVideo"
    }
}
