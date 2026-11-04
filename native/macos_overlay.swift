// Display-only overlay. Protocol: NDJSON snapshots on stdin, clicked row IDs on stdout.
// No capture, model calls, network access, or settings are implemented here.
import AppKit
import SwiftUI
import Carbon

struct TranscriptLine: Decodable, Identifiable {
    let id: String
    let speaker: String
    let text: String
}
struct QuestionRow: Decodable, Identifiable {
    let id: String
    let question: String
    let answer: String?
    let busy: Bool?
    let streaming: Bool?
    let completedOnly: Bool?
    let warnings: [String]?
    let sources: [String]?
    init(id: String, question: String, answer: String? = nil, busy: Bool? = nil, streaming: Bool? = nil, completedOnly: Bool? = nil, warnings: [String]? = nil, sources: [String]? = nil) {
        self.id = id; self.question = question; self.answer = answer; self.busy = busy; self.streaming = streaming; self.completedOnly = completedOnly; self.warnings = warnings; self.sources = sources
    }
    enum CodingKeys: String, CodingKey { case id, question, answer, busy, streaming, completedOnly = "completed_only", warnings, sources }
}
struct Snapshot: Decodable {
    var transcript: [TranscriptLine] = []
    var questions: [QuestionRow] = []
    var message: String? = nil
    var shareWarning: String? = nil
    var listening: Bool? = nil
    var truncated: Bool? = nil
    init(transcript: [TranscriptLine] = [], questions: [QuestionRow] = [], message: String? = nil, shareWarning: String? = nil, listening: Bool? = nil, truncated: Bool? = nil) {
        self.transcript = transcript; self.questions = questions; self.message = message; self.shareWarning = shareWarning; self.listening = listening; self.truncated = truncated
    }
    enum CodingKeys: String, CodingKey { case transcript, questions, message, shareWarning = "share_warning", listening, truncated }
    init(from decoder: Decoder) throws {
        let box = try decoder.container(keyedBy: CodingKeys.self)
        transcript = (try? box.decode([TranscriptLine].self, forKey: .transcript)) ?? []
        questions = (try? box.decode([QuestionRow].self, forKey: .questions)) ?? []
        message = try? box.decode(String.self, forKey: .message)
        shareWarning = try? box.decode(String.self, forKey: .shareWarning)
        listening = try? box.decode(Bool.self, forKey: .listening)
        truncated = try? box.decode(Bool.self, forKey: .truncated)
    }
}

// Bounded presentation state, kept separate from the views for deterministic tests.
struct OverlayState {
    private(set) var transcript: [TranscriptLine] = []
    private(set) var questions: [QuestionRow] = []
    private(set) var message: String? = nil
    private(set) var shareWarning: String? = nil
    private(set) var listening: Bool = true
    private(set) var truncated: Bool = false
    mutating func update(_ snapshot: Snapshot) {
        transcript = Array(snapshot.transcript.suffix(6))
        questions = Array(snapshot.questions.suffix(20))
        message = snapshot.message
        shareWarning = snapshot.shareWarning
        if let listening = snapshot.listening { self.listening = listening }
        if let truncated = snapshot.truncated { self.truncated = truncated }
    }
    func clickable(_ id: String) -> Bool {
        guard listening else { return false }
        return questions.contains { $0.id == id && $0.busy != true }
    }
}

func clickEvent(id: String, interactive: Bool, state: OverlayState) -> String? {
    guard interactive, state.clickable(id),
          let data = try? JSONSerialization.data(withJSONObject: ["type": "click", "id": id]) else { return nil }
    return String(data: data, encoding: .utf8)
}

final class OverlayModel: ObservableObject {
    @Published var state = OverlayState()
    @Published var interactive = false
    func apply(_ snapshot: Snapshot) { state.update(snapshot) }
    func click(_ id: String) {
        guard let line = clickEvent(id: id, interactive: interactive, state: state) else { return }
        print(line); fflush(stdout)
    }
}

struct OverlayView: View {
    @ObservedObject var model: OverlayModel
    // Same stable per-speaker palette as the pane: first speaker accent,
    // then green/orange/pink/blue/purple by first appearance.
    private var speakerColors: [String: Color] {
        let palette: [Color] = [.accentColor, .green, .orange, .pink, .blue, .purple]
        var map: [String: Color] = [:]
        for line in model.state.transcript where map[line.speaker] == nil {
            map[line.speaker] = palette[map.count % palette.count]
        }
        return map
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Meeting Copilot").font(.headline)
                Spacer()
                Text(model.interactive ? "Interactive · ~10s · press shortcut again to return early" : "Click-through · press ⌃⌥⌘O to interact")
                    .font(.caption).foregroundColor(.secondary)
                    .accessibilityLabel(model.interactive ? "Interactive mode, returns to click-through in about 10 seconds, or press the shortcut again to return early" : "Click-through mode, press Control Option Command O to interact")
            }
            .accessibilityElement(children: .combine)
            if let message = model.state.message { Text(message).font(.caption).accessibilityLabel("Status: \(message)") }
            if let warning = model.state.shareWarning { Text(warning).font(.caption).foregroundColor(.secondary).accessibilityLabel("Privacy notice: \(warning)") }
            if model.state.truncated { Text("Long answers shortened; open the pane for full text.").font(.caption).foregroundColor(.secondary) }
            ForEach(model.state.transcript) { line in
                HStack(alignment: .top, spacing: 6) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(speakerColors[line.speaker] ?? .accentColor)
                        .frame(width: 3)
                    Text("\(line.speaker): \(line.text)").font(.caption).lineLimit(2)
                }
                .padding(6)
                .background((speakerColors[line.speaker] ?? .accentColor).opacity(0.08))
                .cornerRadius(6)
            }
            Divider()
            if model.state.questions.isEmpty {
                Text(model.state.listening ? "Listening — questions you ask will appear here. Tap one to get its answer." : "Paused — resume capture in the Meeting Copilot pane to detect questions.")
                    .font(.caption).foregroundColor(.secondary)
                    .accessibilityLabel(model.state.listening ? "Listening, no questions detected yet" : "Paused, resume capture in the pane")
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(model.state.questions) { row in
                        VStack(alignment: .leading, spacing: 4) {
                            Button(action: { model.click(row.id) }) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(row.question).font(.subheadline).bold()
                                    if row.busy == true {
                                        Text(row.streaming == true ? "Streaming answer…" : "Preparing answer…").font(.caption)
                                    }
                                    if let answer = row.answer {
                                        Text(answer).font(.caption).textSelection(.enabled)
                                    }
                                    if let warnings = row.warnings {
                                        ForEach(warnings, id: \.self) { warning in
                                            Text("⚠ \(warning)").font(.caption).foregroundColor(.orange)
                                                .accessibilityLabel("Warning: \(warning)")
                                        }
                                    }
                                    if let sources = row.sources, !sources.isEmpty {
                                        Text("Sources: \(sources.joined(separator: " · "))").font(.caption).foregroundColor(.secondary)
                                            .accessibilityLabel("Sources: \(sources.joined(separator: ", "))")
                                    }
                                }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
                                .background(Color.primary.opacity(0.05))
                                .cornerRadius(8)
                            }.buttonStyle(.plain).disabled(!model.interactive || !model.state.listening || row.busy == true)
                            .accessibilityLabel("Ask: \(row.question)")
                            .accessibilityHint(model.state.listening ? "Gets an answer for this question" : "Resume capture in the pane to enable")
                            if row.answer != nil {
                                Button(action: {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(row.answer ?? "", forType: .string)
                                }) {
                                    Text("Copy answer").font(.caption)
                                }.buttonStyle(.link)
                                .disabled(!model.interactive)
                                .accessibilityLabel("Copy answer for \(row.question)")
                            }
                        }
                    }
                }
            }
        }.padding(14).frame(minWidth: 380, minHeight: 180).foregroundColor(.primary)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
    }
}

final class Controller: NSObject {
    let model = OverlayModel()
    var window: NSPanel!
    var timer: Timer?
    var hotKey: EventHotKeyRef?
    var eventHandler: EventHandlerRef?
    func start() {
        window = NSPanel(contentRect: NSRect(x: 80, y: 160, width: 440, height: 380),
                         styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.level = .floating
        // Best effort only: Apple documents sharingType.none as legacy and says
        // not to rely on it for hiding captured content. The pane warns that the
        // overlay may be visible in screen shares; never claim privacy.
        window.sharingType = .none
        window.isMovableByWindowBackground = true
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let material = NSVisualEffectView(frame: window.contentView!.bounds)
        material.material = .hudWindow
        material.blendingMode = .behindWindow
        material.state = .active
        material.wantsLayer = true
        material.layer?.cornerRadius = 12
        material.layer?.masksToBounds = true
        let hosting = NSHostingView(rootView: OverlayView(model: model))
        hosting.translatesAutoresizingMaskIntoConstraints = false
        material.addSubview(hosting)
        NSLayoutConstraint.activate([
            hosting.leadingAnchor.constraint(equalTo: material.leadingAnchor),
            hosting.trailingAnchor.constraint(equalTo: material.trailingAnchor),
            hosting.topAnchor.constraint(equalTo: material.topAnchor),
            hosting.bottomAnchor.constraint(equalTo: material.bottomAnchor)
        ])
        window.contentView = material
        window.orderFrontRegardless()
        registerHotkey()
        // stdin EOF (bridge stop/hide/disconnect) terminates the app: no orphan
        // window survives pane unmount. Oversized frames are skipped, never stored.
        DispatchQueue.global(qos: .utility).async { [weak self] in
            while let line = readLine() {
                guard line.utf8.count <= 500_000, let data = line.data(using: .utf8),
                      let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else { continue }
                DispatchQueue.main.async { self?.model.apply(snapshot) }
            }
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: nil) { [weak self] _ in
            self?.timer?.invalidate()
            self?.window.ignoresMouseEvents = true
        }
    }
    func registerHotkey() {
        var event = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let pointer = Unmanaged.passUnretained(self).toOpaque()
        let handler: EventHandlerUPP = { _, _, context in
            guard let context = context else { return OSStatus(eventNotHandledErr) }
            let controller = Unmanaged<Controller>.fromOpaque(context).takeUnretainedValue()
            controller.toggleInteraction()
            return noErr
        }
        guard InstallEventHandler(GetApplicationEventTarget(), handler, 1, &event, pointer, &eventHandler) == noErr else {
            if let line = try? JSONSerialization.data(withJSONObject: ["type": "error", "code": "hotkey"]) , let text = String(data: line, encoding: .utf8) { print(text); fflush(stdout) }
            model.state = OverlayState(); return
        }
        let keyID = EventHotKeyID(signature: OSType(0x4D43504F), id: 1)
        if RegisterEventHotKey(UInt32(kVK_ANSI_O), UInt32(controlKey | optionKey | cmdKey), keyID,
                               GetApplicationEventTarget(), 0, &hotKey) != noErr {
            // Fail closed: the overlay remains click-through if the shortcut is unavailable.
            // stderr is discarded by the bridge, so also report on stdout for the pane.
            if let line = try? JSONSerialization.data(withJSONObject: ["type": "error", "code": "hotkey"]) , let text = String(data: line, encoding: .utf8) { print(text); fflush(stdout) }
            fputs("Overlay hotkey unavailable; remaining click-through.\n", stderr)
        }
    }
    func toggleInteraction() {
        timer?.invalidate()
        model.interactive.toggle()
        window.ignoresMouseEvents = !model.interactive
        if model.interactive {
            timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: false) { [weak self] _ in
                self?.model.interactive = false
                self?.window.ignoresMouseEvents = true
            }
        }
    }
}

func selfTest() -> Bool {
    let lines = (0..<8).map { TranscriptLine(id: String($0), speaker: "s", text: String($0)) }
    let rows = (0..<22).map { QuestionRow(id: String($0), question: "q", answer: nil, busy: $0 == 21) }
    var state = OverlayState()
    state.update(Snapshot(transcript: lines, questions: rows, message: nil))
    guard state.transcript.count == 6, state.transcript.first?.id == "2",
          state.questions.count == 20, state.questions.first?.id == "2",
          state.clickable("20"), !state.clickable("21"), !state.clickable("missing"),
          clickEvent(id: "20", interactive: false, state: state) == nil,
          clickEvent(id: "21", interactive: true, state: state) == nil,
          let event = clickEvent(id: "20", interactive: true, state: state),
          let eventData = event.data(using: .utf8),
          let decoded = try? JSONSerialization.jsonObject(with: eventData) as? [String: String],
          decoded["type"] == "click", decoded["id"] == "20" else { return false }
    let wire = #"{"type":"snapshot","transcript":[{"id":"a","speaker":"remote","text":"hello"}],"questions":[{"id":"q","question":"Why?","answer":"Because","busy":true,"streaming":true,"completed_only":false}],"share_warning":"Overlay may be visible."}"#
    guard let decodedSnapshot = try? JSONDecoder().decode(Snapshot.self, from: Data(wire.utf8)),
          decodedSnapshot.questions.first?.answer == "Because",
          decodedSnapshot.questions.first?.streaming == true,
          decodedSnapshot.questions.first?.completedOnly == false,
          decodedSnapshot.shareWarning == "Overlay may be visible." else { return false }
    state.update(decodedSnapshot)
    guard state.transcript.first?.text == "hello", !state.clickable("q"),
          state.shareWarning == "Overlay may be visible." else { return false }
    let completedWire = #"{"questions":[{"id":"c","question":"Why?","answer":"Done.","busy":false,"completed_only":true}]}"#
    guard let completedSnapshot = try? JSONDecoder().decode(Snapshot.self, from: Data(completedWire.utf8)),
          completedSnapshot.questions.first?.completedOnly == true,
          completedSnapshot.shareWarning == nil else { return false }
    state.update(Snapshot(transcript: [], questions: [], message: "paused"))
    return state.transcript.isEmpty && state.questions.isEmpty && state.message == "paused"
}

@main struct Main {
    static func main() {
        if CommandLine.arguments.dropFirst().elementsEqual(["--self-test"]) {
            if selfTest() { print("overlay state tests passed"); return }
            exit(1)
        }
        guard CommandLine.arguments.count == 1 else { fputs("Invalid arguments\n", stderr); exit(2) }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let controller = Controller()
        controller.start()
        withExtendedLifetime(controller) { app.run() }
    }
}
