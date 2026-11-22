package com.clutch.clips

import android.app.Activity
import android.app.AlertDialog
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import android.widget.VideoView
import com.clutch.clips.Ui.button
import com.clutch.clips.Ui.dp
import com.clutch.clips.Ui.lp
import com.clutch.clips.Ui.row
import com.clutch.clips.Ui.text
import java.io.File
import kotlin.concurrent.thread

/** Watch one clip, trim it, share it, save it to the Gallery, rename or delete it. */
class PlayerActivity : Activity() {

    private lateinit var file: File
    private lateinit var video: VideoView
    private lateinit var title: TextView
    private lateinit var startBar: SeekBar
    private lateinit var endBar: SeekBar
    private lateinit var rangeLabel: TextView
    private lateinit var playButton: android.widget.Button
    private var durationMs = 0
    private val handler = Handler(Looper.getMainLooper())

    private val loopTicker = object : Runnable {
        override fun run() {
            if (video.isPlaying && durationMs > 0 && video.currentPosition >= endBar.progress) {
                video.seekTo(startBar.progress)
            }
            handler.postDelayed(this, 80)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        file = File(intent.getStringExtra("path") ?: run { finish(); return })
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) window.setDecorFitsSystemWindows(false)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
            setPadding(dp(16), dp(8), dp(16), dp(16))
        }
        Ui.padForSystemBars(root)
        setContentView(root)

        val top = row()
        top.addView(button("‹ Back") { finish() }, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, dp(44)))
        title = text(file.nameWithoutExtension, 16f, bold = true).apply {
            maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END; setPadding(dp(12), 0, 0, 0)
        }
        top.addView(title, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(top)

        val frame = FrameLayout(this).apply { setBackgroundColor(0xFF000000.toInt()) }
        video = VideoView(this)
        frame.addView(video, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER))
        frame.setOnClickListener { togglePlay() }
        root.addView(frame, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f).apply { topMargin = dp(10) })

        rangeLabel = text("", 14f, Ui.MUTED)
        root.addView(rangeLabel, lp(top = dp(12)))
        startBar = trimBar(root, "Start")
        endBar = trimBar(root, "End")

        val controls = row()
        playButton = button("Pause") { togglePlay() }
        controls.addView(playButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { rightMargin = dp(6) })
        controls.addView(button("Save trimmed", primary = true) { saveTrim() }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { leftMargin = dp(6) })
        root.addView(controls, lp(top = dp(10)))

        val actions = row()
        listOf(
            "Share" to { startActivity(ClipStore.shareIntent(this, file)) },
            "Gallery" to { saveToGallery() },
            "Rename" to { rename() },
            "Delete" to { delete() },
        ).forEachIndexed { i, (label, action) ->
            actions.addView(button(label) { action() }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                if (i > 0) leftMargin = dp(6)
            })
        }
        root.addView(actions, lp(top = dp(8)))

        video.setOnPreparedListener { mp ->
            mp.isLooping = true
            durationMs = mp.duration
            startBar.max = durationMs
            endBar.max = durationMs
            startBar.progress = 0
            endBar.progress = durationMs
            updateRange()
            video.start()
        }
        video.setVideoPath(file.path)
    }

    private fun trimBar(parent: LinearLayout, label: String): SeekBar {
        val r = row()
        r.addView(text(label, 14f), LinearLayout.LayoutParams(dp(48), LinearLayout.LayoutParams.WRAP_CONTENT))
        val bar = SeekBar(this)
        bar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar, progress: Int, fromUser: Boolean) {
                if (!fromUser) return
                val gap = 500
                if (bar === startBar && progress > endBar.progress - gap) bar.progress = maxOf(0, endBar.progress - gap)
                if (bar === endBar && progress < startBar.progress + gap) bar.progress = minOf(durationMs, startBar.progress + gap)
                video.seekTo(if (bar === startBar) bar.progress else maxOf(0, bar.progress - 1500))
                updateRange()
            }
            override fun onStartTrackingTouch(seekBar: SeekBar) {}
            override fun onStopTrackingTouch(seekBar: SeekBar) {}
        })
        r.addView(bar, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        parent.addView(r, lp(top = dp(4)))
        return bar
    }

    private fun updateRange() {
        val len = (endBar.progress - startBar.progress).toLong()
        rangeLabel.text = "${ClipStore.formatDuration(startBar.progress.toLong())} – " +
            "${ClipStore.formatDuration(endBar.progress.toLong())}  ·  ${ClipStore.formatDuration(len)} long"
    }

    private fun togglePlay() {
        if (video.isPlaying) video.pause() else video.start()
        playButton.text = if (video.isPlaying) "Pause" else "Play"
    }

    private fun saveTrim() {
        if (durationMs == 0) return
        val start = startBar.progress.toLong()
        val end = endBar.progress.toLong()
        if (start <= 200 && end >= durationMs - 200) {
            Toast.makeText(this, "Drag Start or End to choose the part to keep", Toast.LENGTH_SHORT).show()
            return
        }
        val dst = ClipStore.newClipFile(this, file.nameWithoutExtension + " (trimmed)")
        Toast.makeText(this, "Trimming…", Toast.LENGTH_SHORT).show()
        thread {
            val ok = ClipStore.trim(file, dst, start, end)
            runOnUiThread {
                Toast.makeText(this, if (ok) "Saved as \"${dst.nameWithoutExtension}\"" else "Couldn't trim this clip", Toast.LENGTH_LONG).show()
                if (ok) {
                    file = dst
                    title.text = dst.nameWithoutExtension
                    video.setVideoPath(dst.path)
                }
            }
        }
    }

    private fun saveToGallery() {
        thread {
            val ok = ClipStore.saveToGallery(this, file)
            runOnUiThread {
                Toast.makeText(this, if (ok) "Saved to Gallery › Movies › Clutch" else "Couldn't save to Gallery", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun rename() {
        val input = EditText(this).apply {
            setText(file.nameWithoutExtension)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            selectAll()
        }
        AlertDialog.Builder(this)
            .setTitle("Rename clip")
            .setView(FrameLayout(this).apply { setPadding(dp(20), dp(8), dp(20), 0); addView(input) })
            .setPositiveButton("Rename") { _, _ ->
                val name = input.text.toString().replace(Regex("[\\\\/:*?\"<>|]"), "").trim()
                if (name.isEmpty()) return@setPositiveButton
                val dst = File(file.parentFile, "$name.mp4")
                if (dst.exists()) {
                    Toast.makeText(this, "A clip with that name already exists", Toast.LENGTH_SHORT).show()
                } else if (file.renameTo(dst)) {
                    file = dst
                    title.text = name
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun delete() {
        AlertDialog.Builder(this)
            .setTitle("Delete this clip?")
            .setMessage("\"${file.nameWithoutExtension}\" will be removed from Clutch. Copies you saved to the Gallery stay.")
            .setPositiveButton("Delete") { _, _ ->
                video.stopPlayback()
                file.delete()
                finish()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    override fun onResume() {
        super.onResume()
        handler.post(loopTicker)
    }

    override fun onPause() {
        handler.removeCallbacks(loopTicker)
        video.pause()
        super.onPause()
    }
}
