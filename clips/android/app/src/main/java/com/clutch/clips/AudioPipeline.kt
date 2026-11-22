package com.clutch.clips

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.util.Log
import java.nio.ByteOrder
import kotlin.math.min

/**
 * Records game sound (what the phone is playing) and/or the microphone, mixes
 * them into one stereo track and encodes it to AAC into the replay buffer.
 */
class AudioPipeline(
    private val context: Context,
    private val projection: MediaProjection,
    private val gameAudio: Boolean,
    private val mic: Boolean,
    private val ring: SampleRing,
    private val onFormat: (MediaFormat) -> Unit,
) {
    @Volatile private var running = false
    private var thread: Thread? = null
    private var micThread: Thread? = null

    fun start() {
        if (!gameAudio && !mic) return
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "No audio permission: recording video only")
            return
        }
        running = true
        thread = Thread({ runSafely() }, "clutch-audio").apply { start() }
    }

    fun stop() {
        running = false
        thread?.join(1500)
        micThread?.join(1500)
    }

    private fun runSafely() {
        try {
            run()
        } catch (e: Exception) {
            Log.e(TAG, "Audio stopped", e)
        }
    }

    @SuppressLint("MissingPermission")
    private fun createGameRecord(): AudioRecord? = try {
        val config = AudioPlaybackCaptureConfiguration.Builder(projection)
            .addMatchingUsage(AudioAttributes.USAGE_GAME)
            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
            .build()
        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
            .build()
        val minSize = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT)
        AudioRecord.Builder()
            .setAudioFormat(format)
            .setBufferSizeInBytes(maxOf(minSize * 2, 32 * 1024))
            .setAudioPlaybackCaptureConfig(config)
            .build()
            .takeIf { it.state == AudioRecord.STATE_INITIALIZED }
    } catch (e: Exception) {
        Log.w(TAG, "Game sound not available", e)
        null
    }

    @SuppressLint("MissingPermission")
    private fun createMicRecord(): AudioRecord? = try {
        val minSize = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        AudioRecord(
            MediaRecorder.AudioSource.MIC, RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(minSize * 2, 16 * 1024),
        ).takeIf { it.state == AudioRecord.STATE_INITIALIZED }
    } catch (e: Exception) {
        Log.w(TAG, "Microphone not available", e)
        null
    }

    private fun run() {
        val game = if (gameAudio) createGameRecord() else null
        val micRec = if (mic) createMicRecord() else null
        if (game == null && micRec == null) return

        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, RATE, 2).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, 160_000)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16 * 1024)
        }
        val encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
        encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        encoder.start()

        val fifo = ShortFifo(RATE) // one second of mic audio
        game?.startRecording()
        micRec?.startRecording()
        if (game != null && micRec != null) {
            // Game sound sets the pace; the mic is read on its own thread and mixed in.
            micThread = Thread({
                val tmp = ShortArray(FRAMES)
                while (running) {
                    val n = micRec.read(tmp, 0, tmp.size)
                    if (n > 0) fifo.write(tmp, n)
                }
            }, "clutch-mic").apply { start() }
        }

        val out = ShortArray(FRAMES * 2)
        val gameBuf = ShortArray(FRAMES * 2)
        val micBuf = ShortArray(FRAMES)
        val info = MediaCodec.BufferInfo()
        val clock = Clock()

        while (running) {
            var frames: Int
            if (game != null) {
                val n = game.read(gameBuf, 0, gameBuf.size)
                if (n <= 0) continue
                frames = n / 2
                System.arraycopy(gameBuf, 0, out, 0, frames * 2)
                if (micRec != null) {
                    val m = fifo.read(micBuf, frames)
                    for (i in 0 until m) {
                        out[2 * i] = clamp(out[2 * i] + micBuf[i])
                        out[2 * i + 1] = clamp(out[2 * i + 1] + micBuf[i])
                    }
                }
            } else {
                val n = micRec!!.read(micBuf, 0, FRAMES)
                if (n <= 0) continue
                frames = n
                for (i in 0 until frames) {
                    out[2 * i] = micBuf[i]
                    out[2 * i + 1] = micBuf[i]
                }
            }
            clock.onChunk(frames)
            feed(encoder, out, frames, clock, info)
            drain(encoder, info)
        }

        runCatching { game?.stop(); game?.release() }
        micThread?.join(1000)
        runCatching { micRec?.stop(); micRec?.release() }
        runCatching { encoder.stop(); encoder.release() }
    }

    /** Gives each chunk a timestamp on the same clock as the video, closing gaps when capture stalls. */
    private class Clock {
        private var baseUs = -1L
        var frames = 0L
            private set

        fun onChunk(chunkFrames: Int) {
            val chunkUs = chunkFrames * 1_000_000L / RATE
            val nowStartUs = System.nanoTime() / 1000 - chunkUs
            if (baseUs < 0) baseUs = nowStartUs
            val expected = ptsUs(0)
            if (nowStartUs - expected > 200_000) baseUs += nowStartUs - expected
        }

        fun ptsUs(extraFrames: Int) = baseUs + (frames + extraFrames) * 1_000_000L / RATE
        fun advance(n: Int) { frames += n }
    }

    private fun feed(encoder: MediaCodec, data: ShortArray, frames: Int, clock: Clock, info: MediaCodec.BufferInfo) {
        var offset = 0 // in shorts
        val total = frames * 2
        while (offset < total && running) {
            val index = encoder.dequeueInputBuffer(10_000)
            if (index < 0) {
                drain(encoder, info)
                continue
            }
            val buffer = encoder.getInputBuffer(index) ?: continue
            buffer.clear()
            buffer.order(ByteOrder.nativeOrder())
            val count = min(total - offset, (buffer.remaining() / 4) * 2)
            buffer.asShortBuffer().put(data, offset, count)
            encoder.queueInputBuffer(index, 0, count * 2, clock.ptsUs(0), 0)
            clock.advance(count / 2)
            offset += count
        }
    }

    private fun drain(encoder: MediaCodec, info: MediaCodec.BufferInfo) {
        while (true) {
            val index = encoder.dequeueOutputBuffer(info, 0)
            when {
                index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> onFormat(encoder.outputFormat)
                index >= 0 -> {
                    val out = encoder.getOutputBuffer(index)
                    if (out != null && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        out.position(info.offset)
                        out.limit(info.offset + info.size)
                        ring.add(out, info.presentationTimeUs, info.flags, SampleRing.TRACK_AUDIO)
                    }
                    encoder.releaseOutputBuffer(index, false)
                }
                else -> return
            }
        }
    }

    private fun clamp(v: Int): Short = v.coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt()).toShort()

    /** A small thread-safe queue of mic samples. */
    private class ShortFifo(private val capacity: Int) {
        private val data = ShortArray(capacity)
        private var read = 0
        private var count = 0

        @Synchronized
        fun write(src: ShortArray, n: Int) {
            for (i in 0 until n) {
                data[(read + count) % capacity] = src[i]
                if (count < capacity) count++ else read = (read + 1) % capacity
            }
        }

        @Synchronized
        fun read(dst: ShortArray, n: Int): Int {
            val m = min(n, count)
            for (i in 0 until m) {
                dst[i] = data[read]
                read = (read + 1) % capacity
            }
            count -= m
            return m
        }
    }

    companion object {
        private const val TAG = "ClutchAudio"
        const val RATE = 48_000
        private const val FRAMES = 1024
    }
}
