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

    def run_mode(self, mode):
        result = subprocess.run([self.binary, mode], capture_output=True, timeout=15, check=True)
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
