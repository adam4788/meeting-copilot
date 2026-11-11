"""Google cancellation fixtures: no devices, network, real keys or audio."""
import asyncio
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import audio_worker as w
import google_asr as g
from tests.test_meta_error_shutdown import _SyntheticHelper


class FixtureCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_fixture_and_awaits_delayed_close(self):
        stop = threading.Event()
        closed = asyncio.Event()
        async def transcribe(*args, **kwargs):
            try:
                stop.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.03)
                closed.set()
        with patch.object(g, 'transcribe_wav', side_effect=transcribe):
            with self.assertRaises(g.GoogleAsrError):
                await w._run_google_fixture(b'synthetic', 'fake-key', stop, lambda _: None)
        self.assertTrue(closed.is_set())

    async def test_preexisting_stop_does_not_start_transmission(self):
        stop = threading.Event()
        stop.set()
        with patch.object(g, 'transcribe_wav', side_effect=AssertionError('not authorized after stop')):
            with self.assertRaises(g.GoogleAsrError):
                await w._run_google_fixture(b'synthetic', 'fake-key', stop, lambda _: None)

    async def test_completed_fixture_returns_report_and_retires_watcher(self):
        async def transcribe(*args, **kwargs):
            return {'tail_confirmed': False}
        with patch.object(g, 'transcribe_wav', side_effect=transcribe):
            result = await w._run_google_fixture(b'synthetic', 'fake-key', threading.Event(), lambda _: None)
        self.assertEqual(result, {'tail_confirmed': False})

    async def test_external_task_cancellation_also_awaits_close(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def transcribe(*args, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.03)
                closed.set()
        with patch.object(g, 'transcribe_wav', side_effect=transcribe):
            task = asyncio.create_task(w._run_google_fixture(b'synthetic', 'fake-key', threading.Event(), lambda _: None))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())


class CaptureCancellationTests(unittest.TestCase):
    def test_capture_keeps_loop_alive_for_google_connection_close(self):
        stop = threading.Event()
        helper = _SyntheticHelper(finish_delay=.01, initial_drops=0)
        closed, events = [], []
        async def session(*args, **kwargs):
            try:
                stop.set()
                await asyncio.Event().wait()  # Requires cancellation, not EOF.
            finally:
                await asyncio.sleep(.03)
                closed.append(True)
        with patch.object(w.subprocess, 'Popen', return_value=helper), \
             patch.object(w, 'emit', side_effect=events.append), \
             patch.object(g, 'run_session', side_effect=session), \
             patch.object(w, 'GOOGLE_SESSION_DRAIN_TIMEOUT', .02):
            w.run_capture_cloud('microphone', 'ENDPOINTING', 'fake-key', g.DEFAULT_MODEL,
                                helper=Path('/synthetic'), stop=stop, provider='google')
        self.assertEqual(closed, [True])
        self.assertTrue(any('drain was incomplete' in event.get('warning', '') for event in events))
        self.assertEqual(helper.terminate_calls, 1)

    def test_google_budget_fits_adapter_without_modifying_meta(self):
        self.assertLess(1.05 + .1 + w.GOOGLE_SESSION_DRAIN_TIMEOUT + w.GOOGLE_CLEANUP_TIMEOUT, 9.5)
        self.assertEqual(w.META_SESSION_DRAIN_TIMEOUT, .75)
