"""Synthetic process/protocol tests. Never starts real Windows audio devices."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import windows_audio
import windows_audio_worker as worker


class WindowsIsolationTests(unittest.TestCase):
    def test_environment_has_no_credentials_or_loader_overrides(self):
        with patch.dict(os.environ, {'SystemRoot': 'C:/Windows', 'USERPROFILE': 'C:/Users/Test',
                                     'OPENAI_API_KEY': 'synthetic', 'HF_TOKEN': 'synthetic',
                                     'PYTHONPATH': 'bad', 'PYTHONSTARTUP': 'bad',
                                     'HF_HUB_OTHER_TOKEN': 'bad', 'HTTP_PROXY': 'bad',
                                     'HF_HUB_OFFLINE': '0', 'HF_HOME': 'public-cache',
                                     'PATH': 'untrusted-tool-folder;secret-path'}, clear=True):
            env = windows_audio.worker_environment()
        self.assertEqual(env['SYSTEMROOT'], 'C:/Windows')
        self.assertEqual(env['HF_HOME'], 'public-cache')
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')
        self.assertEqual(env['HF_HUB_DISABLE_IMPLICIT_TOKEN'], '1')
        for name in ('OPENAI_API_KEY', 'HF_TOKEN', 'PYTHONPATH', 'PYTHONSTARTUP', 'HF_HUB_OTHER_TOKEN', 'HTTP_PROXY'):
            self.assertNotIn(name, env)


    def test_windows_path_contains_only_os_resolved_system_directory(self):
        trusted = str(Path('C:/Windows/System32'))
        with patch.dict(os.environ, {'PATH': 'untrusted-tool-folder;secret-path',
                                     'SystemRoot': 'untrusted-root', 'OPENAI_API_KEY': 'synthetic'}, clear=True), \
             patch.object(windows_audio, 'trusted_system_directory', return_value=trusted):
            env = windows_audio.worker_environment()
        self.assertEqual(env['PATH'], trusted)
        self.assertNotIn('untrusted-tool-folder', str(env))
        self.assertNotIn('OPENAI_API_KEY', env)

    def test_system_directory_resolution_error_fails_closed(self):
        with patch.object(windows_audio, 'trusted_system_directory', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                windows_audio.worker_environment()

    @unittest.skipUnless(sys.platform == 'win32', 'Real Windows directory API')
    def test_windows_system_directory_api_returns_existing_absolute_directory(self):
        path = Path(windows_audio.trusted_system_directory())
        self.assertTrue(path.is_absolute())
        self.assertTrue(path.is_dir())
        self.assertTrue((path / 'ole32.dll').is_file())

    def test_adapter_is_local_only_and_fixed_command(self):
        with self.assertRaises(ValueError):
            windows_audio.WindowsAudioCapture(provider='meta')
        capture = windows_audio.WindowsAudioCapture(source='both')
        root = Path.cwd() / 'fixed' / 'plugin'
        command = capture._worker_command(root, None)
        self.assertEqual(command, [sys.executable, '-B', '-u',
                                   str(root / 'windows_audio_worker.py'),
                                   '--model', 'base.en', '--source', 'both'])
        self.assertNotIn('--provider', command)
        self.assertEqual(capture._worker_stdin(None), subprocess.PIPE)
        with self.assertRaises(RuntimeError):
            capture._worker_stdin(b'secret')

    def test_startup_trace_is_bounded_metadata_and_copy_safe(self):
        capture = windows_audio.WindowsAudioCapture()
        capture._on_status = lambda value: None
        for _ in range(20):
            capture._notify('loading')
            capture._notify('starting')
        trace = capture.status()['startup_trace']
        self.assertEqual(len(trace), 16)
        self.assertTrue(all(set(row) == {'state', 'elapsed_ms'} for row in trace))
        self.assertTrue(all(row['elapsed_ms'] >= 0 for row in trace))
        trace[0]['state'] = 'changed'
        self.assertNotEqual(capture.status()['startup_trace'][0]['state'], 'changed')
        with patch.object(windows_audio, 'worker_environment', return_value={}):
            capture._worker_environment()
        self.assertEqual(capture.status()['startup_trace'], [{'state': 'starting', 'elapsed_ms': 0}])

    def test_control_only_stops_and_eof_is_fail_closed(self):
        for value in (b'{"action":"stop"}\n', b'', b'{"action":"start"}\n', b'x' * 100):
            stop = threading.Event()
            worker.read_control(io.BytesIO(value), stop)
            self.assertTrue(stop.is_set())

    def test_control_partial_line_and_idle_shutdown(self):
        read_fd, write_fd = os.pipe()
        stop = threading.Event()
        with os.fdopen(read_fd, 'rb', buffering=0) as stream:
            try:
                with worker.control_scope(stream, stop):
                    os.write(write_fd, b'{"action":')
                    self.assertFalse(stop.wait(.1))
                    os.write(write_fd, b'"stop"}\n')
                    self.assertTrue(stop.wait(1))
            finally:
                os.close(write_fd)
        self.assertFalse(any(t.name == 'windows-audio-control' for t in threading.enumerate()))

    def test_control_eof_malformed_and_oversized(self):
        for data in (b'', b'bad\n', b'x' * 65, b'{"action":"start"}\n'):
            read_fd, write_fd = os.pipe()
            stop = threading.Event()
            with os.fdopen(read_fd, 'rb', buffering=0) as stream:
                try:
                    with worker.control_scope(stream, stop):
                        if data:
                            os.write(write_fd, data)
                        else:
                            os.close(write_fd)
                            write_fd = None
                        self.assertTrue(stop.wait(1))
                finally:
                    if write_fd is not None:
                        os.close(write_fd)

    def test_idle_control_allows_native_import_and_clean_process_exit(self):
        # Keep stdin OPEN and empty throughout startup/exit. The old buffered
        # reader stalled NumPy loading on Windows and could abort at shutdown.
        script = """
import sys, threading
import windows_audio_worker as worker
with worker.control_scope(sys.stdin.buffer, threading.Event()):
    import numpy
print('native-import-and-clean-exit', flush=True)
"""
        child = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.wait(timeout=15)
            self.assertEqual(child.returncode, 0, child.stderr.read().decode())
            self.assertEqual(child.stdout.read().strip(), b'native-import-and-clean-exit')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_invalid_probe_never_loads_model_or_starts_audio(self):
        events = []
        with patch.object(worker, 'emit', events.append):
            result = worker.run('microphone', 'base.en', probe=lambda source: {'ready': False},
                                model_loader=lambda name: self.fail('must not load'))
        self.assertEqual(result, 1)
        self.assertEqual(events[-1], {'type': 'error', 'code': 'capture'})

    def test_stop_while_model_loads_never_opens_audio(self):
        # Reproduces a bounded test ending during cold model startup.
        stop, loading, release = threading.Event(), threading.Event(), threading.Event()
        events, result = [], []
        def model_loader(name):
            loading.set()
            self.assertTrue(release.wait(2))
            return object()
        probe = lambda source: {'version': 1, 'ready': True, 'capture_started': False,
                                'source': source, 'endpoints': {'microphone': {'id': 'exact-mic', 'ready': True}}}
        def run():
            result.append(worker.run('microphone', 'base.en', stop=stop, probe=probe,
                                     model_loader=model_loader,
                                     capture_factory=lambda **kw: self.fail('audio must remain closed')))
        with patch.object(worker, 'emit', events.append):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(loading.wait(1))
                stop.set()
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])
        self.assertEqual(events, [{'type': 'status', 'state': 'loading'}])

    def test_bound_endpoints_and_final_loss_survive_worker(self):
        events, calls = [], []
        stop = threading.Event()
        class Capture:
            def __init__(self, **kwargs):
                calls.append(kwargs)
            def set_recording_directory(self, directory):
                self.directory = directory
            def start(self, segment, status):
                self.on_status = status
                segment({'speaker': 'you', 'text': 'synthetic speech', 'timestamp': 2})
                stop.set()
            def stop(self, timeout):
                calls.append(timeout)
            def status(self):
                return {'state': 'stopped', 'running': False, 'worker_dropped_chunks': 2,
                        'sub_second_dropped_chunks': 1, 'error': 'do not expose secret/path'}
        probe = lambda source: {'version': 1, 'ready': True, 'capture_started': False,
                                'source': source, 'endpoints': {'microphone': {'id': 'exact-mic', 'name': 'test', 'ready': True}}}
        with patch.object(worker, 'emit', events.append):
            result = worker.run('microphone', 'base.en', stop=stop, probe=probe,
                                model_loader=lambda name: object(), capture_factory=Capture)
        self.assertEqual(result, 0)
        self.assertEqual(events[:2], [{'type': 'status', 'state': 'loading'},
                                      {'type': 'status', 'state': 'starting'}])
        self.assertEqual(calls[0]['input_id'], 'exact-mic')
        self.assertIsNone(calls[0]['output_id'])
        self.assertEqual(events[-1]['worker_dropped_chunks'], 2)
        self.assertEqual(events[-1]['sub_second_dropped_chunks'], 1)
        self.assertNotIn('secret/path', json.dumps(events))

    def test_private_stdin_gracefully_stops_real_synthetic_process(self):
        # Run an inert Python process in place of the fixed capture script.
        # This tests adapter pipe + parser + teardown, not devices/providers.
        script = ('import sys,json; print(json.dumps({"type":"status","state":"running"}),flush=True); '
                  'line=sys.stdin.buffer.readline(); '
                  'print(json.dumps({"type":"status","state":"stopped","worker_dropped_chunks":1,"warning":"synthetic tail"}),flush=True)')
        capture = windows_audio.WindowsAudioCapture()
        statuses = []
        with patch.object(capture, '_worker_command', return_value=[sys.executable, '-u', '-c', script]):
            capture.start(lambda value: self.fail('no segments expected'), statuses.append)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and capture.status()['state'] != 'running':
                time.sleep(.01)
            self.assertEqual(capture.status()['state'], 'running')
            capture.stop(timeout=2)
        self.assertTrue(capture.status()['stop_confirmed'])
        self.assertEqual(statuses[-1]['worker_dropped_chunks'], 1)
        self.assertNotIn('Forced shutdown', statuses[-1]['warning'])

    def test_forced_worker_stop_marks_partial(self):
        capture = windows_audio.WindowsAudioCapture()
        statuses = []
        with patch.object(capture, '_worker_command', return_value=[sys.executable, '-u', '-c', 'import time; time.sleep(30)']):
            capture.start(lambda value: None, statuses.append)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and capture._process is None:
                time.sleep(.01)
            self.assertIsNotNone(capture._process)
            capture.stop(timeout=.2)
        self.assertIn('Forced shutdown', statuses[-1]['warning'])
        self.assertFalse(capture.status()['running'])

    def test_each_audio_thread_owns_and_releases_its_com_keeper(self):
        from audio import AudioCapture
        events = []
        class Keeper:
            def __init__(self):
                events.append(('enter', threading.get_ident()))
            def __del__(self):
                events.append(('exit', threading.get_ident()))
        capture = worker.WindowsThreadCapture(com_factory=Keeper)
        for method in ('_run', '_capture'):
            with patch.object(AudioCapture, method, side_effect=lambda *args: events.append(('body', threading.get_ident()))):
                fn = capture._run if method == '_run' else lambda: capture._capture('you', object())
                thread = threading.Thread(target=fn)
                thread.start()
                thread.join(1)
                self.assertFalse(thread.is_alive())
        self.assertEqual([kind for kind, _ in events], ['enter', 'body', 'exit'] * 2)
        self.assertTrue(all(events[i][1] == events[i + 1][1] == events[i + 2][1] for i in (0, 3)))
        self.assertTrue(all(ident != threading.get_ident() for _, ident in events))

    def test_com_context_releases_when_audio_raises(self):
        from audio import AudioCapture
        events = []
        class Keeper:
            def __init__(self):
                events.append('enter')
            def __del__(self):
                events.append('exit')
        for method in ('_run', '_capture'):
            capture = worker.WindowsThreadCapture(com_factory=Keeper)
            with patch.object(AudioCapture, method, side_effect=RuntimeError('synthetic')):
                if method == '_run':
                    capture._run()
                else:
                    capture._capture('you', object())
            self.assertTrue(capture._stop.is_set())
            self.assertEqual(capture.status()['state'], 'error')
        self.assertEqual(events, ['enter', 'exit'] * 2)

    def test_worker_preserves_long_segments_and_rejects_oversized_ones(self):
        probe = lambda source: {'version': 1, 'ready': True, 'capture_started': False,
                                'source': source, 'endpoints': {'microphone': {'id': 'exact-mic', 'ready': True}}}
        for length in (10000, 16001):
            stop = threading.Event()
            events = []
            class Capture:
                def __init__(self, **kwargs):
                    pass
                def set_recording_directory(self, directory):
                    pass
                def start(self, segment, status):
                    segment({'speaker': 'you', 'text': 'x' * length, 'timestamp': 1})
                    stop.set()
                def stop(self, timeout):
                    pass
                def status(self):
                    return {'state': 'stopped', 'running': False}
            with patch.object(worker, 'emit', events.append):
                result = worker.run('microphone', 'base.en', stop=stop, probe=probe,
                                    model_loader=lambda name: object(), capture_factory=Capture)
            segments = [event for event in events if event.get('type') == 'segment']
            if length == 10000:
                self.assertEqual(result, 0)
                self.assertEqual(len(segments[0]['text']), 10000)
            else:
                self.assertEqual(result, 1)
                self.assertFalse(segments)
                self.assertEqual(events[-1]['type'], 'error')
