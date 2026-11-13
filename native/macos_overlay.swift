// Display-only overlay. Protocol: meeting-overlay/2 NDJSON snapshots on stdin.
// No capture, model calls, network access, credentials, or persistent settings.
import AppKit
import SwiftUI
import Carbon
import Combine

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
    var status: String? = nil
    init(id: String, question: String, answer: String? = nil, busy: Bool? = nil, streaming: Bool? = nil, completedOnly: Bool? = nil, warnings: [String]? = nil, sources: [String]? = nil) {
        self.id = id; self.question = question; self.answer = answer; self.busy = busy; self.streaming = streaming; self.completedOnly = completedOnly; self.warnings = warnings; self.sources = sources
    }
    enum CodingKeys: String, CodingKey { case id, question, answer, busy, streaming, completedOnly = "completed_only", warnings, sources, status }
}
struct Snapshot: Decodable {
    var transcript: [TranscriptLine] = []
    var questions: [QuestionRow] = []
    var message: String? = nil
    var shareWarning: String? = nil
    var listening: Bool? = nil
    var truncated: Bool? = nil
    var snapshotID: String? = nil
    var answerActions: Bool? = nil
    var focusAnswer: QuestionRow? = nil
    init(transcript: [TranscriptLine] = [], questions: [QuestionRow] = [], message: String? = nil, shareWarning: String? = nil, listening: Bool? = nil, truncated: Bool? = nil, snapshotID: String? = nil, answerActions: Bool? = nil) {
        self.snapshotID = snapshotID
        self.answerActions = answerActions
        self.transcript = transcript; self.questions = questions; self.message = message; self.shareWarning = shareWarning; self.listening = listening; self.truncated = truncated
    }
    enum CodingKeys: String, CodingKey { case transcript, questions, message, shareWarning = "share_warning", listening, truncated, snapshotID = "snapshot_id", answerActions = "answer_actions", focusAnswer = "focus_answer" }
    init(from decoder: Decoder) throws {
        let box = try decoder.container(keyedBy: CodingKeys.self)
        transcript = (try? box.decode([TranscriptLine].self, forKey: .transcript)) ?? []
        questions = (try? box.decode([QuestionRow].self, forKey: .questions)) ?? []
        message = try? box.decode(String.self, forKey: .message)
        shareWarning = try? box.decode(String.self, forKey: .shareWarning)
        listening = try? box.decode(Bool.self, forKey: .listening)
        truncated = try? box.decode(Bool.self, forKey: .truncated)
        snapshotID = try? box.decode(String.self, forKey: .snapshotID)
        answerActions = try? box.decode(Bool.self, forKey: .answerActions)
        focusAnswer = try? box.decode(QuestionRow.self, forKey: .focusAnswer)
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
    private(set) var snapshotID: String? = nil
    private(set) var answerActions: Bool = false
    private(set) var focusAnswer: QuestionRow? = nil
    mutating func update(_ snapshot: Snapshot) {
        snapshotID = snapshot.snapshotID
        focusAnswer = snapshot.focusAnswer
        answerActions = snapshot.answerActions ?? false
        transcript = Array(snapshot.transcript.suffix(6))
        questions = Array(snapshot.questions.suffix(20))
        message = snapshot.message
        shareWarning = snapshot.shareWarning
        if let listening = snapshot.listening { self.listening = listening }
        if let truncated = snapshot.truncated { self.truncated = truncated }
    }
}

// Placement calculations use AppKit coordinates, including negative screen origins.
enum OverlayPlacement: CaseIterable {
    case topLeft, topCenter, topRight, bottomLeft, bottomCenter, bottomRight
    var label: String {
        switch self {
        case .topLeft: return "Top left"
        case .topCenter: return "Top center"
        case .topRight: return "Top right"
        case .bottomLeft: return "Bottom left"
        case .bottomCenter: return "Bottom center"
        case .bottomRight: return "Bottom right"
        }
    }
    var hint: String {
        switch self {
        case .topCenter: return "Near the webcam"
        case .bottomCenter: return "Near the captions"
        default: return label
        }
    }
}

enum OverlayGeometry {
    // The pointer may enter a neighbouring display while a drag is in progress.
    // Select its current visible frame afresh, then clamp the panel into it.
    static func dragVisibleFrame(mouse: NSPoint, screens: [(frame: NSRect, visible: NSRect)],
                                 fallback: NSRect?) -> NSRect? {
        screens.first(where: { $0.frame.contains(mouse) })?.visible ?? fallback
    }
    static func clamp(_ frame: NSRect, to visible: NSRect) -> NSRect {
        let width = max(0, min(frame.width, visible.width))
        let height = max(0, min(frame.height, visible.height))
        return NSRect(x: max(visible.minX, min(frame.minX, visible.maxX - width)),
                      y: max(visible.minY, min(frame.minY, visible.maxY - height)),
                      width: width, height: height)
    }
    static func frame(_ placement: OverlayPlacement, current: NSRect, visible: NSRect) -> NSRect {
        let size = clamp(current, to: visible).size
        let xInset = min(CGFloat(16), max(0, (visible.width - size.width) / 2))
        let yInset = min(CGFloat(16), max(0, (visible.height - size.height) / 2))
        let x: CGFloat
        switch placement {
        case .topLeft, .bottomLeft: x = visible.minX + xInset
        case .topCenter, .bottomCenter: x = visible.midX - size.width / 2
        case .topRight, .bottomRight: x = visible.maxX - size.width - xInset
        }
        let y: CGFloat
        switch placement {
        case .topLeft, .topCenter, .topRight: y = visible.maxY - size.height - yInset
        case .bottomLeft, .bottomCenter, .bottomRight: y = visible.minY + yInset
        }
        return clamp(NSRect(origin: NSPoint(x: x, y: y), size: size), to: visible)
    }
}

// Idle interaction lasts 10 seconds. A drag pauses that deadline but has a hard
// 60-second ceiling, and a lost mouse-up is detected via pressedMouseButtons.
struct InteractionSession {
    private(set) var active = false
    private(set) var dragging = false
    private var idleDeadline: TimeInterval = 0
    private var dragDeadline: TimeInterval = 0
    mutating func start(at now: TimeInterval) {
        active = true; dragging = false; idleDeadline = now + 10
    }
    mutating func stop() { active = false; dragging = false }
    mutating func beginDrag(at now: TimeInterval) {
        guard active else { return }
        dragging = true; dragDeadline = now + 60
    }
    mutating func endDrag(at now: TimeInterval) {
        guard active && dragging else { return }
        dragging = false; idleDeadline = now + 10
    }
    mutating func tick(at now: TimeInterval, mouseDown: Bool) {
        guard active else { return }
        if dragging {
            if now >= dragDeadline { stop() }
            else if !mouseDown { endDrag(at: now) }
        } else if now >= idleDeadline { stop() }
    }
}

final class OverlayModel: ObservableObject {
    @Published var state = OverlayState()
    @Published var interactive = false
    @Published var showInRecordings = false
    var moveTo: (OverlayPlacement) -> Void = { _ in }
    var changeRecording: (Bool) -> Void = { _ in }
    var dragDown: (NSEvent) -> Void = { _ in }
    var dragMove: (NSEvent) -> Void = { _ in }
    var dragUp: (NSEvent) -> Void = { _ in }
    func apply(_ snapshot: Snapshot) { state.update(snapshot) }
}

// Only this view handles drag events. Transcript, text selection and buttons do
// not participate in window movement. The controller moves the actual NSPanel.
final class WindowDragGrip: NSView {
    weak var model: OverlayModel?
    override var mouseDownCanMoveWindow: Bool { false }
    // The floating nonactivating panel is not normally key: dragging must work
    // on the first press without requiring the user to focus another app.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { model?.dragDown(event) }
    override func mouseDragged(with event: NSEvent) { model?.dragMove(event) }
    override func mouseUp(with event: NSEvent) { model?.dragUp(event) }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
}

struct DragGrip: NSViewRepresentable {
    let model: OverlayModel
    func makeNSView(context: Context) -> WindowDragGrip {
        let view = WindowDragGrip()
        view.model = model
        return view
    }
    func updateNSView(_ view: WindowDragGrip, context: Context) { view.model = model }
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
        VStack(alignment: .leading, spacing: 8) {
            Text("Meeting Copilot").font(.headline)
            Text(model.interactive
                 ? "Interactive · click-through ~10s; drag extends · ⌃⌥⌘O ends"
                 : "Click-through · press ⌃⌥⌘O to move or use controls")
                .font(.caption)
                .accessibilityLabel(model.interactive
                    ? "Interactive; returns to click-through in about 10 seconds, but dragging extends it. Press Control Option Command O to finish."
                    : "Click-through. Press Control Option Command O to move or use controls.")
            if model.interactive {
                HStack(spacing: 6) {
                    Image(systemName: "line.3.horizontal")
                    Text("Drag to move").font(.caption.bold())
                }
                .frame(maxWidth: .infinity).padding(6)
                .background(Color.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 5))
                .overlay(DragGrip(model: model))
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Drag to move overlay")
                .accessibilityHint("Drag this grip to move the overlay.")
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(minimum: 0), spacing: 4), count: 3), spacing: 4) {
                    ForEach(OverlayPlacement.allCases, id: \.self) { position in
                        Button(action: { model.moveTo(position) }) {
                            Text(position.label).font(.caption2).lineLimit(1).minimumScaleFactor(0.7)
                                .frame(maxWidth: .infinity)
                        }
                        .help(position.hint)
                        .accessibilityLabel("Move overlay to \(position.label)")
                        .accessibilityHint(position.hint)
                    }
                }
            }
            HStack {
                Toggle("Show in recordings", isOn: Binding(
                    get: { model.showInRecordings },
                    set: { model.changeRecording($0) }))
                    .font(.caption).disabled(!model.interactive)
                Text(model.showInRecordings ? "On" : "Off")
                    .font(.caption.bold()).foregroundColor(.white)
            }
            Text(model.showInRecordings
                 ? "Visibility is requested, not guaranteed. To exclude it, hide it in the pane and verify it is gone."
                 : "Exclusion is best effort; macOS may still record this overlay. Hide it in the pane and verify it is gone.")
                .font(.caption).foregroundColor(.white).fixedSize(horizontal: false, vertical: true)
            Divider()
            // Keep privacy controls pinned. Questions lead the scrollable content
            // so transcript history never hides them in the click-through view.
            ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    if let message = model.state.message {
                        Text(message).font(.caption).lineLimit(2).accessibilityLabel("Status: \(message)")
                    }
                    if model.state.truncated {
                        Text("Long answers shortened; open the pane for full text.").font(.caption)
                    }
                    if model.state.questions.isEmpty && model.state.focusAnswer == nil {
                        Text(model.state.listening ? "Listening - detected questions appear here. Ask in the Meeting Copilot pane." : "Paused - resume capture in the Meeting Copilot pane to detect questions.")
                            .font(.caption).foregroundColor(.secondary)
                            .accessibilityLabel(model.state.listening ? "Listening, no questions detected yet" : "Paused, resume capture in the pane")
                    }
                    if let response = model.state.focusAnswer {
                        responseCard(response)
                    } else if let latest = model.state.questions.last {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Latest question").font(.caption).foregroundColor(.secondary)
                            Text(latest.question).font(.system(size: 15, weight: .semibold))
                            Text("Ask in the pane to see your response here.").font(.caption)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                            .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                    }
                    if !model.state.questions.isEmpty || !model.state.transcript.isEmpty {
                        DisclosureGroup("Recent context") {
                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(model.state.questions.reversed()) { row in
                                    Text(row.question).font(.caption).padding(.vertical, 3)
                                }
                                if !model.state.transcript.isEmpty {
                                    Divider()
                                    Text("Recent transcript").font(.caption.bold()).foregroundColor(.secondary)
                                }
                                ForEach(model.state.transcript) { line in
                                    HStack(alignment: .top, spacing: 6) {
                                        RoundedRectangle(cornerRadius: 2)
                                            .fill(speakerColors[line.speaker] ?? .accentColor).frame(width: 3)
                                        Text("\(line.speaker): \(line.text)").font(.caption).lineLimit(2)
                                    }.padding(6)
                                        .background((speakerColors[line.speaker] ?? .accentColor).opacity(0.08))
                                        .cornerRadius(6)
                                }
                            }.padding(.top, 6)
                        }.font(.caption).disabled(!model.interactive)
                    }
                    if let warning = model.state.shareWarning {
                        Text(warning).font(.caption).foregroundColor(.secondary)
                            .accessibilityLabel("Privacy notice: \(warning)")
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
                    .id("response-top")
            }
            .onChange(of: model.state.focusAnswer?.id) { _ in
                scroll.scrollTo("response-top", anchor: .top)
            }
            }
        }
        .padding(14).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .foregroundColor(.primary)
        .background(Color.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 14))
        .preferredColorScheme(.dark)
    }
    // One stable card; partial tokens update in place without moving focus or
    // auto-scrolling. No generated shortening or extra provider request.
    private func responseCard(_ row: QuestionRow) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Latest requested answer").font(.caption.bold())
                Spacer()
                Text(responseStatus(row)).font(.caption)
            }.foregroundColor(.secondary)
            Text(row.question).font(.subheadline).bold()
            if row.status == "cancelled" || row.status == "error" || row.status == "interrupted" {
                Text("Incomplete response. Review or retry in the pane.")
                    .font(.caption).foregroundColor(.orange)
            }
            if let answer = row.answer, !answer.isEmpty {
                Text(answer).font(.system(size: 15)).lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            } else {
                Text(row.busy == true ? "Waiting for the first words…" : "No answer text. Check the pane.")
                    .font(.subheadline).foregroundColor(.secondary)
            }
            if let warnings = row.warnings {
                ForEach(warnings, id: \.self) { warning in
                    Text("⚠ \(warning)").font(.caption).foregroundColor(.orange)
                        .accessibilityLabel("Warning: \(warning)")
                }
            }
            if let sources = row.sources, !sources.isEmpty {
                Text("Sources: \(sources.joined(separator: " · "))").font(.caption).foregroundColor(.secondary)
            }
            Text("AI suggestion · verify in the pane").font(.caption2).foregroundColor(.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
            .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
    }
    private func responseStatus(_ row: QuestionRow) -> String {
        switch row.status {
        case "queued": return "Queued"
        case "running", "streaming": return row.streaming == true ? "Streaming answer…" : "Preparing answer…"
        case "cancelled": return "Cancelled"
        case "error", "interrupted": return "Interrupted"
        case "done": return "Ready to review"
        default: return "Check pane"
        }
    }

}

// Route only the dedicated grip at window level, so the first drag works even
// when this nonactivating panel is not key. Other controls keep AppKit dispatch.
final class OverlayPanel: NSPanel {
    var allowsKeyWindow = false
    private var movingGrip: WindowDragGrip?
    var isTrackingGrip: Bool { movingGrip != nil }
    override var canBecomeKey: Bool { allowsKeyWindow }
    override var canBecomeMain: Bool { false }
    func cancelGripTracking() { movingGrip = nil }
    override func sendEvent(_ event: NSEvent) {
        if !ignoresMouseEvents {
            switch event.type {
            case .leftMouseDown:
                if let root = contentView {
                    let location = root.superview?.convert(event.locationInWindow, from: nil) ?? event.locationInWindow
                    if let grip = root.hitTest(location) as? WindowDragGrip {
                        movingGrip = grip
                        grip.mouseDown(with: event)
                        return
                    }
                }
            case .leftMouseDragged:
                if let grip = movingGrip { grip.mouseDragged(with: event); return }
            case .leftMouseUp:
                if let grip = movingGrip {
                    movingGrip = nil
                    grip.mouseUp(with: event)
                    return
                }
            default: break
            }
        }
        super.sendEvent(event)
    }
}

final class Controller: NSObject {
    static let preferredSize = NSSize(width: 440, height: 380)
    let model = OverlayModel()
    var window: OverlayPanel!
    var timer: Timer?
    var hotKey: EventHotKeyRef?
    var eventHandler: EventHandlerRef?
    private var interaction = InteractionSession()
    private var dragAnchor: (mouse: NSPoint, origin: NSPoint)?
    private var now: TimeInterval { ProcessInfo.processInfo.systemUptime }

    // Separated from start() so the optional diagnostic can exercise the real
    // panel and callbacks without showing a window, reading stdin or a hotkey.
    func configureWindow() {
        window = OverlayPanel(contentRect: NSRect(origin: NSPoint(x: 80, y: 160), size: Self.preferredSize),
                         styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.level = .floating
        // Legacy best effort only; it does not guarantee exclusion from capture.
        window.sharingType = .none
        window.isMovableByWindowBackground = false
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        model.moveTo = { [weak self] in self?.moveTo($0) }
        model.changeRecording = { [weak self] in self?.changeRecording($0) }
        model.dragDown = { [weak self] in self?.beginDrag($0) }
        model.dragMove = { [weak self] in self?.moveDrag($0) }
        model.dragUp = { [weak self] _ in self?.endDrag() }
        let hosting = NSHostingView(rootView: OverlayView(model: model))
        if #available(macOS 13.0, *) { hosting.sizingOptions = [] }
        window.contentView = hosting
        if let visible = currentVisibleFrame() {
            window.setFrame(OverlayGeometry.clamp(window.frame, to: visible), display: false)
        }
    }
    func start() {
        configureWindow()
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
    func currentVisibleFrame() -> NSRect? {
        (window.screen ?? NSScreen.main)?.visibleFrame
    }
    var preferredFrame: NSRect { NSRect(origin: window.frame.origin, size: Self.preferredSize) }
    func moveTo(_ position: OverlayPlacement) {
        guard interaction.active, let visible = currentVisibleFrame() else { return }
        window.setFrame(OverlayGeometry.frame(position, current: preferredFrame, visible: visible), display: true)
    }
    func changeRecording(_ show: Bool) {
        guard interaction.active else { return }
        if show { window.sharingType = .readOnly }
        else { window.sharingType = .none }
        model.showInRecordings = show
    }
    func beginDrag(_ event: NSEvent) {
        guard interaction.active else { return }
        dragAnchor = (window.convertPoint(toScreen: event.locationInWindow), window.frame.origin)
        interaction.beginDrag(at: now)
    }
    func moveDrag(_ event: NSEvent) {
        guard interaction.active, interaction.dragging, let anchor = dragAnchor else { return }
        let mouse = window.convertPoint(toScreen: event.locationInWindow)
        let screens = NSScreen.screens.map { (frame: $0.frame, visible: $0.visibleFrame) }
        guard let visible = OverlayGeometry.dragVisibleFrame(mouse: mouse, screens: screens,
                                                             fallback: currentVisibleFrame()) else { return }
        var frame = preferredFrame
        frame.origin = NSPoint(x: anchor.origin.x + mouse.x - anchor.mouse.x,
                               y: anchor.origin.y + mouse.y - anchor.mouse.y)
        window.setFrame(OverlayGeometry.clamp(frame, to: visible), display: true)
    }
    func endDrag() {
        interaction.endDrag(at: now)
        dragAnchor = nil
    }
    private func syncInteraction() {
        // Do not republish unchanged state on each polling tick: doing so would
        // rerender the grip repeatedly while AppKit is delivering its drag.
        if model.interactive != interaction.active { model.interactive = interaction.active }
        if window.ignoresMouseEvents == interaction.active { window.ignoresMouseEvents = !interaction.active }
        if !interaction.active, window.isKeyWindow { window.resignKey() }
        window.allowsKeyWindow = interaction.active
        if !interaction.dragging {
            dragAnchor = nil
            window.cancelGripTracking()
        }
        if !interaction.active { timer?.invalidate(); timer = nil }
    }
    func advanceInteraction(at time: TimeInterval, mouseDown: Bool) {
        interaction.tick(at: time, mouseDown: mouseDown)
        syncInteraction()
    }
    func toggleInteraction() {
        if interaction.active { interaction.stop() }
        else { interaction.start(at: now) }
        syncInteraction()
        if interaction.active {
            let clock = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in
                guard let self = self else { return }
                self.advanceInteraction(at: self.now, mouseDown: (NSEvent.pressedMouseButtons & 1) != 0)
            }
            timer = clock
            RunLoop.main.add(clock, forMode: .common)
        }
    }
}

func crossScreenGeometrySelfTest() -> Bool {
    let left = NSRect(x: -1920, y: -200, width: 1920, height: 1080)
    let leftVisible = NSRect(x: -1920, y: -180, width: 1920, height: 1040)
    let right = NSRect(x: 0, y: 0, width: 1440, height: 900)
    let rightVisible = NSRect(x: 0, y: 20, width: 1440, height: 850)
    let screens = [(frame: left, visible: leftVisible), (frame: right, visible: rightVisible)]
    guard OverlayGeometry.dragVisibleFrame(mouse: NSPoint(x: -10, y: 50), screens: screens, fallback: rightVisible) == leftVisible,
          OverlayGeometry.dragVisibleFrame(mouse: NSPoint(x: 10, y: 50), screens: screens, fallback: leftVisible) == rightVisible,
          OverlayGeometry.dragVisibleFrame(mouse: NSPoint(x: 5000, y: 5000), screens: screens, fallback: rightVisible) == rightVisible else { return false }
    let moved = OverlayGeometry.clamp(NSRect(x: -50, y: 0, width: 440, height: 380), to: leftVisible)
    return moved.maxX == leftVisible.maxX && moved.minX < 0
}

func geometryAndInteractionSelfTest() -> Bool {
    let visible = NSRect(x: -1920, y: -220, width: 1920, height: 1080)
    let original = NSRect(x: -1500, y: 0, width: 440, height: 380)
    let topLeft = OverlayGeometry.frame(.topLeft, current: original, visible: visible)
    let topCenter = OverlayGeometry.frame(.topCenter, current: original, visible: visible)
    let topRight = OverlayGeometry.frame(.topRight, current: original, visible: visible)
    let bottomLeft = OverlayGeometry.frame(.bottomLeft, current: original, visible: visible)
    let bottomCenter = OverlayGeometry.frame(.bottomCenter, current: original, visible: visible)
    let bottomRight = OverlayGeometry.frame(.bottomRight, current: original, visible: visible)
    guard OverlayPlacement.allCases.count == 6,
          topLeft.minX == visible.minX + 16, topLeft.maxY == visible.maxY - 16,
          topCenter.midX == visible.midX, topCenter.maxY == visible.maxY - 16,
          topRight.maxX == visible.maxX - 16, topRight.maxY == visible.maxY - 16,
          bottomLeft.minX == visible.minX + 16, bottomLeft.minY == visible.minY + 16,
          bottomCenter.midX == visible.midX, bottomCenter.minY == visible.minY + 16,
          bottomRight.maxX == visible.maxX - 16, bottomRight.minY == visible.minY + 16 else { return false }
    let small = NSRect(x: -400, y: -100, width: 240, height: 100)
    for placement in OverlayPlacement.allCases {
        guard OverlayGeometry.frame(placement, current: original, visible: small) == small else { return false }
    }
    let beyond = NSRect(x: visible.maxX + 100, y: visible.minY - 100, width: 440, height: 380)
    let clamped = OverlayGeometry.clamp(beyond, to: visible)
    guard clamped.maxX == visible.maxX, clamped.minY == visible.minY else { return false }

    var session = InteractionSession()
    session.start(at: 0); session.tick(at: 9.9, mouseDown: false)
    guard session.active else { return false }
    session.tick(at: 10, mouseDown: false)
    guard !session.active else { return false }
    session.start(at: 0); session.beginDrag(at: 9)
    session.tick(at: 20, mouseDown: true)
    guard session.active && session.dragging else { return false }
    session.endDrag(at: 20); session.tick(at: 29.9, mouseDown: false)
    guard session.active && !session.dragging else { return false }
    session.tick(at: 30, mouseDown: false)
    guard !session.active else { return false }
    session.start(at: 0); session.beginDrag(at: 1); session.tick(at: 61, mouseDown: true)
    guard !session.active else { return false }
    session.start(at: 0); session.beginDrag(at: 1); session.tick(at: 3, mouseDown: false)
    guard session.active && !session.dragging else { return false }
    session.tick(at: 13, mouseDown: false)
    return !session.active && crossScreenGeometrySelfTest()
}

func selfTest() -> Bool {
    let lines = (0..<8).map { TranscriptLine(id: String($0), speaker: "s", text: String($0)) }
    let rows = (0..<22).map { QuestionRow(id: String($0), question: "q", answer: nil, busy: $0 == 21) }
    var state = OverlayState()
    // Even if an old snapshot sets answer_actions, this display has no action
    // that initiates an answer. Decoding remains compatible with protocol /2.
    state.update(Snapshot(transcript: lines, questions: rows, message: nil, snapshotID: "meeting:1", answerActions: true))
    guard state.transcript.count == 6, state.transcript.first?.id == "2",
          state.questions.count == 20, state.questions.first?.id == "2",
          state.answerActions, state.snapshotID == "meeting:1" else { return false }
    let wire = #"{"type":"snapshot","snapshot_id":"meeting:2","transcript":[{"id":"a","speaker":"remote","text":"hello"}],"questions":[{"id":"q","question":"Why?","answer":"Because","busy":true,"streaming":true,"completed_only":false}],"share_warning":"Overlay may be visible."}"#
    guard let decodedSnapshot = try? JSONDecoder().decode(Snapshot.self, from: Data(wire.utf8)),
          decodedSnapshot.questions.first?.answer == "Because",
          decodedSnapshot.questions.first?.streaming == true,
          decodedSnapshot.questions.first?.completedOnly == false,
          decodedSnapshot.shareWarning == "Overlay may be visible." else { return false }
    state.update(decodedSnapshot)
    guard state.transcript.first?.text == "hello", !state.answerActions,
          state.shareWarning == "Overlay may be visible.", state.snapshotID == "meeting:2" else { return false }
    let focusWire = #"{"snapshot_id":"meeting:3","focus_answer":{"id":"request-1","question":"When?","answer":"Friday","status":"streaming","busy":true}}"#
    guard let focused = try? JSONDecoder().decode(Snapshot.self, from: Data(focusWire.utf8)) else { return false }
    state.update(focused)
    guard state.focusAnswer?.id == "request-1", state.focusAnswer?.status == "streaming" else { return false }
    state.update(decodedSnapshot)
    guard state.focusAnswer == nil else { return false }
    let completedWire = #"{"questions":[{"id":"c","question":"Why?","answer":"Done.","busy":false,"completed_only":true}]}"#
    guard let completedSnapshot = try? JSONDecoder().decode(Snapshot.self, from: Data(completedWire.utf8)),
          completedSnapshot.questions.first?.completedOnly == true,
          completedSnapshot.shareWarning == nil else { return false }
    state.update(Snapshot(transcript: [], questions: [], message: "paused"))
    return state.transcript.isEmpty && state.questions.isEmpty && state.message == "paused"
        && geometryAndInteractionSelfTest()
}

// Optional diagnostic: real NSPanel and the same callbacks/drag view as the UI,
// but no ordered window, stdin reader, global hotkey, audio, network or secrets.
func presentationSelfTest() -> Bool {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    var checks: [String: Bool] = ["main_thread": Thread.isMainThread]
    let controller = Controller()
    controller.configureWindow()
    defer {
        controller.timer?.invalidate()
        controller.window.orderOut(nil)
        controller.window.close()
    }
    let panel = controller.window!
    checks["default_click_through"] = panel.ignoresMouseEvents && !controller.model.interactive && !panel.isVisible
    checks["recording_default_off"] = panel.sharingType == .none && !controller.model.showInRecordings
    let initiallyNotKeyable = !panel.canBecomeKey
    guard let visible = controller.currentVisibleFrame() else {
        let report: [String: Any] = ["type": "presentation_self_test", "scope": "synthetic_appkit_panel",
                                     "ok": false, "preset_count": 0, "checks": checks, "error": "no_screen"]
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) { print(text) }
        return false
    }
    func near(_ a: CGFloat, _ b: CGFloat) -> Bool { abs(a - b) < 1 }
    func same(_ a: NSRect, _ b: NSRect) -> Bool {
        near(a.minX, b.minX) && near(a.minY, b.minY) && near(a.width, b.width) && near(a.height, b.height)
    }
    controller.toggleInteraction()
    let keyableWhileInteractive = panel.canBecomeKey
    var changeCount = 0
    let observation = controller.model.objectWillChange.sink { _ in changeCount += 1 }
    controller.advanceInteraction(at: ProcessInfo.processInfo.systemUptime + 1, mouseDown: false)
    checks["interaction_poll_stable"] = changeCount == 0
    observation.cancel()
    var presetsOK = !panel.ignoresMouseEvents
    for position in OverlayPlacement.allCases {
        let expected = OverlayGeometry.frame(position, current: panel.frame, visible: visible)
        controller.model.moveTo(position)
        presetsOK = presetsOK && same(panel.frame, expected)
    }
    checks["presets"] = presetsOK
    let normalSize = panel.frame.size
    let smallDisplay = NSRect(x: visible.minX, y: visible.minY, width: 320, height: 200)
    panel.setFrame(OverlayGeometry.clamp(panel.frame, to: smallDisplay), display: false)
    controller.model.moveTo(.topLeft)
    checks["restores_size_after_small_display"] = panel.frame.size == normalSize
    controller.model.changeRecording(true)
    let included = panel.sharingType == .readOnly && controller.model.showInRecordings
    controller.model.changeRecording(false)
    checks["recording_modes"] = included && panel.sharingType == .none && !controller.model.showInRecordings

    controller.model.moveTo(.bottomLeft)
    func findGrip(in view: NSView) -> WindowDragGrip? {
        if let grip = view as? WindowDragGrip { return grip }
        for child in view.subviews {
            if let grip = findGrip(in: child) { return grip }
        }
        return nil
    }
    panel.contentView?.layoutSubtreeIfNeeded()
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
    panel.contentView?.layoutSubtreeIfNeeded()
    let mountedGrip = panel.contentView.flatMap { findGrip(in: $0) }
    checks["mounted_grip"] = mountedGrip != nil
    if let root = panel.contentView, let mounted = mountedGrip {
        let center = mounted.convert(NSPoint(x: mounted.bounds.midX, y: mounted.bounds.midY), to: nil)
        let point = root.superview?.convert(center, from: nil) ?? center
        let hit = root.hitTest(point)
        checks["grip_hit_test"] = hit === mounted || (hit?.isDescendant(of: mounted) ?? false)
    } else { checks["grip_hit_test"] = false }
    // Fallback preserves the other independent AppKit checks if a headless
    // environment cannot materialize SwiftUI's hosted view hierarchy.
    let grip = mountedGrip ?? WindowDragGrip(frame: NSRect(x: 0, y: 0, width: 100, height: 25))
    if mountedGrip == nil { grip.model = controller.model; panel.contentView?.addSubview(grip) }
    func mouse(_ type: NSEvent.EventType, _ point: NSPoint) -> NSEvent? {
        NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: 0,
                           windowNumber: panel.windowNumber, context: nil, eventNumber: 1,
                           clickCount: 1, pressure: 1)
    }
    let before = panel.frame
    // The content padding is not the grip: window-level routing must leave it
    // to AppKit instead of treating the whole panel as a draggable background.
    if let down = mouse(.leftMouseDown, NSPoint(x: 2, y: 2)),
       let moved = mouse(.leftMouseDragged, NSPoint(x: 12, y: 12)),
       let up = mouse(.leftMouseUp, NSPoint(x: 12, y: 12)) {
        panel.sendEvent(down); panel.sendEvent(moved); panel.sendEvent(up)
        checks["non_grip_not_dragging"] = !panel.isTrackingGrip && same(panel.frame, before)
    } else { checks["non_grip_not_dragging"] = false }
    let startPoint = grip.convert(NSPoint(x: grip.bounds.midX, y: grip.bounds.midY), to: nil)
    let dx = min(CGFloat(60), max(0, (visible.width - before.width) / 3))
    let dy = min(CGFloat(60), max(0, (visible.height - before.height) / 3))
    if let down = mouse(.leftMouseDown, startPoint),
       let moved = mouse(.leftMouseDragged, NSPoint(x: startPoint.x + dx, y: startPoint.y + dy)),
       let far = mouse(.leftMouseDragged, NSPoint(x: 100000, y: -100000)),
       let up = mouse(.leftMouseUp, startPoint) {
        panel.sendEvent(down)
        panel.sendEvent(moved)
        checks["drag_moves_panel"] = (dx > 0 || dy > 0) && near(panel.frame.minX, before.minX + dx)
            && near(panel.frame.minY, before.minY + dy)
        panel.sendEvent(far)
        checks["manual_clamped"] = near(panel.frame.maxX, visible.maxX) && near(panel.frame.minY, visible.minY)
        panel.sendEvent(up)
    } else {
        checks["drag_moves_panel"] = false; checks["manual_clamped"] = false
    }
    checks["cross_screen_geometry"] = crossScreenGeometrySelfTest() // Pure synthetic screen rectangles.
    let positioned = panel.frame
    controller.model.changeRecording(true)
    controller.model.apply(Snapshot(message: "synthetic", snapshotID: "diagnostic"))
    controller.model.apply(Snapshot(message: "synthetic update", snapshotID: "diagnostic"))
    checks["snapshot_keeps_placement"] = same(panel.frame, positioned)
    checks["snapshot_keeps_recording_mode"] = panel.sharingType == .readOnly && controller.model.showInRecordings
    controller.model.changeRecording(false)
    controller.advanceInteraction(at: ProcessInfo.processInfo.systemUptime + 11, mouseDown: false)
    checks["interaction_timeout"] = panel.ignoresMouseEvents && !controller.model.interactive
    checks["temporary_key_capability"] = initiallyNotKeyable && keyableWhileInteractive && !panel.canBecomeKey
    controller.toggleInteraction()
    if let down = mouse(.leftMouseDown, startPoint) {
        panel.sendEvent(down)
        let tracked = panel.isTrackingGrip
        controller.advanceInteraction(at: ProcessInfo.processInfo.systemUptime + 1, mouseDown: false)
        checks["lost_mouse_up_releases_grip"] = tracked && !panel.isTrackingGrip && controller.model.interactive
        panel.sendEvent(down)
        let clock = ProcessInfo.processInfo.systemUptime
        controller.advanceInteraction(at: clock + 11, mouseDown: true)
        let held = !panel.ignoresMouseEvents && controller.model.interactive
        controller.advanceInteraction(at: clock + 61, mouseDown: true)
        checks["drag_timeout"] = held && panel.ignoresMouseEvents && !controller.model.interactive && !panel.isTrackingGrip
    } else {
        checks["lost_mouse_up_releases_grip"] = false
        checks["drag_timeout"] = false
    }
    let ok = checks.values.allSatisfy { $0 }
    let report: [String: Any] = ["type": "presentation_self_test", "scope": "synthetic_appkit_panel",
                                 "ok": ok, "preset_count": OverlayPlacement.allCases.count, "checks": checks]
    guard let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]),
          let text = String(data: data, encoding: .utf8) else { return false }
    print(text)
    return ok
}

@main struct Main {
    static func main() {
        if CommandLine.arguments.dropFirst().elementsEqual(["--protocol-version"]) {
            print("meeting-overlay/2"); return
        }
        if CommandLine.arguments.dropFirst().elementsEqual(["--self-test"]) {
            if selfTest() { print("overlay state tests passed"); return }
            exit(1)
        }
        if CommandLine.arguments.dropFirst().elementsEqual(["--presentation-self-test"]) {
            if presentationSelfTest() { return }
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
