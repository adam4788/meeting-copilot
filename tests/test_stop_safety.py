"""Physical-stop uncertainty: real adapter methods, no microphone or model access."""
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import audio
import macos_audio
from tests.thread_test_support import CheckedThreads


class AdapterStopSafetyTests(CheckedThreads):
    def test_native_stop_signals_worker_even_if_status_callback_raises(self):
        capture = audio.AudioCapture()
        worker = threading.Thread(target=lambda: capture._stop.wait(3), daemon=True)
        capture._threads = [worker]
        worker.start()
        capture._on_status = lambda value: (_ for _ in ()).throw(RuntimeError('status callback failed'))
        try:
            capture.stop(timeout=.2)
            self.assertTrue(capture._stop.is_set(), 'observer failure must not skip shutdown signal')
            worker.join(.3)
            self.assertFalse(worker.is_alive())
        finally:
            capture._stop.set()
            worker.join(1)

    def test_router_keeps_real_native_worker_visibly_stopping_until_retry(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import server
        capture = audio.AudioCapture()
        done = threading.Event()
        worker = threading.Thread(target=lambda: done.wait(3), daemon=True)
        capture._threads = [worker]
        state = server.MeetingState(audio_factory=lambda: capture)
        app = FastAPI()
        app.include_router(server.make_router(state))
        client = TestClient(app)
        ticket = client.get('/overlay/lifecycle').json()
        pane = client.post('/overlay/claim', json={**ticket, 'client': 'real-adapter', 'creation': 1}).json()['pane']
        # Install only the worker fixture: calling start would capture hardware.
        state.audio, state.backend, state.listening = capture, 'native', True
        worker.start()
        real_stop = capture.stop
        try:
            with patch.object(capture, 'stop', side_effect=lambda: real_stop(timeout=0)), \
                    patch.object(state, '_start_stop_retry_locked'):
                response = client.post('/pause', json={'pane': pane, 'action': 1})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()['stop_confirmed'])
                self.assertTrue(worker.is_alive())
                self.assertTrue(capture._stop.is_set())
                self.assertEqual(client.get('/status').json()['capture']['state'], 'stopping')
                done.set()
                worker.join(1)
                with patch('server.time.sleep'):
                    state._retry_stop(state.stop_sequence)
                self.assertFalse(client.get('/status').json()['stopping'])
        finally:
            done.set()
            worker.join(1)
            client.close()

    def test_macos_failed_signal_keeps_process_for_bounded_retry(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True)
        capture = macos_audio.MacOSAudioCapture()
        capture._process = process
        capture._stopping = threading.Event()
        capture._on_status = lambda value: None
        try:
            with patch.object(process, 'terminate', side_effect=PermissionError('signal denied')), \
                    patch.object(macos_audio, '_signal_group', side_effect=PermissionError('signal denied')):
                with self.assertRaises((OSError, RuntimeError)):
                    capture.stop(timeout=0)
            self.assertIs(capture._process, process, 'failed stop must not orphan the process handle')
            self.assertIsNone(process.poll())
            self.assertTrue(capture._cancel.is_set(), 'delivery must be fenced even if physical stop fails')
            self.assertEqual(capture.status()['state'], 'stopping')
            self.assertTrue(capture.status()['running'])
            capture.stop(timeout=.2)
            self.assertIsNotNone(process.poll())
            self.assertFalse(capture.status()['running'])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)


if __name__ == '__main__':
    unittest.main()
