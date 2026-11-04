"""Offline error provenance regressions; no audio or provider services."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import server
from test_overlay_descendants import wait_until


class ErrorAttributionTests(unittest.TestCase):
    def make_state(self):
        callbacks = []
        audio = SimpleNamespace(start=lambda **kw: callbacks.append(kw['on_status']), stop=lambda: None)
        class Engine:
            def supports_streaming(self, **kw):
                return False
            def answer(self, *args, **kw):
                raise RuntimeError('Answer provider failed')
        return server.MeetingState(audio_factory=lambda: audio, engine_factory=Engine), callbacks

    def fail_answer(self, state):
        state.assist('Original question', transcript='Private original transcript')
        wait_until(lambda: state.live_workers == 0)

    def test_same_session_resume_does_not_attach_old_answer_to_capture_error(self):
        state, callbacks = self.make_state()
        with patch.object(state, 'watchdog', return_value=None):
            try:
                state.start(True, backend='native')
                self.fail_answer(state)
                before = state.status(touch=False)
                self.assertEqual(before['error_request']['transcript'], 'Private original transcript')
                state.pause()
                state.start(True, backend='native')
                callbacks[-1]({'state': 'error', 'listening': False, 'error': 'Microphone failed'})
                after = state.status(touch=False)
                self.assertIsNone(after['error_request'])
                self.assertIsNone(after['answer_error'])
                self.assertEqual(after['error_source'], 'capture')
                self.assertEqual(after['capture_error'], 'Microphone failed')
                self.assertEqual(after['error'], 'Microphone failed')
            finally:
                state.pause()

    def test_simultaneous_errors_preserve_independent_sources_and_request(self):
        state, callbacks = self.make_state()
        with patch.object(state, 'watchdog', return_value=None):
            try:
                state.start(True, backend='native')
                callbacks[-1]({'state': 'error', 'listening': False, 'error': 'Microphone failed'})
                self.fail_answer(state)
                state.overlay_cleanup_error = 'Overlay cleanup denied'
                state.meetily_clear_error = 'Meetily forget boundary failed'
                state.stop_error = 'Capture shutdown unconfirmed'
                status = state.status(touch=False)
                self.assertEqual(status['error_source'], 'stop')
                self.assertEqual(status['stop_error'], 'Capture shutdown unconfirmed')
                self.assertEqual(status['capture_error'], 'Microphone failed')
                self.assertIn('Answer provider failed', status['answer_error'])
                self.assertEqual(status['error_request']['question'], 'Original question')
                self.assertEqual(status['overlay_cleanup_error'], 'Overlay cleanup denied')
                self.assertEqual(status['meetily_clear_error'], 'Meetily forget boundary failed')
            finally:
                state.pause()

    def test_source_switch_rejects_late_old_capture_and_answer_failure(self):
        state, callbacks = self.make_state()
        entered, release = threading.Event(), threading.Event()
        workers = []
        def answer(*args, **kw):
            workers.append(threading.current_thread())
            entered.set()
            if not release.wait(3):
                raise AssertionError('answer gate timeout')
            raise RuntimeError('Old answer failed')
        with patch.object(state, 'watchdog', return_value=None), patch.object(state.get_engine(), 'answer', side_effect=answer):
            try:
                state.start(True, backend='native', audio_source='microphone')
                old_status = callbacks[-1]
                state.assist('Old question', transcript='Old transcript')
                self.assertTrue(entered.wait(1))
                state.pause()  # source controls are disabled until capture is paused
                state.start(True, backend='native', audio_source='system')
                callbacks[-1]({'state': 'error', 'listening': False, 'error': 'System capture failed'})
                old_status({'state': 'error', 'listening': False, 'error': 'Old microphone failed'})
                release.set()
                workers[0].join(3)
                self.assertFalse(workers[0].is_alive())
                status = state.status(touch=False)
                self.assertEqual(status['capture_error'], 'System capture failed')
                self.assertEqual(status['error_source'], 'capture')
                self.assertIsNone(status['answer_error'])
                self.assertIsNone(status['error_request'])
            finally:
                release.set()
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                state.pause()
