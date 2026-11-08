"""Device-free regressions for the production native writer and its consumer.

Only Foundation and synthetic zero PCM are compiled; no audio permissions or
capture frameworks are used. A blocked writer makes queue saturation deterministic.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.verify_native_backpressure import writer_source

ROOT = Path(__file__).resolve().parent.parent
DRIVER = r'''
@main struct SaturatedWriterTest {
    static func main() {
        let gate = DispatchSemaphore(value: 0)
        writer.async { gate.wait() }
        let pcm = Data(repeating: 0, count: 171 * 4).base64EncodedString()
        for i in 0..<7 {
            emit(["type":"pcm", "source":"microphone", "rate":16000,
                  "samples":171, "timestamp":1.0, "sequence":i, "pcm":pcm])
        }
        emit(["type":"status", "state":"running"], critical:true)
        gate.signal()
        writer.sync {}
        let pending = dropCounter.sync { droppedFrames }
        let stats = ["attempted":7, "pending":pending]
        FileHandle.standardError.write(try! JSONSerialization.data(withJSONObject:stats))
    }
}
'''


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'),
                     'Foundation Swift toolchain required')
class NativeWriterStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='mc-native-status-')
        path = Path(cls.tmp.name)
        source = writer_source(ROOT).split('@main struct SyntheticWriter {', 1)[0] + DRIVER
        swift = path / 'Writer.swift'
        swift.write_text(source, encoding='utf-8')
        cls.binary = path / 'writer'
        compiler = shutil.which('swiftc')
        if compiler is None:
            raise unittest.SkipTest('Foundation Swift toolchain required')
        build = subprocess.run([compiler, '-parse-as-library',
                                '-module-cache-path', str(path / 'module-cache'),
                                str(swift), '-o', str(cls.binary)],
                               capture_output=True, text=True, timeout=120)
        if build.returncode:
            cls.tmp.cleanup()
            raise AssertionError(f'Swift writer failed to compile: {build.stderr}')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_full_audio_queue_still_delivers_running_status_and_loss_count(self):
        result = subprocess.run([self.binary], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, 'full PCM queue must not kill capture before running status')
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['sequence'] for r in rows if r['type'] == 'pcm'], [0, 1, 2, 3])
        statuses = [r for r in rows if r['type'] == 'status']
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]['state'], 'running')
        self.assertEqual(statuses[0]['native_dropped_chunks'], 3)
        stats = json.loads(result.stderr)
        self.assertEqual(stats, {'attempted': 7, 'pending': 0})
        self.assertEqual(sum(r.get('native_dropped_chunks', 0) for r in rows) + 4, 7)

    def test_actual_worker_forwards_control_frame_loss_without_double_counting(self):
        import audio_worker
        events = []
        real_popen = subprocess.Popen
        with patch.object(audio_worker, 'emit', events.append), \
             patch.object(audio_worker.signal, 'signal'), \
             patch.object(audio_worker.subprocess, 'Popen',
                          lambda command, **kw: real_popen([str(self.binary)], **kw)):
            audio_worker.run_capture(object(), 'microphone', 'en', helper=Path('/synthetic'))
        running = [e for e in events if e.get('state') == 'running']
        stopped = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(running[-1]['native_dropped_chunks'], 3)
        self.assertEqual(stopped[-1]['native_dropped_chunks'], 3)
        self.assertEqual(stopped[-1]['sub_second_dropped_chunks'], 1)
        self.assertEqual([e.get('code') for e in events if e.get('type') == 'error'], ['capture'])


if __name__ == '__main__':
    unittest.main()
