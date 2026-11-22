import AppIntents

/// "Save Clip" for Shortcuts, Siri, Back Tap and the Action Button, so you can save without leaving your game.
struct SaveClipIntent: AppIntent {
    static var title: LocalizedStringResource = "Save Clip"
    static var description = IntentDescription("Saves the last few seconds of your game to Clutch.")
    static var openAppWhenRun: Bool = false

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard Shared.isRecordingNow else {
            return .result(dialog: "Replay is off. Open Clutch and tap Start replay first.")
        }
        let saved = await DarwinNotify.wait(for: Shared.clipQueuedNote, timeout: 4, afterPosting: Shared.saveClipNote)
        if saved {
            return .result(dialog: "Clip saved")
        }
        return .result(dialog: "Couldn't save a clip. Is replay still on?")
    }
}

struct ClutchShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: SaveClipIntent(),
            phrases: ["Save a clip in \(.applicationName)", "Clip that with \(.applicationName)"],
            shortTitle: "Save Clip",
            systemImageName: "record.circle"
        )
    }
}
