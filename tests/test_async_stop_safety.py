"""Actual adapter/parser/router fault paths; synthetic I/O only, no capture."""
from tests.thread_test_support import mock_capture_preflight
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import macos_audio
import meetily
import server
from tests.thread_test_support import CheckedThreads


class ParserWorker:
    """Only the OS boundary is fake; parsing and group teardown are production."""
    def __init__(self, frame=b'not json\n'):
        self.stdout: Any = io.BytesIO(frame)
        self.alive = True
        self.denied = True
        self.signals = []

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=0):
        if self.alive:
            raise subprocess.TimeoutExpired('synthetic-worker', timeout)
        return 0

    def terminate(self):
        if self.denied:
            raise PermissionError('injected signal denial')
        self.alive = False

    def signal(self, process, sig):
        if process is not self:
            raise AssertionError('wrong process handle')
        self.signals.append(sig)
        self.terminate()


class AsyncStopSafetyTests(CheckedThreads):
    def setUp(self):
        super().setUp()
        mock_capture_preflight(self)

    def client(self, state):
        app = FastAPI()
        app.include_router(server.make_router(state))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def claim(self, client):
        ticket = client.get('/overlay/lifecycle').json()
        return client.post('/overlay/claim', json={**ticket, 'client': 'safety', 'creation': 1}).json()['pane']

    def test_actual_macos_parser_failure_fences_replacement_and_retries(self):
        capture = macos_audio.MacOSAudioCapture()
        state = server.MeetingState(macos_factory=lambda **kw: capture)
        client = self.client(state)
        pane = self.claim(client)
        process = ParserWorker()
        scheduled = []

        def schedule(sequence=None):
            self.assertFalse(getattr(state.lock, '_is_owned')(), 'never schedule under callback/state lock')
            scheduled.append(state.stop_sequence if sequence is None else sequence)

        with patch.object(state, 'watchdog'), \
                patch.object(state, '_start_stop_retry_locked', self.checked_callback(schedule)), \
                patch('macos_audio.worker_python', return_value='/synthetic/python'), \
                patch('macos_audio.subprocess.Popen', return_value=process), \
                patch('macos_audio._signal_group', side_effect=process.signal):
            response = client.post('/start', json={'consent': True, 'backend': 'macos', 'pane': pane, 'action': 1})
            self.assertEqual(response.status_code, 200)
            assert capture._thread is not None
            capture._thread.join(2)
            self.assertFalse(capture._thread.is_alive())
            self.assertIsNone(capture.status()['callback_error'])
            self.assertTrue(process.alive)
            self.assertIs(capture._process, process)
            status = client.get('/status').json()
            self.assertFalse(status['listening'])
            self.assertTrue(status['stopping'])
            self.assertFalse(status['stop_confirmed'])
            self.assertIn('not confirmed', status['error'])
            self.assertEqual(len(scheduled), 1)
            for options in ({}, {'audio_source': 'system'}, {'backend': 'meetily', 'recording_id': 'other'}):
                reply = client.post('/start', json={'consent': True, 'backend': 'macos', 'pane': pane,
                                                    'action': state.overlay_action + 1, **options})
                self.assertEqual(reply.status_code, 409)
            with self.assertRaises(RuntimeError):
                capture.start(lambda segment: None)
            self.assertIs(capture._process, process)
            with patch('server.time.sleep'):
                state._retry_stop(scheduled[0])
            self.assertEqual(state.stop_attempts, 3)
            self.assertIs(state.pending_stop, capture)
            self.assertTrue(process.alive)
            process.denied = False
            response = client.post('/pause', json={'pane': pane, 'action': state.overlay_action + 1})
            self.assertTrue(response.json()['stop_confirmed'])
            self.assertFalse(process.alive)
            self.assertFalse(client.get('/status').json()['stopping'])
            self.assertTrue(capture.status()['stop_confirmed'])
            calls = len(process.signals)
            with patch('server.time.sleep'):
                state._retry_stop(scheduled[0])
            self.assertEqual(len(process.signals), calls, 'stale sequence must never stop a newer capture')

    def test_macos_dead_parent_failed_group_cleanup_still_blocks_start(self):
        capture = macos_audio.MacOSAudioCapture()
        process = ParserWorker()
        process.alive = False  # children/group cleanup is still uncertain
        with patch('macos_audio.worker_python', return_value='/synthetic/python'), \
                patch('macos_audio.subprocess.Popen', return_value=process), \
                patch('macos_audio._signal_group', side_effect=process.signal):
            capture.start(lambda segment: None)
            assert capture._thread is not None
            capture._thread.join(2)
            self.assertFalse(capture._thread.is_alive())
            self.assertFalse(capture.status()['stop_confirmed'])
            with self.assertRaises(RuntimeError):
                capture.start(lambda segment: None)
            self.assertIs(capture._process, process)
            process.denied = False
            capture.stop(timeout=0)
            self.assertTrue(capture.status()['stop_confirmed'])

    def test_async_retry_thread_runs_outside_callback_and_locks(self):
        capture = macos_audio.MacOSAudioCapture()
        process = ParserWorker()
        state = server.MeetingState(macos_factory=lambda **kw: capture)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        retry = state._retry_stop

        def run(sequence):
            try:
                retry(sequence)
            finally:
                done.set()

        def sleep(seconds):
            entered.set()
            self.checked_wait(release, 5, 'retry release')

        real_stop = capture.stop

        def stop():
            self.assertIsNot(threading.current_thread(), capture._thread)
            self.assertFalse(getattr(state.lock, '_is_owned')())
            self.assertFalse(getattr(state.overlay_lock, '_is_owned')())
            real_stop(timeout=.2)

        with patch.object(state, 'watchdog'), \
                patch.object(state, '_retry_stop', self.checked_callback(run)), \
                patch.object(capture, 'stop', self.checked_callback(stop)), \
                patch('server.time', SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=sleep)), \
                patch('macos_audio.worker_python', return_value='/synthetic/python'), \
                patch('macos_audio.subprocess.Popen', return_value=process), \
                patch('macos_audio._signal_group', side_effect=process.signal):
            try:
                state.start(True, 'macos')
                self.assertTrue(entered.wait(2))
                assert capture._thread is not None
                capture._thread.join(2)
                self.assertFalse(capture._thread.is_alive(), 'callback must return without joining itself')
                self.assertIsNone(capture.status()['callback_error'])
                self.assertIs(state.pending_stop, capture)
                process.denied = False
                release.set()
                self.assertTrue(done.wait(2), 'retry must not deadlock on callbacks')
                self.assertFalse(state.status()['stopping'])
                self.assertEqual(state.stop_attempts, 1)
                self.assertFalse(process.alive)
            finally:
                release.set()
                self.assertTrue(done.wait(2))

    def test_actual_macos_clean_shutdown_of_inert_subprocess(self):
        # This child emits protocol only: no native helper, mic, model or provider.
        process = subprocess.Popen([sys.executable, '-u', '-c',
                                    'import time; print(\'{"type":"status","state":"running"}\'); time.sleep(60)'],
                                   stdout=subprocess.PIPE, start_new_session=True)
        capture = macos_audio.MacOSAudioCapture()
        state = server.MeetingState(macos_factory=lambda **kw: capture)
        ready = threading.Event()
        real_status = state.audio_status

        def status(info):
            real_status(info)
            if info.get('state') == 'running':
                ready.set()

        try:
            with patch.object(state, 'watchdog'), \
                    patch.object(state, 'audio_status', self.checked_callback(status)), \
                    patch('macos_audio.worker_python', return_value='/synthetic/python'), \
                    patch('macos_audio.subprocess.Popen', return_value=process):
                state.start(True, 'macos')
                self.assertTrue(ready.wait(2))
                result = state.pause()
                self.assertTrue(result['stop_confirmed'])
                self.assertFalse(capture.status()['running'])
                self.assertIsNotNone(process.poll())
                self.assertIsNone(capture.status()['callback_error'])
                self.assertIsNone(state.pending_stop)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            if capture._thread:
                capture._thread.join(2)
                self.assertFalse(capture._thread.is_alive())
            if process.stdout:
                process.stdout.close()

    def test_actual_meetily_bounded_join_remains_pending_until_reader_exits(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / 'recording'
            folder.mkdir()
            (folder / 'metadata.json').write_text(json.dumps({'status': 'recording'}))
            (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': [
                {'sequence_id': 1, 'audio_start_time': 0, 'text': 'retired snapshot'}]}))
            bridge = meetily.MeetilyBridge(root)
            state = server.MeetingState(bridge_factory=lambda: bridge)
            client = self.client(state)
            pane = self.claim(client)
            entered, release = threading.Event(), threading.Event()
            real_read = bridge._read

            def read(folder, name):
                value = real_read(folder, name)
                if name == 'transcripts.json':
                    entered.set()
                    self.checked_wait(release, 8, 'reader release')
                return value

            try:
                with patch.object(bridge, '_read', self.checked_callback(read)), \
                        patch.object(state, 'watchdog'), patch.object(state, '_start_stop_retry_locked'):
                    reply = client.post('/start', json={'consent': True, 'backend': 'meetily',
                                                        'recording_id': 'recording', 'pane': pane, 'action': 1})
                    self.assertEqual(reply.status_code, 200)
                    self.assertTrue(entered.wait(2))
                    worker = bridge._thread
                    reply = client.post('/pause', json={'pane': pane, 'action': 2})
                    self.assertEqual(reply.status_code, 200)
                    self.assertFalse(reply.json()['stop_confirmed'])
                    self.assertTrue(bridge.status()['running'])
                    self.assertFalse(bridge.status()['stop_confirmed'])
                    self.assertEqual(bridge.status()['status'], 'stopping')
                    self.assertIs(state.pending_stop, bridge)
                    reply = client.post('/start', json={'consent': True, 'backend': 'macos', 'pane': pane, 'action': 3})
                    self.assertEqual(reply.status_code, 409)
                    with self.assertRaises(RuntimeError):
                        bridge.attach('recording', lambda value: None, lambda value: None)
                    self.assertIs(bridge._thread, worker)
                    release.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    with patch('server.time.sleep'):
                        state._retry_stop(state.stop_sequence)
                    self.assertFalse(state.status()['stopping'])
                    self.assertTrue(bridge.status()['stop_confirmed'])
                    self.assertEqual(list(state.segments), [])
                    self.assertEqual(bridge._emitted_seq, {})
                    self.assertEqual(bridge._attached_folders, set(), 'retired scan cannot advance initial window')
                    # Ordinary reattach and clean shutdown after confirmation.
                    delivered = threading.Event()
                    bridge.attach('recording', self.checked_callback(lambda value: delivered.set()), lambda value: None)
                    self.assertTrue(delivered.wait(2))
                    bridge.stop()
                    self.assertTrue(bridge.status()['stop_confirmed'])
                    self.assertFalse(bridge.status()['running'])
            finally:
                release.set()
                if getattr(bridge, '_thread', None):
                    bridge._thread.join(2)
                bridge.stop()

    def test_clear_overlay_fault_stops_capture_and_reports_error(self):
        for fault in ('hide_overlay', 'invalidate'):
            for stop_fails in (False, True):
                with self.subTest(fault=fault, stop_fails=stop_fails):
                    capture = macos_audio.MacOSAudioCapture()
                    process = ParserWorker()
                    process.denied = stop_fails
                    read_started, read_release = threading.Event(), threading.Event()

                    def read_line(size):
                        if not read_started.is_set():
                            read_started.set()
                            return b'{"type":"status","state":"running"}\n'
                        self.checked_wait(read_release, 5, 'clear parser release')
                        return b''

                    def terminate():
                        if process.denied:
                            raise PermissionError('injected clear stop failure')
                        process.alive = False
                        read_release.set()

                    process.stdout = SimpleNamespace(readline=self.checked_callback(read_line), close=lambda: None)
                    process.terminate = terminate
                    state = server.MeetingState(macos_factory=lambda **kw: capture)
                    client = self.client(state)
                    pane = self.claim(client)
                    real_stop = capture.stop
                    with patch.object(state, 'watchdog'), patch.object(state, '_start_stop_retry_locked') as retry, \
                            patch('macos_audio.worker_python', return_value='/synthetic/python'), \
                            patch('macos_audio.subprocess.Popen', return_value=process) as spawn, \
                            patch('macos_audio._signal_group', side_effect=process.signal), \
                            patch.object(capture, 'stop', side_effect=lambda: real_stop(timeout=.2)) as stop:
                        try:
                            state.start(True, 'macos', pane=pane, action=1)
                            self.assertTrue(read_started.wait(2))
                            display = Mock()
                            getattr(display, fault).side_effect = RuntimeError('overlay teardown fault')
                            state.overlay = display
                            reply = client.post('/clear', json={'pane': pane, 'action': 2})
                            self.assertEqual(reply.status_code, 200)
                            result = reply.json()
                            self.assertFalse(result['listening'])
                            self.assertEqual(result['stopping'], stop_fails)
                            self.assertEqual(result['stop_confirmed'], not stop_fails)
                            self.assertEqual(process.alive, stop_fails)
                            self.assertIn('overlay', result['overlay_cleanup_error'].lower())
                            stop.assert_called_once()
                            spawn.assert_called_once()
                            self.assertEqual(retry.call_count, int(stop_fails))
                            self.assertIsNone(state.overlay)
                            self.assertEqual(client.post('/clear', json={'pane': pane, 'action': 1}).status_code, 409)
                            stop.assert_called_once()
                        finally:
                            process.denied = False
                            read_release.set()
                            state.pause(retain_tail=False)
                            if capture._thread:
                                capture._thread.join(2)
                                self.assertFalse(capture._thread.is_alive())
                            self.assertIsNone(capture.status()['callback_error'])
                            self.assertFalse(process.alive)
