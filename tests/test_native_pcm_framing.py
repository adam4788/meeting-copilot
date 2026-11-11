"""Production PCM framing with synthetic buffers only; no Capture or devices."""
import base64
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = r'''
@main struct FramingTest {
    static func main() {
        let mode = CommandLine.arguments[1]
        if mode == "invalid" {
            let format = AVAudioFormat(commonFormat:.pcmFormatFloat32, sampleRate:48000, channels:2, interleaved:false)!
            let buffer = AVAudioPCMBuffer(pcmFormat:format, frameCapacity:16)!
            buffer.frameLength = 16
            for channel in 0..<2 { for frame in 0..<16 { buffer.floatChannelData![channel][frame] = 0.25 } }
            let raw = buffer.mutableAudioBufferList
            let list = UnsafeMutableAudioBufferListPointer(raw)
            let original = list[0]
            let kind = CommandLine.arguments[2]
            if kind == "short" { list[0].mDataByteSize = 1 }
            if kind == "missing" { list[0].mData = nil }
            if kind == "channels" { list[0].mNumberChannels = 2 }
            if kind == "nan" { buffer.floatChannelData![0][0] = .nan }
            if kind == "infinity" { buffer.floatChannelData![0][0] = .infinity }
            let rejected = normalizedMono(format:format, frames:16, buffers:UnsafePointer(raw)) == nil
            let target = AVAudioPCMBuffer(pcmFormat:format, frameCapacity:16)!
            target.frameLength = 16
            let copyRejected = !copyPCMInput(UnsafePointer(raw), into:target)
            list[0] = original
            print("{\"rejected\":\(rejected),\"copy_rejected\":\(copyRejected)}")
            return
        }
        if mode == "layout" {
            let channels = Int(CommandLine.arguments[2])!
            let active = Int(CommandLine.arguments[3])!
            let kind: AVAudioCommonFormat = ["f32": .pcmFormatFloat32, "f64": .pcmFormatFloat64,
                                            "i16": .pcmFormatInt16, "i32": .pcmFormatInt32][CommandLine.arguments[4]]!
            let interleaved = CommandLine.arguments[5] == "1"
            let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | UInt32(channels))!
            let format = AVAudioFormat(commonFormat:kind, sampleRate:48000, interleaved:interleaved, channelLayout:layout)
            let converter = Converter()
            for _ in 0..<24 {
                let buffer = AVAudioPCMBuffer(pcmFormat:format, frameCapacity:512)!
                buffer.frameLength = 512
                let list = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
                for channel in 0..<channels {
                    let data = list[interleaved ? 0 : channel].mData!
                    for frame in 0..<512 {
                        let index = interleaved ? frame * channels + channel : frame
                        let value:Float = active == -1 || channel == active ? 0.375 : 0
                        switch kind {
                        case .pcmFormatFloat32: data.assumingMemoryBound(to:Float.self)[index] = value
                        case .pcmFormatFloat64: data.assumingMemoryBound(to:Double.self)[index] = Double(value)
                        case .pcmFormatInt16: data.assumingMemoryBound(to:Int16.self)[index] = Int16(value * 32768)
                        case .pcmFormatInt32: data.assumingMemoryBound(to:Int32.self)[index] = Int32(Double(value) * 2147483648)
                        default: fatalError("unsupported fixture")
                        }
                    }
                }
                converter.consume(buffer, source:"microphone")
                writer.sync {}
            }
            converter.finish()
            writer.sync {}
            return
        }
        if mode == "retire" {
            let converter = Converter()
            let format = AVAudioFormat(commonFormat:.pcmFormatFloat32, sampleRate:16000, channels:1, interleaved:false)!
            let buffer = AVAudioPCMBuffer(pcmFormat:format, frameCapacity:171)!
            buffer.frameLength = 171
            for i in 0..<171 { buffer.floatChannelData![0][i] = 0.25 }
            converter.consume(buffer, source:"microphone")
            converter.finish()
            writer.sync {}
            converter.consume(buffer, source:"microphone")
            converter.finish()
            writer.sync {}
            return
        }
        let mic = PCMFramer(), system = PCMFramer()
        let gate = DispatchSemaphore(value:0)
        let gated = mode == "stall" || mode == "saturate"
        if gated { writer.async { gate.wait() } }
        let rounds = mode == "stall" ? 20 : mode == "saturate" ? 150 : mode == "worker" ? 120 : 50
        for index in 0..<rounds {
            let samples = (0..<171).map { Float(index * 171 + $0) / 10000 }
            samples.withUnsafeBufferPointer { data in
                mic.append(data.baseAddress!, count:171, source:"microphone", timestamp:10 + Double(index * 171) / 16000)
                system.append(data.baseAddress!, count:171, source:"system", timestamp:20 + Double(index * 171) / 16000)
            }
            if !gated { writer.sync {} }
        }
        mic.flush()
        system.flush()
        if gated { gate.signal() }
        writer.sync {}
        let stats = ["samples_per_source":rounds * 171, "pending_drops":dropCounter.sync { droppedFrames }]
        FileHandle.standardError.write(try! JSONSerialization.data(withJSONObject:stats))
    }
}
'''


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS Swift buffers required')
class NativePCMFramingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='mc-pcm-framing-')
        root = Path(cls.tmp.name)
        native = (ROOT / 'native/macos_capture.swift').read_text()
        production = native[native.index('private let writer ='):native.index('private final class Capture:')]
        source = 'import Foundation\nimport AVFoundation\n' + production + DRIVER
        assert 'Capture()' not in source and 'requestAccess' not in source
        swift = root / 'Framing.swift'
        swift.write_text(source)
        cls.binary = root / 'framing'
        build = subprocess.run([shutil.which('swiftc'), '-parse-as-library',
                                '-module-cache-path', str(root / 'module-cache'),
                                str(swift), '-o', str(cls.binary)],
                               capture_output=True, text=True, timeout=120)
        if build.returncode:
            cls.tmp.cleanup()
            raise AssertionError(build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_mode(self, mode, *args):
        result = subprocess.run([self.binary, mode, *map(str, args)], capture_output=True, timeout=15, check=True)
        return [json.loads(line) for line in result.stdout.splitlines()], json.loads(result.stderr or '{}')

    def assert_samples_preserved(self, rows, count):
        for source, start in [('microphone', 10), ('system', 20)]:
            frames = [row for row in rows if row['source'] == source]
            offset = 0
            for row in frames:
                self.assertLessEqual(row['samples'], 4000)
                self.assertAlmostEqual(row['timestamp'], start + offset / 16000)
                samples = struct.unpack('<' + 'f' * row['samples'], base64.b64decode(row['pcm']))
                for index, value in enumerate(samples):
                    self.assertAlmostEqual(value, (offset + index) / 10000, places=6)
                offset += row['samples']
            self.assertEqual(offset, count)

    def test_two_sources_preserve_samples_timestamps_and_residual_tails(self):
        rows, stats = self.run_mode('normal')
        self.assertEqual(len(rows), 6)
        self.assertEqual(stats['pending_drops'], 0)
        self.assert_samples_preserved(rows, 8550)

    def test_short_writer_stall_no_longer_drops_tiny_callbacks(self):
        rows, stats = self.run_mode('stall')
        # Forty callbacks fit in two coalesced messages, not forty writer slots.
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats['pending_drops'], 0)
        self.assert_samples_preserved(rows, 3420)

    def test_sustained_saturation_stays_bounded_and_counts_rejected_tails(self):
        rows, stats = self.run_mode('saturate')
        self.assertEqual(len(rows), 4)
        self.assertEqual(stats['samples_per_source'], 25650)
        self.assertEqual(len(rows) + stats['pending_drops'] + sum(r.get('native_dropped_chunks', 0) for r in rows), 14)

    def test_normalization_rejects_missing_short_mismatched_and_nonfinite_samples(self):
        for kind in ('short', 'missing', 'channels', 'nan', 'infinity'):
            with self.subTest(kind=kind):
                rows, _ = self.run_mode('invalid', kind)
                self.assertEqual(rows, [{'rejected': True, 'copy_rejected': kind in ('short', 'missing', 'channels')}])

    def test_discrete_layout_mixes_every_channel_for_each_pcm_representation(self):
        # Standard mono/stereo labels missed a native silent-success failure:
        # valid discrete layouts converted to zero despite a nonzero channel.
        for channels in (1, 2, 3, 4):
            for active in [-1, *range(channels)]:
                for kind in ('f32', 'f64', 'i16', 'i32'):
                    for interleaved in (0, 1):
                        with self.subTest(channels=channels, active=active, kind=kind, interleaved=interleaved):
                            rows, _ = self.run_mode('layout', channels, active, kind, interleaved)
                            values = []
                            for row in rows:
                                self.assertEqual(row['source'], 'microphone')
                                values.extend(struct.unpack('<' + 'f' * row['samples'], base64.b64decode(row['pcm'])))
                            self.assertGreater(len(values), 4000)
                            self.assertLessEqual(len(values), 4096)
                            expected = .375 if active == -1 else .375 / channels
                            self.assertAlmostEqual(sum(values[-1000:]) / 1000, expected, delta=1e-4)
                            self.assertGreater(max(abs(value) for value in values), expected * .9)

    def test_actual_worker_transcribes_both_synthetic_source_tails(self):
        import audio_worker
        from types import SimpleNamespace
        from unittest.mock import patch
        events, lengths = [], []
        class Model:
            def transcribe(self, samples, **kwargs):
                lengths.append(len(samples))
                return iter([SimpleNamespace(text='Fictional fixture words')]), None
        real_popen = subprocess.Popen
        with patch.object(audio_worker, 'emit', events.append), \
             patch.object(audio_worker.signal, 'signal'), \
             patch.object(audio_worker.subprocess, 'Popen',
                          lambda command, **kw: real_popen([str(self.binary), 'worker'], **kw)):
            audio_worker.run_capture(Model(), 'both', 'en', helper=Path('/synthetic'))
        segments = [row for row in events if row.get('type') == 'segment']
        self.assertEqual([row['speaker'] for row in segments], ['you', 'remote'])
        self.assertEqual(lengths, [20520, 20520])
        self.assertTrue(all(row['text'] == 'Fictional fixture words' for row in segments))
        stopped = next(row for row in events if row.get('state') == 'stopped')
        self.assertEqual(stopped['native_dropped_chunks'], 0)
        self.assertEqual(stopped['worker_dropped_chunks'], 0)
        self.assertEqual(stopped['audio_diagnostics']['microphone']['completed'], 1)
        self.assertEqual(stopped['audio_diagnostics']['system']['completed'], 1)

    def test_retirement_flushes_once_and_rejects_post_terminal_callbacks(self):
        rows, _ = self.run_mode('retire')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['samples'], 171)
        self.assertEqual(struct.unpack('<171f', base64.b64decode(rows[0]['pcm'])), (0.25,) * 171)


if __name__ == '__main__':
    unittest.main()
