import Foundation

/// Things the app and the recording extension share through their App Group.
enum Shared {
    /// Set by APP_GROUP_ID in project.yml. Both targets must use the same group.
    static let appGroupID: String =
        (Bundle.main.object(forInfoDictionaryKey: "AppGroupID") as? String) ?? "group.com.example.clutch"

    static let saveClipNote = appGroupID + ".saveClip"
    static let clipQueuedNote = appGroupID + ".clipQueued"

    /// False when the App Group isn't set up in Xcode, so the app and the recorder can't see each other's files.
    static var isGroupConfigured: Bool {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroupID) != nil
    }

    static var container: URL {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroupID)
            ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    static var segmentsDir: URL { container.appendingPathComponent("Segments", isDirectory: true) }
    static var pendingDir: URL { container.appendingPathComponent("Pending", isDirectory: true) }
    static var clipsDir: URL { container.appendingPathComponent("Clips", isDirectory: true) }

    static func prepareFolders() {
        for dir in [segmentsDir, pendingDir, clipsDir] {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
    }

    static let defaults: UserDefaults = UserDefaults(suiteName: appGroupID) ?? .standard

    enum Key {
        static let clipSeconds = "clipSeconds"
        static let includeMic = "includeMic"
        static let quality = "quality"
        static let heartbeat = "heartbeat"
    }

    static var clipSeconds: Int {
        let v = defaults.integer(forKey: Key.clipSeconds)
        return v > 0 ? v : 30
    }

    static var includeMic: Bool {
        defaults.object(forKey: Key.includeMic) as? Bool ?? true
    }

    /// Shorter side of the video in pixels.
    static var quality: Int {
        let v = defaults.integer(forKey: Key.quality)
        return v > 0 ? v : 1080
    }

    /// The recorder writes a heartbeat every second while the replay buffer is on.
    static var isRecordingNow: Bool {
        let beat = defaults.double(forKey: Key.heartbeat)
        return beat > 0 && Date().timeIntervalSince1970 - beat < 4
    }

    static func beat(on: Bool) {
        defaults.set(on ? Date().timeIntervalSince1970 : 0, forKey: Key.heartbeat)
    }
}

/// A clip the recorder has cut out of the replay buffer, waiting for the app to finish it.
struct PendingClip: Codable {
    struct Segment: Codable {
        var file: String
        /// Host-clock times of the first and last video frame in this segment file.
        var start: Double
        var end: Double
    }

    var segments: [Segment]
    var windowStart: Double
    var windowEnd: Double
    var createdAt: Date

    static let fileName = "clip.json"
}

/// System-wide notifications between the app, the Save Clip shortcut and the recorder.
enum DarwinNotify {
    static func post(_ name: String) {
        CFNotificationCenterPostNotification(
            CFNotificationCenterGetDarwinNotifyCenter(), CFNotificationName(name as CFString), nil, nil, true
        )
    }

    /// Posts [send] (if given), then waits up to [timeout] seconds for [name]. Returns whether it arrived.
    static func wait(for name: String, timeout: Double, afterPosting send: String? = nil) async -> Bool {
        await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            let once = Once(cont)
            DispatchQueue.main.async {
                let observer = DarwinObserver(name) { once.resume(true) }
                if let send { post(send) }
                DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
                    once.resume(false)
                    withExtendedLifetime(observer) {}
                }
            }
        }
    }

    private final class Once: @unchecked Sendable {
        private var cont: CheckedContinuation<Bool, Never>?
        private let lock = NSLock()
        init(_ c: CheckedContinuation<Bool, Never>) { cont = c }
        func resume(_ v: Bool) {
            lock.lock(); let c = cont; cont = nil; lock.unlock()
            c?.resume(returning: v)
        }
    }
}

final class DarwinObserver {
    private let name: String
    private let handler: () -> Void

    init(_ name: String, handler: @escaping () -> Void) {
        self.name = name
        self.handler = handler
        CFNotificationCenterAddObserver(
            CFNotificationCenterGetDarwinNotifyCenter(),
            Unmanaged.passUnretained(self).toOpaque(),
            { _, observer, _, _, _ in
                guard let observer else { return }
                Unmanaged<DarwinObserver>.fromOpaque(observer).takeUnretainedValue().handler()
            },
            name as CFString, nil, .deliverImmediately
        )
    }

    deinit {
        CFNotificationCenterRemoveObserver(
            CFNotificationCenterGetDarwinNotifyCenter(),
            Unmanaged.passUnretained(self).toOpaque(),
            CFNotificationName(name as CFString), nil
        )
    }
}
