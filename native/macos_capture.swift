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

// A slow reader drops audio rather than blocking real-time capture or growing memory.
private func emit(_ object: [String: Any], critical: Bool = false) {
    if slots.wait(timeout: .now()) != .success {
        if critical { exit(2) }
        return
    }
    writer.async {
        defer { slots.signal() }
        guard var data = try? JSONSerialization.data(withJSONObject: object), data.count < 100000 else { return }
        data.append(10)
        do { try FileHandle.standardOutput.write(contentsOf: data) }
        catch { exit(0) }
    }
}

private func fail(_ code: String) -> Never {
    emit(["type": "error", "code": code], critical: true)
    writer.sync {}
    exit(1)
}

private final class Converter {
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?
    private let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!

    func consume(_ buffer: AVAudioPCMBuffer, source: String) {
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

    func microphone() async {
        guard Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String != nil else { fail("microphone_usage") }
        let authorized = await AVCaptureDevice.requestAccess(for: .audio)
        guard authorized else { fail("microphone_permission") }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { fail("capture") }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            self?.micConverter.consume(buffer, source: "microphone")
        }
        do { try engine.start() } catch { fail("capture") }
    }

    func systemAudio() async {
        // Only reached after explicit --start. Never bypass TCC.
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
        } catch { fail("system_permission") }
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
        let capture = Capture()
        let source = args[2]
        if source == "system" || source == "both" { await capture.systemAudio() }
        if source == "microphone" || source == "both" { await capture.microphone() }
        emit(["type": "status", "state": "running"], critical: true)
        // SIGTERM from the worker/gateway ends the process and releases OS capture objects.
        while true { try? await Task.sleep(nanoseconds: 1_000_000_000) }
    }
}
