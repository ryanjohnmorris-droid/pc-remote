package com.clutch.clips

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.view.WindowManager
import android.widget.TextView
import kotlin.math.abs

/** The floating CLIP button that stays on top of your game. Drag it anywhere; tap to save. */
class Bubble(private val context: Context, private val onTap: () -> Unit) {
    private val wm = context.getSystemService(WindowManager::class.java)
    private val prefs = Prefs(context)
    private var view: TextView? = null

    @SuppressLint("ClickableViewAccessibility")
    fun show() {
        if (view != null) return
        val density = context.resources.displayMetrics.density
        val size = (52 * density).toInt()
        val v = TextView(context).apply {
            text = "CLIP"
            setTextColor(0xFFFFFFFF.toInt())
            textSize = 11f
            typeface = Typeface.DEFAULT_BOLD
            letterSpacing = 0.08f
            gravity = Gravity.CENTER
            alpha = 0.8f
            contentDescription = "Save clip"
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(0xE6FF3D57.toInt())
                setStroke((2 * density).toInt(), 0x80FFFFFF.toInt())
            }
        }
        val lp = WindowManager.LayoutParams(
            size, size,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = prefs.bubbleX
            y = prefs.bubbleY
        }
        val slop = ViewConfiguration.get(context).scaledTouchSlop
        var downX = 0f
        var downY = 0f
        var startX = 0
        var startY = 0
        var moved = false
        v.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = e.rawX; downY = e.rawY; startX = lp.x; startY = lp.y; moved = false
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = e.rawX - downX
                    val dy = e.rawY - downY
                    if (moved || abs(dx) > slop || abs(dy) > slop) {
                        moved = true
                        lp.x = startX + dx.toInt()
                        lp.y = startY + dy.toInt()
                        runCatching { wm.updateViewLayout(v, lp) }
                    }
                }
                MotionEvent.ACTION_UP -> {
                    if (moved) {
                        prefs.bubbleX = lp.x
                        prefs.bubbleY = lp.y
                    } else {
                        onTap()
                    }
                }
            }
            true
        }
        runCatching { wm.addView(v, lp) }.onSuccess { view = v }
    }

    fun flash() {
        val v = view ?: return
        v.animate().scaleX(1.25f).scaleY(1.25f).alpha(1f).setDuration(120).withEndAction {
            v.animate().scaleX(1f).scaleY(1f).alpha(0.8f).setDuration(180).start()
        }.start()
    }

    fun hide() {
        view?.let { runCatching { wm.removeView(it) } }
        view = null
    }
}
