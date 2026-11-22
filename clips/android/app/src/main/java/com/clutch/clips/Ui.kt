package com.clutch.clips

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/** Small helpers for building the screens in code. */
object Ui {
    const val BG = 0xFF0E0F13.toInt()
    const val CARD = 0xFF1A1C23.toInt()
    const val CARD_2 = 0xFF252833.toInt()
    const val ACCENT = 0xFFFF3D57.toInt()
    const val GREEN = 0xFF2BD47D.toInt()
    const val TEXT = 0xFFF4F5F7.toInt()
    const val MUTED = 0xFF9AA0AE.toInt()

    fun Context.dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    fun rounded(context: Context, color: Int, radiusDp: Int = 16) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = context.dp(radiusDp).toFloat()
    }

    fun Context.text(s: String, size: Float = 15f, color: Int = TEXT, bold: Boolean = false) = TextView(this).apply {
        text = s
        textSize = size
        setTextColor(color)
        if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.BOLD)
    }

    fun Context.button(label: String, primary: Boolean = false, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        textSize = 15f
        setTextColor(if (primary) 0xFFFFFFFF.toInt() else TEXT)
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        stateListAnimator = null
        minHeight = dp(48)
        background = RippleDrawable(
            ColorStateList.valueOf(0x33FFFFFF),
            rounded(this@button, if (primary) ACCENT else CARD_2, 14), null,
        )
        setOnClickListener { onClick() }
    }

    fun Context.card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(this@card, CARD, 20)
        setPadding(dp(18), dp(16), dp(18), dp(18))
    }

    fun Context.row(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    /** A row of choices like 15s · 30s · 60s where one is picked. */
    fun Context.segmented(options: List<Pair<String, Int>>, selected: Int, onPick: (Int) -> Unit): LinearLayout {
        val group = row().apply { background = rounded(this@segmented, CARD_2, 12); setPadding(dp(3), dp(3), dp(3), dp(3)) }
        val views = mutableListOf<TextView>()
        fun paint(value: Int) = views.forEachIndexed { i, v ->
            val on = options[i].second == value
            v.background = if (on) rounded(this, ACCENT, 10) else null
            v.setTextColor(if (on) 0xFFFFFFFF.toInt() else MUTED)
        }
        options.forEach { (label, value) ->
            val v = text(label, 14f, MUTED, bold = true).apply {
                gravity = Gravity.CENTER
                setPadding(0, dp(8), 0, dp(8))
                setOnClickListener { paint(value); onPick(value) }
            }
            views += v
            group.addView(v, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        }
        paint(selected)
        return group
    }

    fun lp(w: Int = LinearLayout.LayoutParams.MATCH_PARENT, h: Int = LinearLayout.LayoutParams.WRAP_CONTENT, top: Int = 0) =
        LinearLayout.LayoutParams(w, h).apply { topMargin = top }

    /** Keeps content clear of the status bar and navigation bar. */
    fun padForSystemBars(view: View, extra: Int = 0) {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.R) return
        val l = view.paddingLeft; val t = view.paddingTop; val r = view.paddingRight; val b = view.paddingBottom
        view.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            v.setPadding(l + bars.left, t + bars.top + extra, r + bars.right, b + bars.bottom + extra)
            insets
        }
    }
}
