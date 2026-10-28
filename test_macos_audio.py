"""No device access, network, or permission prompts in unit tests."""
import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def _scratch_dir():
    scratch = Path.home() / '.hermes/cache/scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


class GatewayTests(unittest.TestCase):
    def test_idle_capabilities_and_validation_never_spawn(self):
        module = importlib.import_module('macos_audio')
        with patch('subprocess.Popen', side_effect=AssertionError('must not spawn')):
            capture = module.MacOSAudioCapture()
            self.assertEqual(capture.status()['state'], 'idle')
            self.assertEqual(capture.devices()['sources'], ['microphone', 'system', 'both'])
            self.assertFalse(capture.devices()['permissions_checked'])
            for kw in ({'model_size': '/tmp/model'}, {'model_size': 'large'},
                       {'source': 'url'}, {'language': 'fr'}, {'language': '--help'}):
                with self.subTest(kw=kw), self.assertRaises(ValueError):
                    module.MacOSAudioCapture(**kw)
            capture.stop()
            self.assertEqual(capture.status()['state'], 'stopped')

    def test_explicit_start_streams_segments_and_stop_kills_process_group(self):
        import json
        import subprocess
        import sys
        import threading
        from macos_audio import MacOSAudioCapture
        real_popen = subprocess.Popen
        calls = []
        script = ("import json,time; print(json.dumps({'type':'status','state':'running'}),flush=True); "
                  "print(json.dumps({'type':'segment','speaker':'you','text':'Hello','timestamp':1.0}),flush=True); time.sleep(60)")
        def factory(command, **kwargs):
            calls.append(command)
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        capture = MacOSAudioCapture()
        segments, seen = [], threading.Event()
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), patch('subprocess.Popen', factory):
            capture.start(lambda s: (segments.append(s), seen.set()))
            self.assertTrue(seen.wait(3), capture.status())
            with self.assertRaises(RuntimeError): capture.start(print)
            process = capture._process
            capture.stop(timeout=1)
        self.assertIsNotNone(process.poll())
        self.assertEqual(segments[0]['text'], 'Hello')
        self.assertEqual(capture.status()['state'], 'stopped')
        self.assertFalse(capture.status()['running'])
        self.assertIn('--model', calls[0])
        self.assertNotIn('Meetily', str(calls))

    def test_stop_during_slow_spawn_is_bounded_and_suppresses_late_callbacks(self):
        import subprocess
        import sys
        import threading
        import time
        from macos_audio import MacOSAudioCapture
        entered, release = threading.Event(), threading.Event()
        real_popen = subprocess.Popen
        processes, segments = [], []
        def factory(command, **kw):
            entered.set()
            release.wait(3)
            p = real_popen([sys.executable, '-c', 'print(\'{"type":"status","state":"running"}\',flush=True); import time; time.sleep(60)'], **kw)
            processes.append(p)
            return p
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), patch('subprocess.Popen', factory):
            capture = MacOSAudioCapture()
            capture.start(segments.append, segments.append)
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            capture.stop(timeout=.01)
            self.assertLess(time.monotonic() - before, .5)
            with self.assertRaises(RuntimeError): capture.start(print)
            release.set()
            capture._thread.join(2)
        self.assertEqual(capture.status()['state'], 'stopped')
        self.assertEqual(segments, [])
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(capture.status()['running'])

    def test_oversized_worker_frame_fails_closed(self):
        import subprocess
        import sys
        import threading
        from macos_audio import MacOSAudioCapture
        real_popen = subprocess.Popen
        observed = threading.Event()
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), patch('subprocess.Popen', lambda c, **kw: real_popen([sys.executable, '-c', "print('x'*70000,flush=True)"], **kw)):
            capture = MacOSAudioCapture()
            capture.start(lambda s: self.fail('invalid segment'), lambda s: observed.set() if s['state'] == 'error' else None)
            self.assertTrue(observed.wait(2))
            capture.stop()
        self.assertIn('oversized', capture.status()['error'])

    def test_runtime_discovery_is_explicit_and_never_falls_back_to_host_python(self):
        from macos_audio import worker_python
        import tempfile
        with tempfile.TemporaryDirectory(dir=str(_scratch_dir())) as temp:
            root = Path(temp)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError): worker_python(root)
                project = root / '.venv-audio/bin/python'
                project.parent.mkdir(parents=True)
                project.touch(); project.chmod(0o755)
                self.assertEqual(worker_python(root), str(project))
                stable = root / 'audio-runtime/bin/python'
                stable.parent.mkdir(parents=True)
                stable.touch(); stable.chmod(0o755)
                self.assertEqual(worker_python(root), str(stable))
            with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': '/missing/interpreter'}):
                with self.assertRaises(RuntimeError): worker_python(root)

    def test_worker_error_is_sanitized_and_callbacks_are_contained(self):
        import subprocess
        import sys
        import threading
        from macos_audio import MacOSAudioCapture
        real_popen = subprocess.Popen
        script = "print('{\"type\":\"error\",\"code\":\"microphone_permission\",\"message\":\"SECRET /Users/private\"}',flush=True)"
        failed = threading.Event()
        def status(value):
            if value['state'] == 'error':
                failed.set()
                raise ValueError('SECRET')
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), patch('subprocess.Popen', lambda c, **kw: real_popen([sys.executable, '-c', script], **kw)):
            capture = MacOSAudioCapture()
            capture.start(print, status)
            self.assertTrue(failed.wait(3))
            capture.stop()
        self.assertIn('Microphone permission', capture.status()['error'])
        self.assertNotIn('SECRET', str(capture.status()))
        self.assertIsNotNone(capture.status()['callback_error'])


class WorkerTests(unittest.TestCase):
    def test_pcm_protocol_validates_bounds_and_decodes_mono_float32(self):
        import base64
        import json
        import struct
        from audio_worker import decode_frame, MAX_FRAME
        raw = struct.pack('<4f', 0.0, 0.25, -0.5, 1.0)
        frame = {'type': 'pcm', 'source': 'microphone', 'rate': 16000,
                 'timestamp': 10.0, 'samples': 4, 'pcm': base64.b64encode(raw).decode()}
        speaker, samples, stamp = decode_frame(json.dumps(frame).encode() + b'\n')
        self.assertEqual((speaker, stamp), ('you', 10.0))
        self.assertEqual(samples.tolist(), [0, .25, -.5, 1])
        for key, value in [('rate', 48000), ('source', 'url'), ('samples', 999999),
                           ('timestamp', float('nan')), ('pcm', '!')]:
            broken = dict(frame, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                decode_frame(json.dumps(broken).encode() + b'\n')
        with self.assertRaises(ValueError): decode_frame(b'x' * (MAX_FRAME + 1))

    def test_model_is_allowlisted_cpu_int8_and_transcripts_use_callback_schema(self):
        from audio_worker import load_model, transcribe
        from types import SimpleNamespace as NS
        calls = []
        class Model:
            def transcribe(self, samples, **kw):
                calls.append(kw)
                return iter([NS(text=' Test speech. ')]), None
        def factory(*args, **kw):
            calls.append((args, kw))
            return Model()
        model = load_model('tiny.en', factory)
        result = transcribe(model, [0.0] * 16000, 'you', 1.0, 'en')
        self.assertEqual(calls[0], (('tiny.en',), {'device': 'cpu', 'compute_type': 'int8', 'cpu_threads': 4}))
        self.assertEqual(result, {'type': 'segment', 'speaker': 'you', 'text': 'Test speech.', 'timestamp': 1.0})
        with self.assertRaises(ValueError): load_model('/arbitrary/path', factory)

    def test_live_worker_consumes_framed_helper_without_hardware(self):
        import base64
        import json
        import subprocess
        import sys
        from types import SimpleNamespace as NS
        from audio_worker import run_capture
        frame = {'type': 'pcm', 'source': 'microphone', 'rate': 16000,
                 'samples': 16000, 'timestamp': 1.0,
                 'pcm': base64.b64encode(bytes(64000)).decode()}
        script = 'import sys; sys.stdout.write(' + repr((json.dumps(frame) + '\n') * 4) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        class Model:
            def transcribe(self, samples, **kw):
                self.count = len(samples)
                return iter([NS(text='Independent speech')]), None
        model = Model()
        # The framed payload (~340KB) exceeds Linux's single-argument limit,
        # so run it from a temp file instead of `python -c`.
        import tempfile
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'frames.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                run_capture(model, 'microphone', 'en', helper=Path('/fixture'))
        segments = [e for e in outputs if e['type'] == 'segment']
        self.assertEqual(segments[0]['text'], 'Independent speech')
        self.assertEqual(model.count, 64000)

    def test_concurrent_worker_events_remain_valid_ndjson(self):
        import io
        import json
        import threading
        import time
        from audio_worker import emit
        class YieldingOutput(io.StringIO):
            def write(self, value):
                result = super().write(value)
                time.sleep(.001)
                return result
        stream = YieldingOutput()
        with patch('sys.stdout', stream):
            threads = [threading.Thread(target=lambda: [emit({'type': 'status', 'state': 'running'}) for _ in range(5)]) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        rows = stream.getvalue().splitlines()
        self.assertEqual(len(rows), 10)
        for row in rows: self.assertEqual(json.loads(row)['type'], 'status')

    def test_cli_help_and_invalid_model_never_import_heavy_runtime(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parent
        result = subprocess.run([sys.executable, str(root / 'audio_worker.py'), '--help'], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'--file', result.stdout)
        result = subprocess.run([sys.executable, str(root / 'audio_worker.py'), '--model', '/untrusted'], capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/swiftc').exists(),
                         'macOS Swift helper compile requires darwin and swiftc')
    def test_native_helper_compiles_and_check_only_never_records(self):
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parent
        import plistlib
        import json
        with tempfile.TemporaryDirectory(dir=str(_scratch_dir())) as temp:
            binary = Path(temp) / 'macos_capture'
            info = Path(temp) / 'Info.plist'
            info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'ai.nous.meeting-copilot.audio',
                                            'NSMicrophoneUsageDescription': 'Transcribe microphone audio only when you start meeting capture.'}))
            subprocess.run(['/usr/bin/swiftc', '-parse-as-library', str(root / 'native/macos_capture.swift'),
                            '-Xlinker', '-sectcreate', '-Xlinker', '__TEXT', '-Xlinker', '__info_plist', '-Xlinker', str(info),
                            '-o', str(binary)], check=True, capture_output=True, timeout=120)
            result = subprocess.run([str(binary), '--check-only'], check=True, capture_output=True, timeout=5)
            self.assertIn(b'"capture_started":false', result.stdout)
            self.assertTrue(json.loads(result.stdout)['microphone_usage_description'])
            result = subprocess.run([str(binary), '--help'], check=True, capture_output=True, timeout=5)
            self.assertIn(b'--source', result.stdout)


if __name__ == '__main__':
    unittest.main()
