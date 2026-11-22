import AVKit
import SwiftUI

/// Watch one clip, trim it, share it, save it to Photos, rename or delete it.
struct ClipDetailView: View {
    @EnvironmentObject private var library: ClipLibrary
    @Environment(\.dismiss) private var dismiss
    @State var clip: Clip
    @State private var player = AVPlayer()
    @State private var duration: Double = 0
    @State private var start: Double = 0
    @State private var end: Double = 0
    @State private var busy = false
    @State private var message: String?
    @State private var renaming = false
    @State private var newName = ""
    @State private var confirmDelete = false
    @State private var loopObserver: Any?

    var body: some View {
        VStack(spacing: 14) {
            VideoPlayer(player: player)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.black)
                .clipShape(RoundedRectangle(cornerRadius: 14))

            if duration > 0 {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Trim · \(formatTime(start)) – \(formatTime(end)) · \(formatTime(end - start)) long")
                        .font(.subheadline).foregroundStyle(.secondary)
                    HStack {
                        Text("Start").frame(width: 44, alignment: .leading)
                        Slider(value: $start, in: 0...duration) { editing in
                            if !editing { seek(start) }
                        }
                        .onChange(of: start) { v in
                            if v > end - 0.5 { start = max(0, end - 0.5) }
                            seek(start)
                        }
                    }
                    HStack {
                        Text("End").frame(width: 44, alignment: .leading)
                        Slider(value: $end, in: 0...duration)
                            .onChange(of: end) { v in
                                if v < start + 0.5 { end = min(duration, start + 0.5) }
                                seek(max(0, end - 1.5))
                            }
                    }
                    Button {
                        Task { await saveTrim() }
                    } label: {
                        Text(busy ? "Working…" : "Save trimmed copy").frame(maxWidth: .infinity).padding(.vertical, 8)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || (start < 0.2 && end > duration - 0.2))
                }
            }

            HStack(spacing: 10) {
                ShareLink(item: clip.url) {
                    Label("Share", systemImage: "square.and.arrow.up").frame(maxWidth: .infinity)
                }
                Button {
                    Task {
                        let ok = await library.saveToPhotos(clip)
                        message = ok ? "Saved to Photos." : "Couldn't save to Photos. Check Settings › Clutch › Photos."
                    }
                } label: {
                    Label("Photos", systemImage: "photo").frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.bordered)
        }
        .padding(16)
        .background(Color.black)
        .navigationTitle(clip.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button { newName = clip.name; renaming = true } label: { Label("Rename", systemImage: "pencil") }
                    Button(role: .destructive) { confirmDelete = true } label: { Label("Delete", systemImage: "trash") }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
        }
        .alert("Rename clip", isPresented: $renaming) {
            TextField("Name", text: $newName)
            Button("Rename") {
                if let renamed = library.rename(clip, to: newName) { load(renamed) }
            }
            Button("Cancel", role: .cancel) {}
        }
        .confirmationDialog("Delete this clip?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                player.pause()
                library.delete(clip)
                dismiss()
            }
        } message: {
            Text("Copies you saved to Photos stay.")
        }
        .alert(message ?? "", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
            Button("OK", role: .cancel) {}
        }
        .task { load(clip) }
        .onDisappear {
            player.pause()
            if let loopObserver { player.removeTimeObserver(loopObserver) }
            loopObserver = nil
        }
    }

    private func load(_ c: Clip) {
        clip = c
        player.replaceCurrentItem(with: AVPlayerItem(url: c.url))
        Task {
            let d = await library.duration(of: c)
            duration = d
            start = 0
            end = d
            if loopObserver == nil {
                // Play only the part between Start and End, over and over.
                loopObserver = player.addPeriodicTimeObserver(
                    forInterval: CMTime(seconds: 0.1, preferredTimescale: 600), queue: .main
                ) { time in
                    if end > 0, time.seconds >= end { seek(start) }
                }
            }
            player.play()
        }
    }

    private func seek(_ t: Double) {
        player.seek(to: CMTime(seconds: t, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    private func saveTrim() async {
        busy = true
        defer { busy = false }
        do {
            let trimmed = try await library.trim(clip, start: start, end: end)
            load(trimmed)
            message = "Saved as \"\(trimmed.name)\"."
        } catch {
            message = "Couldn't trim this clip: \(error.localizedDescription)"
        }
    }
}
