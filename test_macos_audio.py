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

    def test_meta_provider_validation_command_and_availability(self):
        module = importlib.import_module('macos_audio')
        for kw in ({'provider': 'cloud'}, {'provider': 'meta', 'asr_mode': 'STREAM'},
                   {'provider': 'meta', 'asr_mode': 'endpointing'}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                module.MacOSAudioCapture(**kw)
        import subprocess
        import sys
        import threading
        real_popen = subprocess.Popen
        calls = []
        script = "import time; time.sleep(60)"
        def factory(command, **kwargs):
            calls.append(command)
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        capture = module.MacOSAudioCapture(provider='meta', asr_mode='DIARIZATION')
        self.assertEqual(capture.status()['provider'], 'meta')
        self.assertEqual(capture.status()['asr_mode'], 'DIARIZATION')
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), patch('subprocess.Popen', factory):
            capture.start(lambda s: None)
            import time as _time
            deadline = _time.time() + 3
            while not calls and _time.time() < deadline:
                _time.sleep(0.02)
            capture.stop(timeout=1)
        self.assertTrue(calls, 'worker was never spawned')
        command = calls[0]
        self.assertEqual(command[command.index('--provider') + 1], 'meta')
        self.assertEqual(command[command.index('--asr-mode') + 1], 'DIARIZATION')
        with patch.dict(os.environ, {'MODEL_API_KEY': 'k'}, clear=False):
            self.assertTrue(module.MacOSAudioCapture().devices()['meta_asr_available'])
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(module.MacOSAudioCapture().devices()['meta_asr_available'])

    def test_diarized_meta_segments_survive_the_adapter(self):
        # Producer: the real Meta event mapper turns a diarized turn into
        # a worker segment. Consumer: the real MacOSAudioCapture._run must
        # accept it (it used to raise protocol and drop the segment).
        import meta_asr
        mapper = meta_asr.EventMapper('you')
        mapper.map({'type': 'speechStart', 'turnId': 1}, 1.0)
        mapper.map({'type': 'speaker', 'label': 'B'}, 1.1)
        produced = mapper.map({'type': 'speechComplete', 'turnId': 1,
                               'transcript': 'Diarized words.'}, 1.2)
        self.assertEqual(produced['speaker'], 'you \u00b7 Speaker B')
        module = importlib.import_module('macos_audio')
        self.assertTrue(module._valid_speaker('you'))
        self.assertTrue(module._valid_speaker('remote'))
        self.assertTrue(module._valid_speaker('you \u00b7 Speaker B'))
        self.assertTrue(module._valid_speaker('Speaker B'))
        self.assertFalse(module._valid_speaker('you; DROP TABLE'))
        self.assertFalse(module._valid_speaker('Speaker ' + 'X' * 40))
        self.assertFalse(module._valid_speaker(''))
        import json
        import subprocess
        import sys
        import threading
        real_popen = subprocess.Popen
        lines = [{'type': 'status', 'state': 'running'}, produced]
        script = "import json,time; " + "; ".join(
            f"print(json.dumps({line!r}), flush=True)" for line in lines) + "; time.sleep(60)"
        def factory(command, **kwargs):
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        capture = module.MacOSAudioCapture(provider='meta', asr_mode='DIARIZATION')
        segments, seen = [], threading.Event()
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), patch('subprocess.Popen', factory):
            capture.start(lambda s: (segments.append(s), seen.set()))
            self.assertTrue(seen.wait(3), capture.status())
            capture.stop(timeout=1)
        self.assertEqual(segments, [{'speaker': 'you \u00b7 Speaker B',
                                     'text': 'Diarized words.', 'timestamp': 1.2}])
        self.assertNotEqual(capture.status()['state'], 'error')

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
        # Teardown delivers exactly one terminal stopped summary (with the
        # last known loss evidence); the slow spawn's late callbacks stay suppressed.
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].get('state'), 'stopped')
        self.assertNotIn('text', segments[0])
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

    def test_worker_env_never_inherits_launcher_pythonpath(self):
        from macos_audio import worker_env
        with patch.dict(os.environ, {'PYTHONPATH': '/host/venv-3.14/site-packages', 'PYTHONHOME': '/host'}):
            env = worker_env()
            self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('PYTHONHOME', env)
            self.assertEqual(env.get('PATH'), os.environ.get('PATH'))

    def test_worker_spawn_uses_hermetic_env(self):
        import subprocess
        import sys
        import threading
        from macos_audio import MacOSAudioCapture
        real_popen = subprocess.Popen
        seen = {}
        script = "print('{\"type\":\"status\",\"state\":\"running\"}',flush=True);import time;time.sleep(30)"
        def factory(cmd, **kw):
            seen.update(kw.get('env') or {})
            return real_popen([sys.executable, '-c', script], **kw)
        started = threading.Event()
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable,
                                      'PYTHONPATH': '/host/venv-3.14/site-packages',
                                      'PYTHONHOME': '/host'}), \
                patch('subprocess.Popen', factory):
            capture = MacOSAudioCapture()
            capture.start(print, lambda value: started.set() if value['state'] == 'running' else None)
            self.assertTrue(started.wait(5))
            capture.stop()
        self.assertNotIn('PYTHONPATH', seen)
        self.assertNotIn('PYTHONHOME', seen)

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
        speaker, samples, stamp, native_drops = decode_frame(json.dumps(frame).encode() + b'\n')
        self.assertEqual((speaker, stamp), ('you', 10.0))
        self.assertEqual(samples.tolist(), [0, .25, -.5, 1])
        self.assertEqual(native_drops, 0)
        reported = dict(frame, dropped_chunks=3)
        self.assertEqual(decode_frame(json.dumps(reported).encode() + b'\n')[3], 3)
        native = dict(frame, native_dropped_chunks=7)
        self.assertEqual(decode_frame(json.dumps(native).encode() + b'\n')[3], 7)
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
            # AVAudioSinkNode requires the macOS 26 SDK (workflow pins macos-26):
            # never swallow the compiler output, or the next toolchain skew
            # fails with only an exit status again.
            proc = subprocess.run(['/usr/bin/swiftc', '-parse-as-library', str(root / 'native/macos_capture.swift'),
                            '-Xlinker', '-sectcreate', '-Xlinker', '__TEXT', '-Xlinker', '__info_plist', '-Xlinker', str(info),
                            '-o', str(binary)], check=False, capture_output=True, timeout=120, text=True)
            self.assertEqual(proc.returncode, 0,
                             f'swiftc failed (exit {proc.returncode}):\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')
            result = subprocess.run([str(binary), '--check-only'], check=True, capture_output=True, timeout=5)
            self.assertIn(b'"capture_started":false', result.stdout)
            self.assertTrue(json.loads(result.stdout)['microphone_usage_description'])
            result = subprocess.run([str(binary), '--help'], check=True, capture_output=True, timeout=5)
            self.assertIn(b'--source', result.stdout)


if __name__ == '__main__':
    unittest.main()


class StatusLossAccumulationTests(unittest.TestCase):
    def test_status_event_drops_accumulate_monotonically(self):
        import json
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        from audio_worker import run_capture
        lines = (json.dumps({'type': 'status', 'state': 'running', 'dropped_chunks': 2}) + '\n'
                 + json.dumps({'type': 'status', 'state': 'running', 'dropped_chunks': 3}) + '\n')
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        class Model:
            pass
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'statuses.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                run_capture(Model(), 'microphone', 'en', helper=Path('/fixture'))
        running = [e for e in outputs if e.get('type') == 'status' and e.get('state') == 'running']
        self.assertEqual([e['dropped_chunks'] for e in running], [2, 5])
        self.assertIn('warning', running[-1])


class StopFinalSummaryTests(unittest.TestCase):
    def test_stop_delivers_terminal_state_with_last_known_loss(self):
        from macos_audio import MacOSAudioCapture
        capture = MacOSAudioCapture.__new__(MacOSAudioCapture)
        import threading
        capture._lock = threading.RLock()
        capture._cancel = threading.Event()
        capture._stopping = threading.Event()
        capture._state = 'running'
        capture._thread = None
        capture._process = None
        capture._error = None
        capture._callback_error = None
        capture._source = 'microphone'
        capture._model_size = 'tiny.en'
        capture.source = 'microphone'
        capture.model_size = 'tiny.en'
        capture._on_status = lambda value: None
        capture._last_loss = {'dropped_chunks': 4, 'warning': 'drops happened'}
        seen = []
        capture._callback = lambda callback, value: seen.append(value)
        capture.stop()
        self.assertTrue(seen)
        self.assertEqual(seen[-1]['state'], 'stopped')
        self.assertEqual(seen[-1]['dropped_chunks'], 4)


class ErrorFrameLossTests(unittest.TestCase):
    def test_error_frame_drops_survive_in_final_summary(self):
        import json
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        from audio_worker import run_capture
        lines = (json.dumps({'type': 'error', 'code': 'device', 'dropped_chunks': 3}) + '\n')
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        class Model:
            pass
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'err.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                run_capture(Model(), 'microphone', 'en', helper=Path('/fixture'))
        stopped = [e for e in outputs if e.get('state') == 'stopped']
        self.assertTrue(stopped)
        self.assertEqual(stopped[0]['dropped_chunks'], 3)


class PcmLossSingleCountTests(unittest.TestCase):
    def test_pcm_frame_drops_counted_exactly_once(self):
        import base64
        import json
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        from audio_worker import run_capture
        pcm = {'type': 'pcm', 'source': 'microphone', 'rate': 16000,
               'samples': 16000, 'timestamp': 1.0,
               'pcm': base64.b64encode(bytes(64000)).decode(), 'native_dropped_chunks': 3}
        lines = (json.dumps({'type': 'status', 'state': 'running', 'dropped_chunks': 2}) + '\n'
                 + json.dumps(pcm) + '\n')
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        class Model:
            def transcribe(self, samples, **kw):
                from types import SimpleNamespace as NS
                return iter([NS(text='')]), None
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'mix.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                run_capture(Model(), 'microphone', 'en', helper=Path('/fixture'))
        running = [e for e in outputs if e.get('type') == 'status' and e.get('state') == 'running']
        self.assertEqual([e['dropped_chunks'] for e in running], [2, 5])


class GracefulShutdownTests(unittest.TestCase):
    def test_sigterm_handler_flushes_instead_of_dying(self):
        import json
        import signal
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        from audio_worker import run_capture
        lines = json.dumps({'type': 'status', 'state': 'running'}) + '\n'
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        prev = signal.getsignal(signal.SIGTERM)
        try:
            with tempfile.TemporaryDirectory() as helper_dir:
                helper_script = Path(helper_dir) / 'one.py'
                helper_script.write_text(script)
                with patch('audio_worker.emit'), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                    run_capture(object(), 'microphone', 'en', helper=Path('/fixture'))
            installed = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(installed) and installed not in (signal.SIG_DFL, signal.SIG_IGN, None))
        finally:
            signal.signal(signal.SIGTERM, prev)


class InProgressTranscriptionTests(unittest.TestCase):
    def test_finished_transcription_emits_despite_stop(self):
        import base64
        import json
        import subprocess
        import sys
        import tempfile
        import threading
        import time
        from unittest.mock import patch
        from audio_worker import run_capture
        pcm = {'type': 'pcm', 'source': 'microphone', 'rate': 16000,
               'samples': 16000, 'timestamp': 1.0,
               'pcm': base64.b64encode(bytes(64000)).decode()}
        script = 'import sys; sys.stdout.write(' + repr((json.dumps(pcm) + '\n') * 4) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        entered, release = threading.Event(), threading.Event()
        class Model:
            def transcribe(self, samples, **kw):
                entered.set()
                self.count = len(samples)
                release.wait(5)
                from types import SimpleNamespace as NS
                return iter([NS(text='Trailing speech')]), None
        model = Model()
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'one.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                done = threading.Event()
                worker_stop = {'event': None}
                import audio_worker
                real_run = audio_worker.run_capture
                def run_with_stop(*a, **k):
                    import threading as _t
                    k['stop'] = worker_stop['event'] = _t.Event()
                    try:
                        real_run(*a, **k)
                    finally:
                        done.set()
                thread = threading.Thread(target=run_with_stop, args=(model, 'microphone'),
                                          kwargs={'helper': Path('/fixture'), 'language': 'en'})
                thread.start()
                self.assertTrue(entered.wait(5))
                worker_stop['event'].set()
                release.set()
                thread.join(10)
        segments = [e for e in outputs if e.get('text')]
        self.assertTrue(any('Trailing speech' in (e.get('text') or '') for e in segments))


class ForcedShutdownTests(unittest.TestCase):
    def test_unresponsive_worker_is_marked_forced(self):
        import threading
        from macos_audio import MacOSAudioCapture
        capture = MacOSAudioCapture.__new__(MacOSAudioCapture)
        capture._lock = threading.RLock()
        capture._cancel = threading.Event()
        capture._stopping = threading.Event()
        capture._state = 'running'
        capture._thread = None
        capture._process = None
        capture._error = None
        capture._callback_error = None
        capture.source = 'microphone'
        capture.model_size = 'tiny.en'
        capture._on_status = lambda value: None
        capture._last_loss = {}
        class Stubborn:
            def terminate(self):
                pass
            def poll(self):
                return None
            def wait(self, timeout=None):
                import time as _t
                _t.sleep(min(timeout or 0, 0.05))
                raise TimeoutError('still alive')
        capture._process = Stubborn()
        seen = []
        capture._callback = lambda callback, value: seen.append(value)
        import macos_audio as _mod
        real_terminate = _mod.terminate_group
        _mod.terminate_group = lambda *a, **k: None
        try:
            capture.stop(timeout=0.2)
        finally:
            _mod.terminate_group = real_terminate
        self.assertTrue(seen)
        self.assertEqual(seen[-1]['state'], 'stopped')
        self.assertIn('Forced shutdown', seen[-1].get('warning', ''))


class SubSecondOnlyLossTests(unittest.TestCase):
    def test_sub_second_tail_alone_produces_loss_summary(self):
        import base64
        import json
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        from audio_worker import run_capture
        # 8000 samples < 16000: never forms a chunk, only a sub-second tail.
        pcm = {'type': 'pcm', 'source': 'microphone', 'rate': 16000,
               'samples': 8000, 'timestamp': 1.0,
               'pcm': base64.b64encode(bytes(32000)).decode()}
        lines = json.dumps(pcm) + '\n'
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        class Model:
            def transcribe(self, samples, **kw):
                from types import SimpleNamespace as NS
                return iter([NS(text='')]), None
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'tail.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                run_capture(Model(), 'microphone', 'en', helper=Path('/fixture'))
        stopped = [e for e in outputs if e.get('state') == 'stopped']
        self.assertTrue(stopped, 'a discarded sub-second tail must surface a summary')
        self.assertEqual(stopped[0].get('sub_second_dropped_chunks'), 1)


class LossContractTests(unittest.TestCase):
    def test_status_events_carry_separate_loss_units(self):
        import json
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        from audio_worker import run_capture
        lines = json.dumps({'type': 'status', 'state': 'running', 'native_dropped_chunks': 2}) + '\n'
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        class Model:
            pass
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 's.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                run_capture(Model(), 'microphone', 'en', helper=Path('/fixture'))
        running = [e for e in outputs if e.get('type') == 'status' and e.get('state') == 'running']
        self.assertTrue(running)
        last = running[-1]
        self.assertEqual(last['native_dropped_chunks'], 2)
        self.assertIn('worker_dropped_chunks', last)


class TailOrderingTests(unittest.TestCase):
    def test_tail_transcribed_after_queued_chunks(self):
        import base64
        import json
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch
        from audio_worker import run_capture
        # 4 full frames (64000 samples: one live chunk) then a 16000
        # trailing chunk with a newer stamp, flushed as the ordered tail.
        frames = []
        for i in range(5):
            frames.append({'type': 'pcm', 'source': 'microphone', 'rate': 16000,
                           'samples': 16000, 'timestamp': 1.0 + i,
                           'pcm': base64.b64encode(bytes(64000)).decode()})
        lines = ''.join(json.dumps(f) + '\n' for f in frames)
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        from types import SimpleNamespace as NS
        def fake_transcribe(model, samples, speaker, stamp, language):
            return {'type': 'segment', 'speaker': speaker,
                    'text': f't@{stamp}', 'timestamp': stamp}
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'o.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)), patch('audio_worker.transcribe', fake_transcribe):
                run_capture(NS(), 'microphone', 'en', helper=Path('/fixture'))
        segments = [e for e in outputs if e.get('type') == 'segment']
        self.assertEqual(len(segments), 2)
        # The queued full chunk (older stamp) comes before the reader tail.
        self.assertLess(float(segments[0]['text'][2:]), float(segments[1]['text'][2:]))
        self.assertTrue(segments[1].get('partial_tail'))


class TeardownErrorVisibilityTests(unittest.TestCase):
    def _capture(self):
        import threading
        from macos_audio import MacOSAudioCapture
        capture = MacOSAudioCapture.__new__(MacOSAudioCapture)
        capture._lock = threading.RLock()
        capture._cancel = threading.Event()
        capture._stopping = threading.Event()
        capture._state = 'running'
        capture._error = None
        capture._callback_error = None
        capture._source = 'microphone'
        capture._model_size = 'tiny.en'
        capture.source = 'microphone'
        capture.model_size = 'tiny.en'
        capture._thread = None
        capture._process = None
        capture._on_status = lambda value: None
        return capture

    def test_expected_worker_eof_stays_suppressed_during_stop(self):
        capture = self._capture()
        seen = []
        capture._callback = lambda callback, value: seen.append(value)
        capture._stopping.set()
        capture._notify('error', 'worker')
        self.assertEqual(seen, [])

    def test_real_failure_during_stop_is_still_delivered(self):
        capture = self._capture()
        seen = []
        capture._callback = lambda callback, value: seen.append(value)
        capture._stopping.set()
        capture._notify('error', 'protocol')
        self.assertTrue(seen)
        self.assertEqual(seen[-1]['state'], 'error')


class CrossSourceOrderingTests(unittest.TestCase):
    def test_older_tail_emits_before_newer_queued_chunk(self):
        import base64
        import json
        import subprocess
        import sys
        import tempfile
        import threading
        from unittest.mock import patch
        from audio_worker import run_capture
        # Remote full chunk (newer stamp) arrives first and queues; the
        # local tail (older stamp) forms at EOF. Emission must be chronological.
        frames = []
        for i in range(4):
            frames.append({'type': 'pcm', 'source': 'system', 'rate': 16000,
                           'samples': 16000, 'timestamp': 5.0,
                           'pcm': base64.b64encode(bytes(64000)).decode()})
        frames.append({'type': 'pcm', 'source': 'microphone', 'rate': 16000,
                       'samples': 16000, 'timestamp': 3.0,
                       'pcm': base64.b64encode(bytes(64000)).decode()})
        lines = ''.join(json.dumps(f) + '\n' for f in frames)
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        def fake_transcribe(model, samples, speaker, stamp, language):
            return {'type': 'segment', 'speaker': speaker,
                    'text': f'{speaker}@{stamp}', 'timestamp': stamp}
        from types import SimpleNamespace as NS
        stop = threading.Event()
        stop.set()  # force the ordered-flush path, not the live loop
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'x.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)), patch('audio_worker.transcribe', fake_transcribe):
                run_capture(NS(), 'both', 'en', stop=stop, helper=Path('/fixture'))
        segments = [e for e in outputs if e.get('type') == 'segment']
        self.assertEqual(len(segments), 2)
        self.assertEqual([s['text'] for s in segments], ['you@3.0', 'remote@5.0'])


class ShutdownErrorPreservationTests(unittest.TestCase):
    def test_recorded_helper_error_reported_even_when_stop_set(self):
        import json
        import subprocess
        import sys
        import tempfile
        import threading
        from unittest.mock import patch
        from audio_worker import run_capture
        lines = json.dumps({'type': 'error', 'code': 'capture'}) + '\n'
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        from types import SimpleNamespace as NS
        stop = threading.Event()
        stop.set()  # teardown already owns the state
        with tempfile.TemporaryDirectory() as helper_dir:
            helper_script = Path(helper_dir) / 'e.py'
            helper_script.write_text(script)
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, str(helper_script)], **kw)):
                run_capture(NS(), 'microphone', 'en', stop=stop, helper=Path('/fixture'))
        errors = [e for e in outputs if e.get('type') == 'error']
        self.assertTrue(errors, 'a real helper failure must surface even during teardown')
        self.assertEqual(errors[0].get('code'), 'capture')
