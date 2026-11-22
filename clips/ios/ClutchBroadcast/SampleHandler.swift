import ReplayKit

/// Runs while the replay buffer is on (started from the Clutch app or Control Center).
class SampleHandler: RPBroadcastSampleHandler {
    private var recorder: SegmentRecorder?
    private var saveObserver: DarwinObserver?
    private var lastBeat = Date.distantPast

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        Shared.prepareFolders()
        let rec = SegmentRecorder()
        rec.clearOldSegments()
        recorder = rec
        saveObserver = DarwinObserver(Shared.saveClipNote) { [weak rec] in rec?.saveClip() }
        Shared.beat(on: true)
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        recorder?.append(sampleBuffer, type: sampleBufferType)
        let now = Date()
        if now.timeIntervalSince(lastBeat) > 1 {
            lastBeat = now
            Shared.beat(on: true)
        }
    }

    override func broadcastFinished() {
        saveObserver = nil
        recorder?.stop()
        recorder = nil
        Shared.beat(on: false)
    }
}
