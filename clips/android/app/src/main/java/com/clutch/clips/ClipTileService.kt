package com.clutch.clips

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** A "Save clip" button in the Quick Settings panel (swipe down from the top). */
class ClipTileService : TileService() {
    private val listener: () -> Unit = { refresh() }

    override fun onStartListening() {
        ReplayService.addListener(listener)
        refresh()
    }

    override fun onStopListening() {
        ReplayService.removeListener(listener)
    }

    private fun refresh() {
        val tile = qsTile ?: return
        tile.state = if (ReplayService.running) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.label = "Save clip"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            tile.subtitle = if (ReplayService.running) "Replay on" else "Replay off"
        }
        tile.updateTile()
    }

    @SuppressLint("StartActivityAndCollapseDeprecated")
    override fun onClick() {
        if (ReplayService.save()) return
        val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE))
        } else {
            @Suppress("DEPRECATION") startActivityAndCollapse(intent)
        }
    }
}
