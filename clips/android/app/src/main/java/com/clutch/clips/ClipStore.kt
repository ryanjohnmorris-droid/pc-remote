package com.clutch.clips

import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import android.media.MediaMuxer
import android.os.Environment
import android.provider.MediaStore
import androidx.core.content.FileProvider
import java.io.File
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Where clips live, plus trimming, thumbnails, sharing and saving to the Gallery. */
object ClipStore {

    fun dir(context: Context): File =
        File(context.getExternalFilesDir(Environment.DIRECTORY_MOVIES), "Clips").apply { mkdirs() }

    fun list(context: Context): List<File> =
        dir(context).listFiles { f -> f.isFile && f.extension == "mp4" && !f.name.startsWith(".") }
            ?.sortedByDescending { it.lastModified() } ?: emptyList()

    fun newClipFile(context: Context, base: String? = null): File {
        val name = base ?: ("Clip " + SimpleDateFormat("yyyy-MM-dd HH.mm.ss", Locale.US).format(Date()))
        var f = File(dir(context), "$name.mp4")
        var i = 2
        while (f.exists()) f = File(dir(context), "$name ($i).mp4").also { i++ }
        return f
    }

    fun durationMs(file: File): Long = runCatching {
        MediaMetadataRetriever().use {
            it.setDataSource(file.path)
            it.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLong() ?: 0L
        }
    }.getOrDefault(0L)

    /** A small picture from the clip, cached so the library loads fast. */
    fun thumbnail(context: Context, file: File): Bitmap? {
        val cache = File(context.cacheDir, "thumbs").apply { mkdirs() }
        val key = File(cache, "${file.name.hashCode()}_${file.length()}_${file.lastModified()}.jpg")
        if (key.exists()) BitmapFactory.decodeFile(key.path)?.let { return it }
        val bmp = runCatching {
            MediaMetadataRetriever().use {
                it.setDataSource(file.path)
                it.getScaledFrameAtTime(1_000_000, MediaMetadataRetriever.OPTION_CLOSEST_SYNC, 480, 480)
            }
        }.getOrNull() ?: return null
        runCatching { key.outputStream().use { bmp.compress(Bitmap.CompressFormat.JPEG, 80, it) } }
        return bmp
    }

    /** Copies the part of [src] between [startMs] and [endMs] into [dst] without re-encoding. */
    fun trim(src: File, dst: File, startMs: Long, endMs: Long): Boolean {
        val tmp = File(dst.parentFile, ".${dst.name}.part")
        val extractor = MediaExtractor()
        var muxer: MediaMuxer? = null
        return try {
            extractor.setDataSource(src.path)
            muxer = MediaMuxer(tmp.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            val rotation = runCatching {
                MediaMetadataRetriever().use {
                    it.setDataSource(src.path)
                    it.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION)?.toInt() ?: 0
                }
            }.getOrDefault(0)
            muxer.setOrientationHint(rotation)
            val trackMap = HashMap<Int, Int>()
            var maxSize = 2 * 1024 * 1024
            for (i in 0 until extractor.trackCount) {
                val format = extractor.getTrackFormat(i)
                val mime = format.getString(MediaFormat.KEY_MIME) ?: continue
                if (!mime.startsWith("video/") && !mime.startsWith("audio/")) continue
                extractor.selectTrack(i)
                trackMap[i] = muxer.addTrack(format)
                if (format.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)) {
                    maxSize = maxOf(maxSize, format.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE))
                }
            }
            if (trackMap.isEmpty()) return false
            val startUs = startMs * 1000
            val endUs = endMs * 1000
            extractor.seekTo(startUs, MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
            val base = extractor.sampleTime.coerceAtLeast(0)
            muxer.start()
            val buffer = ByteBuffer.allocateDirect(maxSize)
            val info = MediaCodec.BufferInfo()
            var wrote = false
            while (true) {
                val size = extractor.readSampleData(buffer, 0)
                if (size < 0) break
                val t = extractor.sampleTime
                if (t > endUs + 1_000_000) break
                val track = trackMap[extractor.sampleTrackIndex]
                if (track != null && t in base..endUs) {
                    val key = extractor.sampleFlags and MediaExtractor.SAMPLE_FLAG_SYNC != 0
                    info.set(0, size, t - base, if (key) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0)
                    muxer.writeSampleData(track, buffer, info)
                    wrote = true
                }
                extractor.advance()
            }
            muxer.stop()
            wrote && tmp.renameTo(dst)
        } catch (e: Exception) {
            false
        } finally {
            runCatching { muxer?.release() }
            extractor.release()
            tmp.delete()
        }
    }

    /** Puts a copy in the phone's Gallery, under Movies/Clutch. */
    fun saveToGallery(context: Context, file: File): Boolean = runCatching {
        val values = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, file.name)
            put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
            put(MediaStore.Video.Media.RELATIVE_PATH, Environment.DIRECTORY_MOVIES + "/Clutch")
            put(MediaStore.Video.Media.IS_PENDING, 1)
        }
        val resolver = context.contentResolver
        val uri = resolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)!!
        resolver.openOutputStream(uri)!!.use { out -> file.inputStream().use { it.copyTo(out) } }
        values.clear()
        values.put(MediaStore.Video.Media.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        true
    }.getOrDefault(false)

    fun shareIntent(context: Context, file: File): Intent {
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
        val send = Intent(Intent.ACTION_SEND)
            .setType("video/mp4")
            .putExtra(Intent.EXTRA_STREAM, uri)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        return Intent.createChooser(send, "Share clip")
    }

    fun formatDuration(ms: Long): String {
        val s = (ms + 500) / 1000
        return "%d:%02d".format(s / 60, s % 60)
    }
}
