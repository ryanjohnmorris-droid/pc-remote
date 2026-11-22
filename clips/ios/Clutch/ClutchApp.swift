import SwiftUI

@main
struct ClutchApp: App {
    @StateObject private var library = ClipLibrary()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(library)
                .preferredColorScheme(.dark)
                .tint(Theme.accent)
        }
        .onChange(of: scenePhase) { phase in
            if phase == .active { library.reload() }
        }
    }
}

enum Theme {
    static let accent = Color(red: 1, green: 0.24, blue: 0.34)
    static let card = Color(white: 0.11)
    static let green = Color(red: 0.17, green: 0.83, blue: 0.49)
}
