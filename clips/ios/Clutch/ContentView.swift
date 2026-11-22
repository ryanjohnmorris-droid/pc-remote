import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var library: ClipLibrary
    @StateObject private var picker = BroadcastPickerHolder()
    @AppStorage(Shared.Key.clipSeconds, store: Shared.defaults) private var clipSeconds = 30
    @AppStorage(Shared.Key.includeMic, store: Shared.defaults) private var includeMic = true
    @AppStorage(Shared.Key.quality, store: Shared.defaults) private var quality = 1080
    @State private var savingNow = false
    @State private var toast: String?

    private let columns = [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Clutch").font(.largeTitle.bold())
                        Text("Save your best mobile game moments.").foregroundStyle(.secondary)
                    }
                    if !Shared.isGroupConfigured { groupWarning }
                    statusCard
                    settingsCard
                    howToCard
                    clipsSection
                }
                .padding(16)
            }
            .background(Color.black)
            .navigationDestination(for: Clip.self) { ClipDetailView(clip: $0) }
            .overlay(alignment: .bottom) {
                if let toast {
                    Text(toast)
                        .padding(.horizontal, 16).padding(.vertical, 10)
                        .background(.thinMaterial, in: Capsule())
                        .padding(.bottom, 24)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .alert("Something went wrong", isPresented: Binding(
                get: { library.lastError != nil }, set: { if !$0 { library.lastError = nil } }
            )) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(library.lastError ?? "")
            }
        }
    }

    private var groupWarning: some View {
        Label("The App Group isn't set up, so the recorder can't hand clips to the app. See the setup steps in the README.",
              systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .padding(14)
            .background(Color.orange.opacity(0.2), in: RoundedRectangle(cornerRadius: 16))
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Circle().fill(library.isRecording ? Theme.green : Color.gray).frame(width: 12, height: 12)
                Text(library.isRecording ? "Replay is on" : "Replay is off").font(.title3.bold())
            }
            Text(library.isRecording
                 ? "Clutch is keeping the last \(clipSeconds)s. Go play, then save when something great happens."
                 : "Start replay, then open your game. Clutch keeps the last \(clipSeconds)s, so you only save the good bits.")
                .font(.subheadline).foregroundStyle(.secondary)
            Button {
                picker.show()
            } label: {
                Text(library.isRecording ? "Stop replay" : "Start replay")
                    .font(.headline).frame(maxWidth: .infinity).padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent)
            .background(BroadcastPickerHost(holder: picker).frame(width: 1, height: 1).opacity(0.011))
            if library.isRecording {
                Button {
                    Task {
                        savingNow = true
                        let ok = await library.saveNow()
                        savingNow = false
                        flash(ok ? "Clip saved" : "Couldn't save a clip")
                    }
                } label: {
                    Text(savingNow ? "Saving…" : "Save clip now")
                        .font(.headline).frame(maxWidth: .infinity).padding(.vertical, 10)
                }
                .buttonStyle(.bordered)
                .disabled(savingNow)
            }
            if !library.isRecording {
                Text("In the sheet that opens, pick Clutch, turn the microphone on or off, then tap Start Broadcast.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 20))
    }

    private var settingsCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Clip length").font(.subheadline).foregroundStyle(.secondary)
            Picker("Clip length", selection: $clipSeconds) {
                Text("15s").tag(15); Text("30s").tag(30); Text("60s").tag(60); Text("90s").tag(90); Text("2 min").tag(120)
            }
            .pickerStyle(.segmented)
            Text("Quality").font(.subheadline).foregroundStyle(.secondary)
            Picker("Quality", selection: $quality) {
                Text("720p").tag(720); Text("1080p").tag(1080)
            }
            .pickerStyle(.segmented)
            Toggle(isOn: $includeMic) {
                VStack(alignment: .leading) {
                    Text("Microphone")
                    Text("Also turn on the mic in the Start sheet.").font(.footnote).foregroundStyle(.secondary)
                }
            }
            Text("Quality and microphone changes apply the next time you start replay.")
                .font(.footnote).foregroundStyle(.secondary)
        }
        .padding(16)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 20))
    }

    private var howToCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Save mid-game with Back Tap").font(.headline)
            VStack(alignment: .leading, spacing: 6) {
                step(1, "Open Settings › Accessibility › Touch › Back Tap.")
                step(2, "Choose Double Tap, scroll to Shortcuts, and pick Save Clip.")
                step(3, "While playing, tap the back of your iPhone twice to save.")
            }
            Text("You can also put Save Clip on the Action Button (Settings › Action Button › Shortcut), or say \"Hey Siri, save a clip in Clutch\".")
                .font(.footnote).foregroundStyle(.secondary)
            Text("Tip: add Screen Recording to Control Center. Long-press it and pick Clutch to start replay without opening the app.")
                .font(.footnote).foregroundStyle(.secondary)
        }
        .padding(16)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 20))
    }

    private func step(_ n: Int, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text("\(n)").font(.caption.bold()).frame(width: 22, height: 22)
                .background(Theme.accent, in: Circle())
            Text(text).font(.subheadline)
        }
    }

    private var clipsSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(library.clips.isEmpty ? "Your clips" : "Your clips (\(library.clips.count))").font(.title2.bold())
                .padding(.top, 8)
            if library.pendingCount > 0 {
                HStack(spacing: 8) {
                    ProgressView()
                    Text(library.pendingCount == 1 ? "Finishing 1 clip…" : "Finishing \(library.pendingCount) clips…")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
            }
            if library.clips.isEmpty && library.pendingCount == 0 {
                Text("No clips yet. Start replay, play a game, and save a clip when something great happens.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            LazyVGrid(columns: columns, spacing: 14) {
                ForEach(library.clips) { clip in
                    NavigationLink(value: clip) { ClipTile(clip: clip) }
                        .buttonStyle(.plain)
                }
            }
        }
    }

    private func flash(_ message: String) {
        withAnimation { toast = message }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { withAnimation { toast = nil } }
    }
}

struct ClipTile: View {
    @EnvironmentObject private var library: ClipLibrary
    let clip: Clip
    @State private var image: UIImage?
    @State private var duration: Double = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ZStack(alignment: .bottomTrailing) {
                Rectangle().fill(Theme.card)
                    .aspectRatio(16 / 10, contentMode: .fit)
                    .overlay {
                        if let image {
                            Image(uiImage: image).resizable().scaledToFill()
                        }
                    }
                    .clipped()
                if duration > 0 {
                    Text(formatTime(duration)).font(.caption.bold())
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 6))
                        .padding(6)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 14))
            Text(clip.name).font(.subheadline).lineLimit(1)
            Text(clip.date, style: .date).font(.caption).foregroundStyle(.secondary)
        }
        .task(id: clip.url) {
            image = await library.thumbnail(for: clip)
            duration = await library.duration(of: clip)
        }
    }
}

func formatTime(_ seconds: Double) -> String {
    let s = Int(seconds.rounded())
    return String(format: "%d:%02d", s / 60, s % 60)
}
