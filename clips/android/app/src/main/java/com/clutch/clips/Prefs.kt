package com.clutch.clips

import android.content.Context

/** The user's choices, kept on the phone. */
class Prefs(context: Context) {
    private val p = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    /** How many seconds a saved clip goes back. */
    var clipSeconds: Int
        get() = p.getInt("clipSeconds", 30)
        set(v) = p.edit().putInt("clipSeconds", v).apply()

    /** Shorter side of the video, in pixels (720 or 1080). */
    var quality: Int
        get() = p.getInt("quality", 720)
        set(v) = p.edit().putInt("quality", v).apply()

    var fps: Int
        get() = p.getInt("fps", 60)
        set(v) = p.edit().putInt("fps", v).apply()

    var gameAudio: Boolean
        get() = p.getBoolean("gameAudio", true)
        set(v) = p.edit().putBoolean("gameAudio", v).apply()

    var mic: Boolean
        get() = p.getBoolean("mic", false)
        set(v) = p.edit().putBoolean("mic", v).apply()

    var bubble: Boolean
        get() = p.getBoolean("bubble", true)
        set(v) = p.edit().putBoolean("bubble", v).apply()

    var bubbleX: Int
        get() = p.getInt("bubbleX", 0)
        set(v) = p.edit().putInt("bubbleX", v).apply()

    var bubbleY: Int
        get() = p.getInt("bubbleY", 300)
        set(v) = p.edit().putInt("bubbleY", v).apply()

    fun recorderConfig() = ScreenRecorder.Config(clipSeconds, quality, fps, gameAudio, mic)
}
