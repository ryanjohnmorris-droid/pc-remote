import AVFoundation
import ImageIO
import ReplayKit

/// Writes the screen to short video files one after another and keeps only the
/// newest ones, so there's always a rolling replay on disk. Saving a clip picks
/// the files that cover the last N seconds and hands them to the app.
final class SegmentRecorder {
    private struct Segment {
        let url: URL
        let start: Double
        var end: Double
    }

    private let queue = DispatchQueue(label: "clutch.recorder")
    private let segmentLength = 6.0

    private var writer: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var appAudioInput: AVAssetWriterInput?
    private var micInput: AVAssetWriterInput?
    private var current: Segment?
    private var currentOrientation: CGImagePropertyOrientation = .up
    private var finished: [Segment] = []
    private var lastVideoTime = 0.0
    private var includeMic = Shared.includeMic
    private var writesInFlight = 0
    private var whenIdle: [() -> Void] = []

    func clearOldSegments() {
        let fm = FileManager.default
        for url in (try? fm.contentsOfDirectory(at: Shared.segmentsDir, includingPropertiesForKeys: nil)) ?? [] {
            try? fm.removeItem(at: url)
        }
    }

    func append(_ sb: CMSampleBuffer, type: RPSampleBufferType) {
        queue.sync {
            guard CMSampleBufferDataIsReady(sb) else { return }
            let time = CMSampleBufferGetPresentationTimeStamp(sb).seconds
            switch type {
            case .video:
                let orientation = Self.orientation(of: sb)
                if let seg = current {
                    if time - seg.start >= segmentLength || orientation != currentOrientation {
                        finishCurrent()
                    }
                }
                if writer == nil { startWriter(with: sb, orientation: orientation) }
                guard let input = videoInput, input.isReadyForMoreMediaData else { return }
                if input.append(sb) {
                    current?.end = time
                    lastVideoTime = time
                }
            case .audioApp:
                guard let input = appAudioInput, input.isReadyForMoreMediaData, current != nil else { return }
                input.append(sb)
            case .audioMic:
                guard let input = micInput, input.isReadyForMoreMediaData, current != nil else { return }
                input.append(sb)
            @unknown default:
                break
            }
        }
    }

    func stop() {
        queue.sync { finishCurrent() }
    }

    /// Cuts the last N seconds out of the replay buffer for the app to finish.
    func saveClip() {
        queue.async {
            let end = self.lastVideoTime
            guard end > 0 else { return }
            let seconds = Double(Shared.clipSeconds)
            self.finishCurrent()
            self.afterWritesFinish {
                let start = end - seconds
                let parts = self.finished.filter { $0.end > start && $0.start < end }.sorted { $0.start < $1.start }
                guard !parts.isEmpty else { return }
                let fm = FileManager.default
                let dir = Shared.pendingDir.appendingPathComponent(UUID().uuidString, isDirectory: true)
                do {
                    try fm.createDirectory(at: dir, withIntermediateDirectories: true)
                    var entries: [PendingClip.Segment] = []
                    for seg in parts {
                        let dst = dir.appendingPathComponent(seg.url.lastPathComponent)
                        // A hard link keeps the file even after the buffer deletes its copy.
                        if (try? fm.linkItem(at: seg.url, to: dst)) == nil {
                            try fm.copyItem(at: seg.url, to: dst)
                        }
                        entries.append(.init(file: seg.url.lastPathComponent, start: seg.start, end: seg.end))
                    }
                    let clip = PendingClip(segments: entries, windowStart: start, windowEnd: end, createdAt: Date())
                    try JSONEncoder().encode(clip).write(to: dir.appendingPathComponent(PendingClip.fileName))
                    DarwinNotify.post(Shared.clipQueuedNote)
                } catch {
                    try? fm.removeItem(at: dir)
                }
            }
        }
    }

    // MARK: - Writing segments (always on `queue`)

    private func startWriter(with sb: CMSampleBuffer, orientation: CGImagePropertyOrientation) {
        guard let image = CMSampleBufferGetImageBuffer(sb) else { return }
        let srcW = Double(CVPixelBufferGetWidth(image))
        let srcH = Double(CVPixelBufferGetHeight(image))
        let quality = Double(Shared.quality)
        let scale = min(1, quality / min(srcW, srcH))
        let w = Int(srcW * scale) / 2 * 2
        let h = Int(srcH * scale) / 2 * 2
        let bitrate = quality >= 1080 ? 8_000_000 : 5_000_000
        includeMic = Shared.includeMic

        let url = Shared.segmentsDir.appendingPathComponent("seg-\(UUID().uuidString).mp4")
        guard let w0 = try? AVAssetWriter(outputURL: url, fileType: .mp4) else { return }

        let video = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: w,
            AVVideoHeightKey: h,
            AVVideoScalingModeKey: AVVideoScalingModeResizeAspectFill,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: bitrate,
                AVVideoExpectedSourceFrameRateKey: 60,
                AVVideoMaxKeyFrameIntervalDurationKey: 1,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            ],
        ])
        video.expectsMediaDataInRealTime = true
        video.transform = Self.transform(for: orientation)
        guard w0.canAdd(video) else { return }
        w0.add(video)

        let app = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 44_100,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 128_000,
        ])
        app.expectsMediaDataInRealTime = true
        if w0.canAdd(app) { w0.add(app); appAudioInput = app } else { appAudioInput = nil }

        if includeMic {
            let mic = AVAssetWriterInput(mediaType: .audio, outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 44_100,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 64_000,
            ])
            mic.expectsMediaDataInRealTime = true
            if w0.canAdd(mic) { w0.add(mic); micInput = mic } else { micInput = nil }
        } else {
            micInput = nil
        }

        guard w0.startWriting() else { return }
        let start = CMSampleBufferGetPresentationTimeStamp(sb)
        w0.startSession(atSourceTime: start)
        writer = w0
        videoInput = video
        currentOrientation = orientation
        current = Segment(url: url, start: start.seconds, end: start.seconds)
    }

    /// Runs [block] on `queue` once every segment being closed has been written.
    private func afterWritesFinish(_ block: @escaping () -> Void) {
        if writesInFlight == 0 { block() } else { whenIdle.append(block) }
    }

    private func finishCurrent() {
        guard let w = writer, let seg = current else { return }
        writesInFlight += 1
        writer = nil
        current = nil
        videoInput?.markAsFinished()
        appAudioInput?.markAsFinished()
        micInput?.markAsFinished()
        videoInput = nil
        appAudioInput = nil
        micInput = nil
        w.finishWriting { [weak self] in
            guard let self else { return }
            self.queue.async {
                if w.status == .completed, seg.end > seg.start {
                    self.finished.append(seg)
                } else {
                    try? FileManager.default.removeItem(at: seg.url)
                }
                self.prune()
                self.writesInFlight -= 1
                if self.writesInFlight == 0 {
                    let blocks = self.whenIdle
                    self.whenIdle = []
                    blocks.forEach { $0() }
                }
            }
        }
    }

    /// Deletes segments too old to be part of any clip.
    private func prune() {
        let keepFrom = lastVideoTime - Double(Shared.clipSeconds) - segmentLength * 2
        let (old, keep) = (finished.filter { $0.end < keepFrom }, finished.filter { $0.end >= keepFrom })
        for seg in old { try? FileManager.default.removeItem(at: seg.url) }
        finished = keep
    }

    // MARK: - Orientation

    private static func orientation(of sb: CMSampleBuffer) -> CGImagePropertyOrientation {
        guard let n = CMGetAttachment(sb, key: RPVideoSampleOrientationKey as CFString, attachmentModeOut: nil) as? NSNumber,
              let o = CGImagePropertyOrientation(rawValue: n.uint32Value) else { return .up }
        return o
    }

    private static func transform(for o: CGImagePropertyOrientation) -> CGAffineTransform {
        switch o {
        case .left, .leftMirrored: return CGAffineTransform(rotationAngle: .pi / 2)
        case .right, .rightMirrored: return CGAffineTransform(rotationAngle: -.pi / 2)
        case .down, .downMirrored: return CGAffineTransform(rotationAngle: .pi)
        default: return .identity
        }
    }
}
