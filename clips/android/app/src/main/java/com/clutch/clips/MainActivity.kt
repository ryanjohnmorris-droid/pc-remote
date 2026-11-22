package com.clutch.clips

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.StatusBarManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.drawable.Icon
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import com.clutch.clips.Ui.button
import com.clutch.clips.Ui.card
import com.clutch.clips.Ui.dp
import com.clutch.clips.Ui.lp
import com.clutch.clips.Ui.row
import com.clutch.clips.Ui.segmented
import com.clutch.clips.Ui.text
import java.io.File
import java.text.DateFormat
import java.util.Date
import java.util.concurrent.Executors

class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var statusDot: View
    private lateinit var statusTitle: TextView
    private lateinit var statusDetail: TextView
    private lateinit var startButton: android.widget.Button
    private lateinit var saveButton: android.widget.Button
    private lateinit var clipsHeader: TextView
    private lateinit var clipsGrid: LinearLayout
    private val loader = Executors.newFixedThreadPool(2)
    private val listener: () -> Unit = { runOnUiThread { refresh() } }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) window.setDecorFitsSystemWindows(false)

        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(8), dp(16), dp(24))
        }
        val scroll = ScrollView(this).apply { isFillViewport = true; addView(content) }
        Ui.padForSystemBars(scroll)
        setContentView(scroll)

        content.addView(text("Clutch", 30f, bold = true), lp(top = dp(12)))
        content.addView(text("Save your best mobile game moments.", 15f, Ui.MUTED), lp(top = dp(2)))

        // Replay status and controls
        val status = card()
        val head = row()
        statusDot = View(this).apply { background = Ui.rounded(this@MainActivity, Ui.MUTED, 6) }
        head.addView(statusDot, LinearLayout.LayoutParams(dp(12), dp(12)).apply { rightMargin = dp(10) })
        statusTitle = text("Replay is off", 19f, bold = true)
        head.addView(statusTitle)
        status.addView(head)
        statusDetail = text("", 14f, Ui.MUTED)
        status.addView(statusDetail, lp(top = dp(6)))
        startButton = button("Start replay", primary = true) { toggleReplay() }
        status.addView(startButton, lp(top = dp(14)))
        saveButton = button("Save clip now") { if (!ReplayService.save()) refresh() }
        status.addView(saveButton, lp(top = dp(8)))
        content.addView(status, lp(top = dp(20)))

        // Settings
        val settings = card()
        settings.addView(text("Clip length", 14f, Ui.MUTED))
        settings.addView(
            segmented(listOf("15s" to 15, "30s" to 30, "60s" to 60, "90s" to 90, "2 min" to 120), prefs.clipSeconds) {
                prefs.clipSeconds = it; settingsChanged()
            }, lp(top = dp(6)),
        )
        settings.addView(text("Quality", 14f, Ui.MUTED), lp(top = dp(14)))
        settings.addView(segmented(listOf("720p" to 720, "1080p" to 1080), prefs.quality) { prefs.quality = it; settingsChanged() }, lp(top = dp(6)))
        settings.addView(text("Frame rate", 14f, Ui.MUTED), lp(top = dp(14)))
        settings.addView(segmented(listOf("30 fps" to 30, "60 fps" to 60), prefs.fps) { prefs.fps = it; settingsChanged() }, lp(top = dp(6)))
        settings.addView(toggle("Game sound", "What the phone plays. Some games block this.", prefs.gameAudio) { prefs.gameAudio = it; settingsChanged() }, lp(top = dp(10)))
        settings.addView(toggle("Microphone", "Your voice, mixed with the game.", prefs.mic) { prefs.mic = it; settingsChanged() })
        settings.addView(toggle("Floating CLIP button", "Shows over your game. It appears in clips too.", prefs.bubble) { prefs.bubble = it; settingsChanged() })
        content.addView(settings, lp(top = dp(14)))

        // Ways to save
        val ways = card()
        ways.addView(text("Ways to save a clip", 16f, bold = true))
        ways.addView(
            text(
                "• Tap the floating CLIP button\n• Swipe down and tap the Save clip tile\n• Tap Save clip in the Clutch notification",
                14f, Ui.MUTED,
            ).apply { setLineSpacing(0f, 1.3f) }, lp(top = dp(6)),
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ways.addView(button("Add the Save clip tile") { addTile() }, lp(top = dp(12)))
        }
        content.addView(ways, lp(top = dp(14)))

        clipsHeader = text("Your clips", 20f, bold = true)
        content.addView(clipsHeader, lp(top = dp(24)))
        clipsGrid = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        content.addView(clipsGrid, lp(top = dp(10)))
    }

    private fun toggle(title: String, detail: String, on: Boolean, onChange: (Boolean) -> Unit): View {
        val r = row().apply { setPadding(0, dp(10), 0, dp(4)) }
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        texts.addView(text(title, 16f))
        texts.addView(text(detail, 13f, Ui.MUTED))
        r.addView(texts, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        r.addView(Switch(this).apply { isChecked = on; setOnCheckedChangeListener { _, v -> onChange(v) } })
        return r
    }

    private fun settingsChanged() {
        if (ReplayService.running) Toast.makeText(this, "Changes apply next time you start replay", Toast.LENGTH_SHORT).show()
    }

    override fun onResume() {
        super.onResume()
        ReplayService.addListener(listener)
        refresh()
    }

    override fun onPause() {
        ReplayService.removeListener(listener)
        super.onPause()
    }

    override fun onDestroy() {
        loader.shutdownNow()
        super.onDestroy()
    }

    private fun refresh() {
        val on = ReplayService.running
        statusDot.background = Ui.rounded(this, if (on) Ui.GREEN else Ui.MUTED, 6)
        statusTitle.text = if (on) "Replay is on" else "Replay is off"
        statusDetail.text = if (on) {
            "Clutch is keeping the last ${prefs.clipSeconds}s. Go play, then save when something great happens."
        } else {
            "Start replay, then open your game. Clutch keeps the last ${prefs.clipSeconds}s in memory, so you only save the good bits."
        }
        startButton.text = if (on) "Stop replay" else "Start replay"
        saveButton.visibility = if (on) View.VISIBLE else View.GONE
        showClips()
    }

    // ---- Starting the replay buffer ----

    private fun toggleReplay() {
        if (ReplayService.running) {
            ReplayService.stop(this)
            return
        }
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) needed += Manifest.permission.POST_NOTIFICATIONS
        if ((prefs.mic || prefs.gameAudio) &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED
        ) needed += Manifest.permission.RECORD_AUDIO
        if (needed.isNotEmpty()) {
            requestPermissions(needed.toTypedArray(), REQ_PERMS)
            return
        }
        checkOverlayThenCapture()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_PERMS) checkOverlayThenCapture()
    }

    private fun checkOverlayThenCapture() {
        if (prefs.bubble && !Settings.canDrawOverlays(this)) {
            AlertDialog.Builder(this)
                .setTitle("Show the CLIP button over games?")
                .setMessage("Android asks you to allow \"Display over other apps\" for Clutch. Turn it on, come back, and tap Start replay again.")
                .setPositiveButton("Open settings") { _, _ ->
                    startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
                }
                .setNegativeButton("Start without it") { _, _ -> askToCapture() }
                .show()
            return
        }
        askToCapture()
    }

    private fun askToCapture() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ_CAPTURE)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION") super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_CAPTURE) return
        if (resultCode != RESULT_OK || data == null) {
            Toast.makeText(this, "Clutch needs screen recording to keep a replay", Toast.LENGTH_LONG).show()
            return
        }
        startForegroundService(
            Intent(this, ReplayService::class.java)
                .setAction(ReplayService.ACTION_START)
                .putExtra(ReplayService.EXTRA_CODE, resultCode)
                .putExtra(ReplayService.EXTRA_DATA, data),
        )
    }

    private fun addTile() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        getSystemService(StatusBarManager::class.java).requestAddTileService(
            ComponentName(this, ClipTileService::class.java), "Save clip",
            Icon.createWithResource(this, R.drawable.ic_notif), mainExecutor,
        ) { result ->
            val msg = when (result) {
                StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ADDED -> "Tile added. Swipe down to find it."
                StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ALREADY_ADDED -> "The tile is already there."
                else -> "Tile not added. You can add it from Quick Settings ✎ edit."
            }
            Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
        }
    }

    // ---- Clip library ----

    private fun showClips() {
        val clips = ClipStore.list(this)
        clipsHeader.text = if (clips.isEmpty()) "Your clips" else "Your clips (${clips.size})"
        clipsGrid.removeAllViews()
        if (clips.isEmpty()) {
            clipsGrid.addView(
                text("No clips yet. Start replay, play a game, and save a clip when something great happens.", 14f, Ui.MUTED),
            )
            return
        }
        clips.chunked(2).forEach { pair ->
            val r = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            pair.forEachIndexed { i, f ->
                r.addView(clipTile(f), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                    if (i == 0) rightMargin = dp(6) else leftMargin = dp(6)
                })
            }
            if (pair.size == 1) r.addView(View(this), LinearLayout.LayoutParams(0, 1, 1f).apply { leftMargin = dp(6) })
            clipsGrid.addView(r, lp(top = dp(12)))
        }
    }

    private fun clipTile(file: File): View {
        val tile = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setOnClickListener { startActivity(Intent(this@MainActivity, PlayerActivity::class.java).putExtra("path", file.path)) }
        }
        val frame = FrameLayout(this).apply { background = Ui.rounded(this@MainActivity, Ui.CARD, 14); clipToOutline = true }
        val image = ImageView(this).apply { scaleType = ImageView.ScaleType.CENTER_CROP }
        frame.addView(image, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, dp(110)))
        val badge = text("", 12f, bold = true).apply {
            background = Ui.rounded(this@MainActivity, 0xB3000000.toInt(), 8)
            setPadding(dp(7), dp(2), dp(7), dp(2))
        }
        frame.addView(badge, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM or Gravity.END).apply {
            setMargins(dp(6), dp(6), dp(6), dp(6))
        })
        tile.addView(frame)
        tile.addView(text(file.nameWithoutExtension, 14f).apply { maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END }, lp(top = dp(6)))
        tile.addView(text(DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(file.lastModified())), 12f, Ui.MUTED))
        loader.execute {
            val bmp: Bitmap? = ClipStore.thumbnail(this, file)
            val dur = ClipStore.formatDuration(ClipStore.durationMs(file))
            runOnUiThread {
                if (bmp != null) image.setImageBitmap(bmp)
                badge.text = dur
            }
        }
        return tile
    }

    companion object {
        private const val REQ_PERMS = 1
        private const val REQ_CAPTURE = 2
    }
}
