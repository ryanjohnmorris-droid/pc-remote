package com.clutch.clips

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.provider.Settings
import android.widget.Toast
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.Executors

/** Runs while the replay buffer is on, so recording keeps going while you play. */
class ReplayService : Service() {

    private val main = Handler(Looper.getMainLooper())
    private val saver = Executors.newSingleThreadExecutor()
    private var projection: MediaProjection? = null
    private var recorder: ScreenRecorder? = null
    private var bubble: Bubble? = null
    private var stopping = false
    private var clipSeconds = 30

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> start(intent)
            ACTION_SAVE -> saveClip()
            ACTION_STOP -> stopAll()
        }
        return START_NOT_STICKY
    }

    private fun start(intent: Intent) {
        if (recorder != null) return
        val prefs = Prefs(this)
        val config = prefs.recorderConfig()
        clipSeconds = config.clipSeconds
        createChannel()

        var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
        val micAllowed = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        if (config.mic && micAllowed && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        }
        startForeground(NOTIFICATION_ID, buildNotification(), type)

        val code = intent.getIntExtra(EXTRA_CODE, 0)
        val data: Intent? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(EXTRA_DATA, Intent::class.java)
        } else {
            @Suppress("DEPRECATION") intent.getParcelableExtra(EXTRA_DATA)
        }
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val proj = data?.let { runCatching { mpm.getMediaProjection(code, it) }.getOrNull() }
        if (proj == null) {
            stopAll()
            return
        }
        proj.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                main.post { stopAll() }
            }
        }, main)
        projection = proj

        try {
            recorder = ScreenRecorder(this, proj, config).also { it.start() }
        } catch (e: Exception) {
            Toast.makeText(this, "Couldn't start recording: ${e.message}", Toast.LENGTH_LONG).show()
            stopAll()
            return
        }
        instance = this
        running = true
        if (prefs.bubble && Settings.canDrawOverlays(this)) {
            bubble = Bubble(this) { saveClip() }.also { it.show() }
        }
        changed()
    }

    fun saveClip() {
        val rec = recorder ?: return
        bubble?.flash()
        saver.execute {
            val file = ClipStore.newClipFile(this)
            val seconds = runCatching { rec.saveClip(file) }.getOrNull()
            main.post {
                if (seconds != null) {
                    vibrate()
                    Toast.makeText(this, "Clip saved · ${seconds}s", Toast.LENGTH_SHORT).show()
                } else {
                    Toast.makeText(this, "Nothing to save yet, keep playing", Toast.LENGTH_SHORT).show()
                }
                changed()
            }
        }
    }

    private fun stopAll() {
        if (stopping) return
        stopping = true
        bubble?.hide()
        bubble = null
        recorder?.stop()
        recorder = null
        runCatching { projection?.stop() }
        projection = null
        running = false
        if (instance === this) instance = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        changed()
    }

    override fun onDestroy() {
        stopAll()
        saver.shutdown()
        super.onDestroy()
    }

    private fun vibrate() {
        val v = getSystemService(Vibrator::class.java) ?: return
        runCatching { v.vibrate(VibrationEffect.createOneShot(40, VibrationEffect.DEFAULT_AMPLITUDE)) }
    }

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(CHANNEL, "Replay buffer", NotificationManager.IMPORTANCE_LOW)
        channel.setShowBadge(false)
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), flags)
        val save = PendingIntent.getService(this, 1, Intent(this, ReplayService::class.java).setAction(ACTION_SAVE), flags)
        val stop = PendingIntent.getService(this, 2, Intent(this, ReplayService::class.java).setAction(ACTION_STOP), flags)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notif)
            .setContentTitle("Replay is on")
            .setContentText("Keeping the last ${clipSeconds}s. Tap Save clip after a great play.")
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Save clip", save).build())
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    companion object {
        const val ACTION_START = "com.clutch.clips.START"
        const val ACTION_SAVE = "com.clutch.clips.SAVE"
        const val ACTION_STOP = "com.clutch.clips.STOP"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        private const val CHANNEL = "replay"
        private const val NOTIFICATION_ID = 7

        @Volatile var running = false
            private set
        @Volatile private var instance: ReplayService? = null

        private val listeners = CopyOnWriteArraySet<() -> Unit>()
        fun addListener(l: () -> Unit) = listeners.add(l)
        fun removeListener(l: () -> Unit) = listeners.remove(l)
        private fun changed() = listeners.forEach { it() }

        /** Saves a clip right now, if the replay buffer is on. */
        fun save(): Boolean {
            val s = instance ?: return false
            s.main.post { s.saveClip() }
            return true
        }

        fun stop(context: Context) {
            if (running) context.startService(Intent(context, ReplayService::class.java).setAction(ACTION_STOP))
        }
    }
}
