// Native opt-in capture. stdout: bounded NDJSON, mono float32 little-endian 16kHz.
// Compile: swiftc -parse-as-library native/macos_capture.swift -o macos_capture
// Production packaging should embed NSMicrophoneUsageDescription in __TEXT,__info_plist.
import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia
import CoreGraphics

private let writer = DispatchQueue(label: "meeting.audio.stdout")
private let slots = DispatchSemaphore(value: 4)
private let dropCounter = DispatchQueue(label: "meeting.audio.drops")
private var droppedFrames = 0

// A slow reader drops audio rather than blocking real-time capture or growing memory.
// Drops are counted and the cumulative total rides on the next successful frame
// so no loss point stays invisible to the transcriber.
private func emit(_ object: [String: Any], critical: Bool = false) {
    if slots.wait(timeout: .now()) != .success {
        if critical { exit(2) }
        dropCounter.sync { droppedFrames += 1 }
        return
    }
    var payload = object
    let pending = dropCounter.sync { () -> Int in let n = droppedFrames; droppedFrames = 0; return n }
    if pending > 0 {
        payload["native_dropped_chunks"] = pending
        payload["warning"] = "Native capture dropped \(pending) frame(s) under backpressure; some audio may be missing."
    }
    writer.async { [payload, pending] in
        defer { slots.signal() }
        guard let data = try? JSONSerialization.data(withJSONObject: payload), data.count < 100000 else {
            // Never silently discard a loss report: re-queue it for the next frame.
            if pending > 0 { dropCounter.sync { droppedFrames += pending } }
            return
        }
        var line = data
        line.append(10)
        do { try FileHandle.standardOutput.write(contentsOf: line) }
        catch { exit(0) }
    }
}

private func fail(_ code: String) -> Never {
    // Terminal errors must carry pending loss evidence even when the writer
    // queue is saturated: try the ordered path briefly, then write directly
    // so the drop count still reaches the transcriber before exit.
    var payload: [String: Any] = ["type": "error", "code": code]
    let pending = dropCounter.sync { () -> Int in let n = droppedFrames; droppedFrames = 0; return n }
    if pending > 0 {
        payload["native_dropped_chunks"] = pending
        payload["warning"] = "Native capture dropped \(pending) frame(s) under backpressure; some audio may be missing."
    }
    if let data = try? JSONSerialization.data(withJSONObject: payload) {
        var line = data
        line.append(10)
        if slots.wait(timeout: .now() + 0.25) == .success {
            writer.async { [line] in defer { slots.signal() }; try? FileHandle.standardOutput.write(contentsOf: line) }
            writer.sync {}
        } else {
            try? FileHandle.standardOutput.write(contentsOf: line)
        }
    }
    exit(1)
}

private var terminating = false

private final class Converter {
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?
    private let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!

    func consume(_ buffer: AVAudioPCMBuffer, source: String) {
        // After the SIGTERM barrier no new audio may enter the writer
        // queue: count it as dropped so the terminal line stays honest.
        if terminating {
            dropCounter.sync { droppedFrames += 1 }
            return
        }
        guard buffer.frameLength > 0, buffer.frameLength <= 192000,
              buffer.format.sampleRate > 0 else { return }
        if inputFormat != buffer.format {
            inputFormat = buffer.format
            converter = AVAudioConverter(from: buffer.format, to: outputFormat)
        }
        guard let converter = converter else { fail("capture") }
        let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * 16000 / buffer.format.sampleRate) + 128)
        guard capacity <= 192000, let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        let result = converter.convert(to: output, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        guard result != .error, error == nil, let channel = output.floatChannelData?[0] else { fail("capture") }
        var offset = 0
        let total = Int(output.frameLength)
        let start = Date().timeIntervalSince1970 - Double(total) / 16000
        while offset < total {
            let count = min(8000, total - offset)
            let data = Data(bytes: channel.advanced(by: offset), count: count * 4)
            emit(["type": "pcm", "source": source, "rate": 16000, "samples": count,
                  "timestamp": start + Double(offset) / 16000, "pcm": data.base64EncodedString()])
            offset += count
        }
    }
}

private final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    let engine = AVAudioEngine()
    let micConverter = Converter()
    let systemConverter = Converter()
    let audioQueue = DispatchQueue(label: "meeting.audio.system")
    var stream: SCStream?
    var micSink: AVAudioSinkNode?

    func microphone() async {
        guard Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String != nil else { fail("microphone_usage") }
        let authorized = await AVCaptureDevice.requestAccess(for: .audio)
        guard authorized else { fail("microphone_permission") }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { fail("capture") }
        // AVAudioSinkNode replaces the deprecated installTap API: the engine
        // pulls microphone audio into the sink, which forwards it unchanged.
        // NOTE: the incoming AudioBufferList is variable-length; index it in
        // place rather than copying the struct, or trailing buffers are lost.
        let sink = AVAudioSinkNode { [weak self] _, frameCount, audioBufferList in
            guard let self, frameCount > 0 else { return noErr }
            guard audioBufferList.pointee.mNumberBuffers > 0,
                  let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else { return noErr }
            buffer.frameLength = frameCount
            let sources = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: audioBufferList))
            let targets = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
            for index in 0..<min(sources.count, targets.count) {
                let bytes = min(Int(sources[index].mDataByteSize), Int(targets[index].mDataByteSize))
                if let from = sources[index].mData, let to = targets[index].mData, bytes > 0 {
                    to.copyMemory(from: from, byteCount: bytes)
                }
            }
            self.micConverter.consume(buffer, source: "microphone")
            return noErr
        }
        engine.attach(sink)
        // connect(_:to:format:) is current through the macOS 26 SDK and only
        // deprecated (renamed to connectNode) in macOS 27: keep the portable
        // spelling so the helper builds on both toolchains.
        do { try engine.connect(input, to: sink, format: format) } catch { fail("capture") }
        micSink = sink
        do { try engine.start() } catch { fail("capture") }
    }

    func systemAudio() async {
        // Only reached after explicit --start. Never bypass TCC.
        // Permission denial and setup/stream failures get distinct codes so
        // the pane can point at the right remedy.
        guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else { fail("system_permission") }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first else { fail("capture") }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = true
            config.sampleRate = 48000
            config.channelCount = 2
            // ScreenCaptureKit requires a display filter, but only audio output is registered.
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            config.queueDepth = 3
            let created = SCStream(filter: filter, configuration: config, delegate: self)
            try created.addStreamOutput(self, type: .audio, sampleHandlerQueue: audioQueue)
            stream = created
            try await created.startCapture()
        } catch { fail("capture") }
    }

    func stop() {
        // Best-effort synchronous halt: the engine stops pulling microphone
        // audio immediately; the ScreenCaptureKit stream stops asynchronously.
        engine.stop()
        if let active = stream {
            stream = nil
            Task { try? await active.stopCapture() }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) { fail("capture") }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid,
              let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              let basic = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              let format = AVAudioFormat(streamDescription: basic) else { return }
        let frames = CMSampleBufferGetNumSamples(sampleBuffer)
        guard frames > 0, frames <= 192000,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else { return }
        buffer.frameLength = AVAudioFrameCount(frames)
        let result = CMSampleBufferCopyPCMDataIntoAudioBufferList(sampleBuffer, at: 0, frameCount: Int32(frames), into: buffer.mutableAudioBufferList)
        guard result == noErr else { fail("capture") }
        systemConverter.consume(buffer, source: "system")
    }
}

@main
struct Main {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        if args == ["--help"] || args.isEmpty {
            print("macos_capture --start --source microphone|system|both\n--check-only: static capabilities; no permission checks or capture")
            return
        }
        if args == ["--check-only"] {
            let usage = Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String != nil
            print("{\"capture_started\":false,\"microphone_usage_description\":\(usage),\"sources\":[\"microphone\",\"system\",\"both\"],\"rate\":16000}")
            return
        }
        guard args.count == 3, args[0] == "--start", args[1] == "--source",
              ["microphone", "system", "both"].contains(args[2]) else { fail("protocol") }
        signal(SIGPIPE, SIG_IGN)
        // Graceful SIGTERM: drain the async writer so queued frames precede
        // the terminal line, report pending drops directly, and exit cleanly.
        // A second SIGTERM exits immediately — shutdown must stay bounded.
        signal(SIGTERM, SIG_IGN)
        // The async main never pumps the main dispatch queue, so the
        // signal source must run on a background queue to fire at all.
        let termSource: DispatchSourceSignal = DispatchSource.makeSignalSource(signal: SIGTERM, queue: DispatchQueue.global())
        var termCount = 0
        termSource.resume()
        let capture = Capture()
        termSource.setEventHandler {
            termCount += 1
            if termCount > 1 { exit(3) }
            // Stop capture inputs first so no callback can enqueue frames
            // after the writer barrier below; anything already in flight
            // lands during the short settle, then the barrier is final.
            capture.stop()
            Thread.sleep(forTimeInterval: 0.3)
            terminating = true
            writer.sync {}
            // Snapshot after the barrier: every pre-barrier frame was
            // emitted above and every post-barrier callback was counted,
            // so the terminal line accounts for all audio observed to this
            // point. The only uncounted window is the direct write below.
            var payload: [String: Any] = ["type": "status", "state": "stopped"]
            let pending = dropCounter.sync { () -> Int in let n = droppedFrames; droppedFrames = 0; return n }
            if pending > 0 {
                payload["native_dropped_chunks"] = pending
                payload["warning"] = "Native capture dropped \(pending) frame(s) under backpressure; some audio may be missing."
            }
            if let data = try? JSONSerialization.data(withJSONObject: payload) {
                var line = data
                line.append(10)
                try? FileHandle.standardOutput.write(contentsOf: line)
            }
            exit(0)
        }
        let source = args[2]
        if source == "system" || source == "both" { await capture.systemAudio() }
        if source == "microphone" || source == "both" { await capture.microphone() }
        emit(["type": "status", "state": "running"], critical: true)
        // SIGTERM from the worker/gateway ends the process and releases OS capture objects.
        while true { try? await Task.sleep(nanoseconds: 1_000_000_000) }
    }
}
