import ReplayKit
import SwiftUI

/// Apple only lets apps start screen recording through its own picker button.
/// We keep that button hidden and press it when you tap our Start/Stop button.
final class BroadcastPickerHolder: ObservableObject {
    let picker: RPSystemBroadcastPickerView = {
        let p = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 44, height: 44))
        if let id = Bundle.main.bundleIdentifier { p.preferredExtension = id + ".broadcast" }
        p.showsMicrophoneButton = true
        return p
    }()

    func show() {
        for case let button as UIButton in picker.subviews {
            button.sendActions(for: .touchUpInside)
        }
    }
}

struct BroadcastPickerHost: UIViewRepresentable {
    let holder: BroadcastPickerHolder
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView { holder.picker }
    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {}
}
