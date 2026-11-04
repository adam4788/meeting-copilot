"""Offline error provenance regressions; no audio or provider services."""
import threading
from types import SimpleNamespace
from unittest.mock import patch

import server
from test_overlay_descendants import wait_until
from thread_test_support import CheckedThreads


class ErrorAttributionTests(CheckedThreads):
    def test_synchronous_new_startup_failure_and_pending_stop_survive_start(self):
        from fastapi import HTTPException
        for unconfirmed in (False, True):
            with self.subTest(unconfirmed=unconfirmed):
                def start(**kw):
                    kw['on_status']({'state': 'error', 'error': 'Current startup failed',
                                     'running': unconfirmed, 'stop_confirmed': not unconfirmed})
                audio = SimpleNamespace(start=start, stop=lambda: None)
                state = server.MeetingState(audio_factory=lambda: audio)
                with patch.object(state, 'watchdog'), patch.object(state, '_start_stop_retry_locked'):
                    try:
                        self.assertFalse(state.start(True, backend='native')['listening'])
                        status = state.status(touch=False)
                        self.assertEqual(status['capture_error'], 'Current startup failed')
                        self.assertEqual(status['stopping'], unconfirmed)
                        self.assertEqual(status['error_source'], 'stop' if unconfirmed else 'capture')
                        if unconfirmed:
                            with self.assertRaises(HTTPException):
                                state.start(True, backend='native', audio_source='system')
                            self.assertEqual(state.status(touch=False)['capture_error'], 'Current startup failed')
                            self.assertIs(state.pending_stop, audio)
                    finally:
                        state.pause()

    def test_new_capture_resets_old_error_before_gated_status_callback(self):
        for source in ('microphone', 'system'):
            with self.subTest(source=source):
                state, callbacks = self.make_state()
                entered, release = threading.Event(), threading.Event()
                workers = []
                with patch.object(state, 'watchdog'), patch.object(state, '_start_stop_retry_locked'):
                    try:
                        state.start(True, backend='native', audio_source='microphone')
                        old_status = callbacks[-1]
                        old_status({'state': 'error', 'listening': False, 'error': 'Old microphone failed',
                                    'message': 'Old capture message', 'warning': 'Old loss', 'dropped_chunks': 4})
                        state.pause()
                        state.start(True, backend='native', audio_source=source)
                        new_status = callbacks[-1]

                        def report():
                            entered.set()
                            self.checked_wait(release, 5, 'release new capture status')
                            new_status({'state': 'error', 'listening': False, 'error': 'New startup failed',
                                        'running': True, 'stop_confirmed': False})

                        worker = threading.Thread(target=self.checked_callback(report))
                        workers.append(worker)
                        worker.start()
                        self.assertTrue(entered.wait(1))
                        old_status({'state': 'error', 'error': 'Late old failure'})
                        pending = state.status(touch=False)
                        self.assertTrue(pending['listening'])
                        self.assertIsNone(pending['capture_error'])
                        self.assertIsNone(pending['error'])
                        self.assertIsNone(pending['error_source'])
                        self.assertEqual(pending['capture'], {})
                        self.assertEqual(pending['last_session']['audio_source'], source)
                        release.set()
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                        failed = state.status(touch=False)
                        self.assertEqual(failed['capture_error'], 'New startup failed')
                        self.assertEqual(failed['error_source'], 'stop')
                        self.assertTrue(failed['stopping'])
                        self.assertFalse(failed['stop_confirmed'])
                        self.assertEqual(failed['capture']['state'], 'stopping')
                        self.assertIs(state.pending_stop, state.audio)
                    finally:
                        release.set()
                        for worker in workers:
                            worker.join(2)
                            self.assertFalse(worker.is_alive())
                        state.pause()

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
                self.assertEqual(status['answer_error'], server.ANSWER_FAILURE)
                self.assertNotIn('Answer provider failed', status['answer_error'])
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
