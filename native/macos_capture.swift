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
// The single startup status must not compete with real-time PCM. Its separate
// slot keeps the queue bounded while preserving startup and loss reporting.
private let controlSlots = DispatchSemaphore(value: 1)
private let dropCounter = DispatchQueue(label: "meeting.audio.drops")
private var droppedFrames = 0
// Written only by the serial writer; read only after its shutdown barrier.
// Timings contain no audio, timestamps, paths, or credentials.
private var maxQueueNs: UInt64 = 0
private var maxEncodeNs: UInt64 = 0
private var maxWriteNs: UInt64 = 0
private func writerTiming() -> [String: Double] {
    return ["max_queue_ms": Double(maxQueueNs) / 1_000_000,
            "max_encode_ms": Double(maxEncodeNs) / 1_000_000,
            "max_write_ms": Double(maxWriteNs) / 1_000_000]
}

// A slow reader drops audio rather than blocking real-time capture or growing memory.
// Drops are counted and the cumulative total rides on the next successful frame
// so no loss point stays invisible to the transcriber.
private func emit(_ object: [String: Any], critical: Bool = false) {
    let selectedSlots = critical ? controlSlots : slots
    if selectedSlots.wait(timeout: .now()) != .success {
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
    let enqueuedAt = DispatchTime.now().uptimeNanoseconds
    writer.async { [payload, pending, enqueuedAt] in
        let beganAt = DispatchTime.now().uptimeNanoseconds
        maxQueueNs = max(maxQueueNs, beganAt - enqueuedAt)
        defer { selectedSlots.signal() }
        guard let data = try? JSONSerialization.data(withJSONObject: payload), data.count < 100000 else {
            // Never silently discard a loss report: re-queue it for the next frame.
            if pending > 0 { dropCounter.sync { droppedFrames += pending } }
            return
        }
        var line = data
        line.append(10)
        let beforeWrite = DispatchTime.now().uptimeNanoseconds
        maxEncodeNs = max(maxEncodeNs, beforeWrite - beganAt)
        do {
            try FileHandle.standardOutput.write(contentsOf: line)
            maxWriteNs = max(maxWriteNs, DispatchTime.now().uptimeNanoseconds - beforeWrite)
        }
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

// Coalesce tiny hardware callbacks into 250 ms PCM messages. With two sources,
// the old ~10 ms messages could saturate four writer slots during brief stalls.
// Storage stays <= 4000 Float32 samples per source; no unbounded audio queue.
private final class PCMFramer {
    private var pending = Data()
    private var timestamp: Double = 0
    private var source = ""
    private let frameBytes = 4000 * 4

    func append(_ channel: UnsafePointer<Float>, count: Int, source: String, timestamp: Double) {
        var offset = 0
        while offset < count {
            if pending.isEmpty {
                self.timestamp = timestamp + Double(offset) / 16000
                self.source = source
            }
            let samples = min((frameBytes - pending.count) / 4, count - offset)
            pending.append(Data(bytes: channel.advanced(by: offset), count: samples * 4))
            offset += samples
            if pending.count == frameBytes { flush() }
        }
    }

    func flush() {
        guard !pending.isEmpty else { return }
        emit(["type": "pcm", "source": source, "rate": 16000,
              "samples": pending.count / 4, "timestamp": timestamp,
              "pcm": pending.base64EncodedString()])
        pending.removeAll(keepingCapacity: true)
    }
}

// Copy only complete, matching PCM buffers. A partial/missing source list must
// fail rather than silently filling the remaining channels with zeros.
private func copyPCMInput(_ source: UnsafePointer<AudioBufferList>, into target: AVAudioPCMBuffer) -> Bool {
    let sources = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: source))
    let targets = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: target.audioBufferList))
    guard sources.count == targets.count, !sources.isEmpty else { return false }
    for index in 0..<sources.count {
        guard sources[index].mNumberChannels == targets[index].mNumberChannels,
              sources[index].mDataByteSize >= targets[index].mDataByteSize,
              sources[index].mData != nil, targets[index].mData != nil else { return false }
    }
    for index in 0..<sources.count {
        targets[index].mData!.copyMemory(from: sources[index].mData!, byteCount: Int(targets[index].mDataByteSize))
    }
    return true
}

// Do not ask AVAudioConverter to interpret hardware channel labels. A valid
// discrete-in-order multichannel layout can silently convert to all-zero mono.
// Normalize PCM explicitly, then use AVAudioConverter only for resampling.
private func normalizedMono(_ input: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
    withExtendedLifetime(input) {
        normalizedMono(format: input.format, frames: input.frameLength, buffers: input.audioBufferList)
    }
}

private func normalizedMono(format inputFormat: AVAudioFormat, frames frameLength: AVAudioFrameCount,
                            buffers bufferList: UnsafePointer<AudioBufferList>) -> AVAudioPCMBuffer? {
    let frames = Int(frameLength)
    let channels = Int(inputFormat.channelCount)
    guard frames > 0, frames <= 192000, channels > 0, channels <= 32,
          inputFormat.sampleRate.isFinite, inputFormat.sampleRate > 0,
          let format = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                    sampleRate: inputFormat.sampleRate, channels: 1, interleaved: false),
          let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameLength),
          let target = output.floatChannelData?[0] else { return nil }
    let bytes: Int
    switch inputFormat.commonFormat {
    case .pcmFormatFloat32, .pcmFormatInt32: bytes = 4
    case .pcmFormatFloat64: bytes = 8
    case .pcmFormatInt16: bytes = 2
    default: return nil
    }
    let interleaved = inputFormat.isInterleaved
    let description = inputFormat.streamDescription.pointee
    guard description.mFormatID == kAudioFormatLinearPCM,
          description.mFormatFlags & kAudioFormatFlagIsBigEndian == 0,
          description.mFormatFlags & kAudioFormatFlagIsPacked != 0,
          description.mBitsPerChannel == UInt32(bytes * 8),
          description.mBytesPerFrame == UInt32(bytes * (interleaved ? channels : 1)),
          description.mFramesPerPacket == 1 else { return nil }
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: bufferList))
    guard buffers.count == (interleaved ? 1 : channels) else { return nil }
    let stride = interleaved ? channels : 1
    let weight = Float(1) / Float(channels)
    output.frameLength = frameLength
    for frame in 0..<frames { target[frame] = 0 }
    for channel in 0..<channels {
        let buffer = buffers[interleaved ? 0 : channel]
        guard Int(buffer.mNumberChannels) == (interleaved ? channels : 1),
              Int(buffer.mDataByteSize) >= frames * stride * bytes,
              let data = buffer.mData else { return nil }
        let offset = interleaved ? channel : 0
        for frame in 0..<frames {
            let index = frame * stride + offset
            let sample: Float
            switch inputFormat.commonFormat {
            case .pcmFormatFloat32: sample = data.assumingMemoryBound(to: Float.self)[index]
            case .pcmFormatFloat64: sample = Float(data.assumingMemoryBound(to: Double.self)[index])
            case .pcmFormatInt16: sample = Float(data.assumingMemoryBound(to: Int16.self)[index]) / 32768
            case .pcmFormatInt32: sample = Float(Double(data.assumingMemoryBound(to: Int32.self)[index]) / 2147483648)
            default: return nil
            }
            guard sample.isFinite else { return nil }
            target[frame] += sample * weight
        }
    }
    for frame in 0..<frames where !target[frame].isFinite { return nil }
    return output
}

private final class Converter {
    // Serialize conversion and tail retirement. finish() seals this source
    // before flushing, so an in-flight callback cannot emit after the terminal.
    private let lock = NSLock()
    private var stopped = false
    private let framer = PCMFramer()

    func finish() {
        lock.lock()
        defer { lock.unlock() }
        stopped = true
        framer.flush()
    }
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?
    private let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!

    func consume(_ buffer: AVAudioPCMBuffer, source: String) {
        lock.lock()
        defer { lock.unlock() }
        guard !stopped else { return }
        guard buffer.frameLength > 0, buffer.frameLength <= 192000,
              buffer.format.sampleRate > 0 else { return }
        guard let mono = normalizedMono(buffer) else { fail("capture") }
        if inputFormat != mono.format {
            inputFormat = mono.format
            converter = AVAudioConverter(from: mono.format, to: outputFormat)
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
            return mono
        }
        guard result != .error, error == nil, let channel = output.floatChannelData?[0] else { fail("capture") }
        let total = Int(output.frameLength)
        let start = Date().timeIntervalSince1970 - Double(total) / 16000
        framer.append(UnsafePointer(channel), count: total, source: source, timestamp: start)

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
            guard copyPCMInput(audioBufferList, into: buffer) else { fail("capture") }
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

    func flushAudio() {
        micConverter.finish()
        systemConverter.finish()
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
            print("macos_capture --start --source microphone|system|both\n--check-only: static capabilities; no permission checks or capture\n--preflight --source microphone|system|both: nonprompting permission checks\n--request-permissions --source microphone|system|both: explicit permission setup; no capture")
            return
        }
        if args == ["--check-only"] {
            let usage = Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String != nil
            print("{\"capture_started\":false,\"microphone_usage_description\":\(usage),\"sources\":[\"microphone\",\"system\",\"both\"],\"rate\":16000}")
            return
        }
        // This command is reachable only through explicit permission setup.
        // It exits before Capture is constructed and never starts audio streams.
        if args.count == 3, args[0] == "--request-permissions", args[1] == "--source",
           ["microphone", "system", "both"].contains(args[2]) {
            let source = args[2]
            if source == "microphone" || source == "both" {
                let usage = Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String
                if !(usage ?? "").isEmpty,
                   AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
                    _ = await AVCaptureDevice.requestAccess(for: .audio)
                }
            }
            if source == "system" || source == "both" {
                if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
            }
            let result: [String: Any] = ["version": 1, "source": source,
                                        "capture_started": false, "permission_request_completed": true]
            if let data = try? JSONSerialization.data(withJSONObject: result),
               let line = String(data: data, encoding: .utf8) { print(line) }
            return
        }
        // Nonprompting readiness only: never instantiate Capture, request access,
        // enumerate shareable content, or start an AVAudioEngine/SCStream here.
        if args.count == 3, args[0] == "--preflight", args[1] == "--source",
           ["microphone", "system", "both"].contains(args[2]) {
            let source = args[2]
            var result: [String: Any] = ["version": 1, "source": source, "capture_started": false]
            if source == "microphone" || source == "both" {
                let usage = Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String
                result["microphone_usage_description"] = !(usage ?? "").isEmpty
                // Device discovery only, no AVCaptureDeviceInput or session.
                result["microphone_available"] = AVCaptureDevice.default(for: .audio) != nil
                switch AVCaptureDevice.authorizationStatus(for: .audio) {
                case .authorized: result["microphone"] = "authorized"
                case .denied: result["microphone"] = "denied"
                case .restricted: result["microphone"] = "restricted"
                case .notDetermined: result["microphone"] = "not_determined"
                @unknown default: result["microphone"] = "unknown"
                }
            }
            if source == "system" || source == "both" {
                result["system"] = CGPreflightScreenCaptureAccess() ? "authorized" : "not_authorized"
                // ScreenCaptureKit's display filter requires an active display.
                // CoreGraphics metadata does not request screen permission.
                var displayCount: UInt32 = 0
                if CGGetActiveDisplayList(0, nil, &displayCount) == .success {
                    result["display_available"] = displayCount > 0
                } // Absent on error: callers must treat this as unknown.
            }
            if let data = try? JSONSerialization.data(withJSONObject: result),
               let line = String(data: data, encoding: .utf8) { print(line) }
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
            // after source retirement below; anything already in flight
            // lands during the short settle, then finish() seals each source.
            capture.stop()
            Thread.sleep(forTimeInterval: 0.3)
            capture.flushAudio()
            writer.sync {}
            // Snapshot after the barrier: every pre-barrier frame was
            // emitted above (including residual PCM) or counted as dropped.
            // Retired converters cannot add post-terminal audio.
            var payload: [String: Any] = ["type": "status", "state": "stopped"]
            payload["native_writer_timing_ms"] = writer.sync { writerTiming() }
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
