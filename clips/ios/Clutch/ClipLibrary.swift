import AVFoundation
import Photos
import SwiftUI
import UIKit

struct Clip: Identifiable, Hashable {
    let url: URL
    let date: Date
    var id: URL { url }
    var name: String { url.deletingPathExtension().lastPathComponent }
}

/// The saved clips, plus finishing clips the recorder cut while you were playing.
@MainActor
final class ClipLibrary: ObservableObject {
    @Published private(set) var clips: [Clip] = []
    @Published private(set) var pendingCount = 0
    @Published private(set) var isRecording = false
    @Published var lastError: String?

    private var queuedObserver: DarwinObserver?
    private var timer: Timer?
    private var processing = false
    private let thumbCache = NSCache<NSURL, UIImage>()

    init() {
        Shared.prepareFolders()
        queuedObserver = DarwinObserver(Shared.clipQueuedNote) { [weak self] in
            Task { @MainActor in self?.reload() }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                let on = Shared.isRecordingNow
                if on != self.isRecording { self.isRecording = on }
            }
        }
        reload()
    }

    func reload() {
        isRecording = Shared.isRecordingNow
        let fm = FileManager.default
        let urls = (try? fm.contentsOfDirectory(at: Shared.clipsDir, includingPropertiesForKeys: [.creationDateKey])) ?? []
        clips = urls.filter { $0.pathExtension == "mp4" && !$0.lastPathComponent.hasPrefix(".") }
            .map { url in
                let date = (try? url.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast
                return Clip(url: url, date: date)
            }
            .sorted { $0.date > $1.date }
        let pending = (try? fm.contentsOfDirectory(at: Shared.pendingDir, includingPropertiesForKeys: nil)) ?? []
        pendingCount = pending.count
        if pendingCount > 0 { processPending() }
    }

    /// Asks the recorder to save the last N seconds right now.
    func saveNow() async -> Bool {
        await DarwinNotify.wait(for: Shared.clipQueuedNote, timeout: 4, afterPosting: Shared.saveClipNote)
    }

    private func processPending() {
        guard !processing else { return }
        processing = true
        Task {
            let fm = FileManager.default
            while let dir = ((try? fm.contentsOfDirectory(at: Shared.pendingDir, includingPropertiesForKeys: nil)) ?? []).first {
                do {
                    try await ClipProcessor.finish(pending: dir, into: Shared.clipsDir)
                } catch {
                    lastError = "A clip couldn't be finished: \(error.localizedDescription)"
                }
                try? fm.removeItem(at: dir)
                pendingCount = max(0, pendingCount - 1)
                reloadClipsOnly()
            }
            processing = false
            reloadClipsOnly()
        }
    }

    private func reloadClipsOnly() {
        let pending = (try? FileManager.default.contentsOfDirectory(at: Shared.pendingDir, includingPropertiesForKeys: nil)) ?? []
        pendingCount = pending.count
        let fm = FileManager.default
        let urls = (try? fm.contentsOfDirectory(at: Shared.clipsDir, includingPropertiesForKeys: [.creationDateKey])) ?? []
        clips = urls.filter { $0.pathExtension == "mp4" && !$0.lastPathComponent.hasPrefix(".") }
            .map { Clip(url: $0, date: (try? $0.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast) }
            .sorted { $0.date > $1.date }
    }

    // MARK: - Clip actions

    func thumbnail(for clip: Clip) async -> UIImage? {
        if let img = thumbCache.object(forKey: clip.url as NSURL) { return img }
        let gen = AVAssetImageGenerator(asset: AVURLAsset(url: clip.url))
        gen.appliesPreferredTrackTransform = true
        gen.maximumSize = CGSize(width: 480, height: 480)
        guard let result = try? await gen.image(at: CMTime(seconds: 1, preferredTimescale: 600)) else { return nil }
        let img = UIImage(cgImage: result.image)
        thumbCache.setObject(img, forKey: clip.url as NSURL)
        return img
    }

    func duration(of clip: Clip) async -> Double {
        (try? await AVURLAsset(url: clip.url).load(.duration).seconds) ?? 0
    }

    func rename(_ clip: Clip, to newName: String) -> Clip? {
        let clean = newName.components(separatedBy: CharacterSet(charactersIn: "/\\:?%*|\"<>")).joined()
            .trimmingCharacters(in: .whitespaces)
        guard !clean.isEmpty else { return nil }
        let dst = Shared.clipsDir.appendingPathComponent(clean).appendingPathExtension("mp4")
        guard !FileManager.default.fileExists(atPath: dst.path) else {
            lastError = "A clip called \"\(clean)\" already exists."
            return nil
        }
        do {
            try FileManager.default.moveItem(at: clip.url, to: dst)
        } catch {
            lastError = error.localizedDescription
            return nil
        }
        reloadClipsOnly()
        return clips.first { $0.url == dst }
    }

    func delete(_ clip: Clip) {
        try? FileManager.default.removeItem(at: clip.url)
        reloadClipsOnly()
    }

    /// Saves the part between [start] and [end] seconds as a new clip.
    func trim(_ clip: Clip, start: Double, end: Double) async throws -> Clip {
        let dst = ClipProcessor.newClipURL(in: Shared.clipsDir, name: clip.name + " (trimmed)")
        let asset = AVURLAsset(url: clip.url)
        guard let export = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetHighestQuality) else {
            throw ClipError.exportFailed
        }
        export.outputURL = dst
        export.outputFileType = .mp4
        export.timeRange = CMTimeRange(
            start: CMTime(seconds: start, preferredTimescale: 600),
            end: CMTime(seconds: end, preferredTimescale: 600)
        )
        try await ClipProcessor.run(export)
        reloadClipsOnly()
        return clips.first { $0.url == dst } ?? Clip(url: dst, date: Date())
    }

    func saveToPhotos(_ clip: Clip) async -> Bool {
        let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard status == .authorized || status == .limited else { return false }
        do {
            try await PHPhotoLibrary.shared().performChanges {
                PHAssetCreationRequest.creationRequestForAssetFromVideo(atFileURL: clip.url)
            }
            return true
        } catch {
            return false
        }
    }
}

enum ClipError: LocalizedError {
    case nothingToSave, exportFailed

    var errorDescription: String? {
        switch self {
        case .nothingToSave: return "There was no video in the replay buffer."
        case .exportFailed: return "The video couldn't be written."
        }
    }
}

/// Joins the recorder's segment files into one finished clip, mixing game sound and mic.
enum ClipProcessor {
    static func newClipURL(in dir: URL, name: String? = nil) -> URL {
        let base: String
        if let name {
            base = name
        } else {
            let f = DateFormatter()
            f.dateFormat = "yyyy-MM-dd HH.mm.ss"
            base = "Clip " + f.string(from: Date())
        }
        var url = dir.appendingPathComponent(base).appendingPathExtension("mp4")
        var i = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = dir.appendingPathComponent("\(base) (\(i))").appendingPathExtension("mp4")
            i += 1
        }
        return url
    }

    static func finish(pending dir: URL, into clipsDir: URL) async throws {
        let data = try Data(contentsOf: dir.appendingPathComponent(PendingClip.fileName))
        let pending = try JSONDecoder().decode(PendingClip.self, from: data)

        let comp = AVMutableComposition()
        guard let videoTrack = comp.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid),
              let gameTrack = comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid),
              let micTrack = comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
        else { throw ClipError.exportFailed }

        var cursor = CMTime.zero
        var pieces: [(at: CMTime, transform: CGAffineTransform, size: CGSize)] = []

        for seg in pending.segments.sorted(by: { $0.start < $1.start }) {
            let asset = AVURLAsset(url: dir.appendingPathComponent(seg.file))
            guard let video = try await asset.loadTracks(withMediaType: .video).first else { continue }
            let duration = try await asset.load(.duration).seconds
            let from = max(0, pending.windowStart - seg.start)
            let to = min(duration, pending.windowEnd - seg.start + 0.05)
            guard to - from > 0.05 else { continue }
            let range = CMTimeRange(
                start: CMTime(seconds: from, preferredTimescale: 600),
                end: CMTime(seconds: to, preferredTimescale: 600)
            )
            let videoRange = try await video.load(.timeRange)
            let vr = range.intersection(videoRange)
            guard vr.duration.seconds > 0.05 else { continue }
            try videoTrack.insertTimeRange(vr, of: video, at: cursor)

            let audios = try await asset.loadTracks(withMediaType: .audio)
            for (i, audio) in audios.prefix(2).enumerated() {
                let audioRange = try await audio.load(.timeRange)
                let ar = vr.intersection(audioRange)
                guard ar.duration.seconds > 0 else { continue }
                let target = i == 0 ? gameTrack : micTrack
                try? target.insertTimeRange(ar, of: audio, at: cursor + (ar.start - vr.start))
            }

            let size = try await video.load(.naturalSize)
            let transform = try await video.load(.preferredTransform)
            pieces.append((cursor, transform, size))
            cursor = cursor + vr.duration
        }
        guard let last = pieces.last, cursor.seconds > 0.1 else { throw ClipError.nothingToSave }

        for track in [gameTrack, micTrack] where track.segments.isEmpty {
            comp.removeTrack(track)
        }

        // Render at the shape of the newest part; earlier parts in another orientation are fitted inside.
        let lastRect = CGRect(origin: .zero, size: last.size).applying(last.transform)
        let render = CGSize(width: even(abs(lastRect.width)), height: even(abs(lastRect.height)))
        let layer = AVMutableVideoCompositionLayerInstruction(assetTrack: videoTrack)
        for p in pieces {
            layer.setTransform(fit(p.transform, natural: p.size, into: render), at: p.at)
        }
        let instruction = AVMutableVideoCompositionInstruction()
        instruction.timeRange = CMTimeRange(start: .zero, duration: cursor)
        instruction.layerInstructions = [layer]
        let videoComp = AVMutableVideoComposition()
        videoComp.renderSize = render
        videoComp.frameDuration = CMTime(value: 1, timescale: 60)
        videoComp.instructions = [instruction]

        let mix = AVMutableAudioMix()
        mix.inputParameters = comp.tracks(withMediaType: .audio).map { track in
            let p = AVMutableAudioMixInputParameters(track: track)
            p.setVolume(1, at: .zero)
            return p
        }

        guard let export = AVAssetExportSession(asset: comp, presetName: AVAssetExportPresetHighestQuality) else {
            throw ClipError.exportFailed
        }
        let out = newClipURL(in: clipsDir)
        let tmp = clipsDir.appendingPathComponent(".\(UUID().uuidString).mp4")
        export.outputURL = tmp
        export.outputFileType = .mp4
        export.videoComposition = videoComp
        export.audioMix = mix
        do {
            try await run(export)
            try FileManager.default.moveItem(at: tmp, to: out)
        } catch {
            try? FileManager.default.removeItem(at: tmp)
            throw error
        }
    }

    static func run(_ export: AVAssetExportSession) async throws {
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            export.exportAsynchronously { cont.resume() }
        }
        if export.status != .completed { throw export.error ?? ClipError.exportFailed }
    }

    private static func even(_ v: CGFloat) -> CGFloat { CGFloat(Int(v) / 2 * 2) }

    /// Rotates a part upright and scales it to fit the output, centred.
    private static func fit(_ t: CGAffineTransform, natural: CGSize, into render: CGSize) -> CGAffineTransform {
        let rect = CGRect(origin: .zero, size: natural).applying(t)
        var out = t.concatenating(CGAffineTransform(translationX: -rect.minX, y: -rect.minY))
        let w = abs(rect.width), h = abs(rect.height)
        let scale = min(render.width / w, render.height / h)
        out = out.concatenating(CGAffineTransform(scaleX: scale, y: scale))
        out = out.concatenating(CGAffineTransform(translationX: (render.width - w * scale) / 2, y: (render.height - h * scale) / 2))
        return out
    }
}
