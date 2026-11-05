"""Gated inert real children; never activate audio, UI, or provider services."""
import codecs
import json
import locale
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
import server
from tests.test_overlay_channel_health import closing_display, inert_helper
from tests.test_overlay_descendants import wait_until
from tests.thread_test_support import CheckedThreads


@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'POSIX pipe/select helper required')
class ChannelRaceTests(CheckedThreads):
    def test_reader_failure_publication_serializes_with_admission(self):
        with closing_display() as (bridge, children, close_stdout):
            self.assertTrue(bridge.show_overlay()['shown'])
            process, epoch = bridge.process, bridge._lifecycle_epoch
            reached, published = threading.Event(), threading.Event()
            publish = bridge._fail_reader_locked

            def gated_publish(target):
                reached.set()
                publish(target)
                published.set()

            with patch.object(bridge, '_fail_reader_locked', self.checked_callback(gated_publish)):
                bridge._admission_lock.acquire()
                closer = threading.Thread(target=self.checked_callback(close_stdout))
                closer.start()
                try:
                    self.assertTrue(reached.wait(1), 'real EOF reached admission publication')
                    self.assertFalse(published.wait(.1), 'failure must serialize with capture admission')
                    self.assertNotIn(process, bridge._reader_failed)
                    self.assertEqual(bridge._lifecycle_epoch, epoch)
                finally:
                    bridge._admission_lock.release()
                    closer.join(3)
                    self.assertFalse(closer.is_alive())
            self.assertIn(process, bridge._reader_failed)
            self.assertGreater(bridge._lifecycle_epoch, epoch)

    def test_display_decodes_international_json_as_strict_utf8(self):
        if os.environ.get('OVERLAY_TEST_C_LOCALE'):
            self.assertEqual(codecs.lookup(locale.getpreferredencoding(False)).name, 'ascii')
        event = {'type': 'click', 'question': '日本語 café العربية 👋'}
        wire = (json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8')
        code = f"""
import os, sys, time
if '--protocol-version' in sys.argv:
    print('meeting-overlay/2'); raise SystemExit
os.write(1, {wire!r})
time.sleep(20)
"""
        with inert_helper(code) as (bridge, children):
            received = []
            with patch.object(bridge, 'on_click_line', side_effect=received.append):
                self.assertTrue(bridge.show_overlay()['shown'])
                wait_until(lambda: received or bridge.process in bridge._reader_failed)
            process = bridge.process
            self.assertNotIn(process, bridge._reader_failed)
            self.assertEqual([json.loads(line) for line in received], [event])
            self.assertEqual(process.stdout.encoding.lower().replace('_', '-'), 'utf-8')
            self.assertEqual(process.stdout.errors, 'strict')
            self.assertTrue(bridge.show_overlay()['reused'])

    def test_display_utf8_in_fresh_c_locale_interpreter(self):
        env = dict(os.environ, LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0',
                   OVERLAY_TEST_C_LOCALE='1')
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v',
            'tests.test_overlay_channel_races.ChannelRaceTests.test_display_decodes_international_json_as_strict_utf8'],
            env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_direct_reuse_rechecks_eof_published_inside_poll(self):
        with closing_display() as (bridge, children, close_stdout):
            self.assertTrue(bridge.show_overlay()['shown'])
            process = bridge.process
            poll = process.poll
            entered = threading.Event()

            def gated_poll():
                if not entered.is_set():
                    entered.set()
                    close_stdout()
                return poll()

            probe = bridge._show_process_alive

            def completed_probe(target):
                # Isolate the post-poll health check from scheduler speed. The
                # separate stalled-poll test proves the production deadline.
                probe(target)
                job = bridge._probe_jobs[target]
                self.checked_wait(job['done'], 3, 'real EOF poll completed')
                return job['alive']

            with patch.object(process, 'poll', self.checked_callback(gated_poll)), \
                    patch.object(bridge, '_show_process_alive', completed_probe):
                result = bridge.show_overlay()
            self.assertTrue(entered.is_set())
            self.assertIn(process, bridge._reader_failed)
            self.assertFalse(result['shown'])
            self.assertTrue(bridge._stdout_eof[process].is_set())
            self.assertIsNone(poll())
            self.assertTrue(bridge.hide_overlay()['stop_confirmed'])

    def test_direct_reuse_poll_is_bounded_and_reuses_inflight_probe(self):
        with closing_display() as (bridge, children, close_stdout):
            self.assertTrue(bridge.show_overlay()['shown'])
            process = bridge.process
            poll = process.poll
            entered, release, returned = threading.Event(), threading.Event(), threading.Event()
            calls, results = [], []

            def stalled_poll():
                calls.append(threading.current_thread())
                entered.set()
                self.checked_wait(release, 3, 'release direct poll')
                return poll()

            def show():
                results.append(bridge.show_overlay())
                returned.set()

            with patch.object(process, 'poll', self.checked_callback(stalled_poll)):
                caller = threading.Thread(target=self.checked_callback(show))
                caller.start()
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(returned.wait(.5), 'direct Show must not wait on a stuck poll')
                    self.assertIsNone(results[0]['shown'])
                    self.assertFalse(results[0]['stop_confirmed'])
                    for _ in range(2):
                        started = time.monotonic()
                        self.assertIsNone(bridge.show_overlay()['shown'])
                        self.assertLess(time.monotonic() - started, .3)
                    self.assertEqual(len(calls), 1, 'retry must reuse a stuck worker')
                    # A different child cannot allocate a second probe while
                    # this one is unresolved; its health stays unknown.
                    other = type('OtherProcess', (), {'poll': lambda self: None})()
                    self.assertIsNone(bridge._show_process_alive(other))
                    with bridge.lock:
                        self.assertIn(calls[0], bridge.threads)
                        self.assertIn(calls[0], bridge._show_probe_threads)
                    self.assertTrue(bridge.lock.acquire(timeout=.1))
                    bridge.lock.release()
                finally:
                    release.set()
                    caller.join(3)
                    for worker in calls:
                        if worker is not caller:
                            worker.join(3)
                            self.assertFalse(worker.is_alive())
                    self.assertFalse(caller.is_alive())
            self.assertTrue(bridge.show_overlay()['reused'])

    def test_direct_reuse_caps_stalled_probes_across_five_generations(self):
        with closing_display() as (bridge, children, close_stdout):
            release, entered = threading.Event(), threading.Event()
            workers, targets = [], []

            def stalled_poll():
                workers.append(threading.current_thread())
                entered.set()
                self.checked_wait(release, 15, 'release cross-generation probe')
                return None  # stale healthy result, even after the child exits

            try:
                for cycle in range(5):
                    self.assertTrue(bridge.show_overlay()['shown'])
                    target = bridge.process
                    targets.append(target)
                    with patch.object(target, 'poll', self.checked_callback(stalled_poll)):
                        started = time.monotonic()
                        result = bridge.show_overlay()
                        self.assertIsNone(result['shown'])
                        self.assertFalse(result['stop_confirmed'])
                        self.assertLess(time.monotonic() - started, .3)
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(len(workers), 1, 'one unresolved probe per bridge, not per generation')
                    self.assertEqual(len(bridge._probe_jobs), 1)
                    self.assertTrue(bridge.hide_overlay()['stop_confirmed'])
                self.assertEqual(len(set(targets)), 5)
                self.assertTrue(bridge.show_overlay()['shown'])
                current = bridge.process
                self.assertNotIn(current, targets)
                release.set()
                workers[0].join(3)
                self.assertFalse(workers[0].is_alive())
                # A completed old healthy result must not approve a new child.
                with patch.object(current, 'poll', return_value=1) as current_poll, \
                        patch.object(bridge, '_reap_locked', return_value=False):
                    self.assertIsNone(bridge.show_overlay()['shown'])
                    current_poll.assert_called_once()
                self.assertTrue(bridge.show_overlay()['reused'], 'healthy current probes recover')
            finally:
                release.set()
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                self.assertTrue(bridge.hide_overlay()['stop_confirmed'])
                for worker in bridge._show_probe_threads:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())

    def test_direct_reuse_prunes_completed_probes_without_losing_other_workers(self):
        with closing_display() as (bridge, children, close_stdout):
            self.assertTrue(bridge.show_overlay()['shown'])
            release = threading.Event()
            live = threading.Thread(target=lambda: release.wait(5), daemon=True)
            unstarted = threading.Thread(target=lambda: None, daemon=True)
            live.start()
            with bridge.lock:
                bridge.threads.extend([live, unstarted])
            try:
                for _ in range(400):
                    self.assertTrue(bridge.show_overlay()['reused'])
                    # Pruning requires actual thread exit, not just its result
                    # publication. A back-to-back call may correctly refuse a
                    # completed probe whose worker is still unwinding.
                    for worker in tuple(bridge._show_probe_threads):
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                with bridge.lock:
                    self.assertIn(live, bridge.threads)
                    self.assertIn(unstarted, bridge.threads)
                    probes = [t for t in bridge.threads if t.name == 'overlay-show-probe']
                    self.assertLessEqual(len(probes), 2)
                self.assertTrue(bridge.hide_overlay()['stop_confirmed'])
            finally:
                with bridge.lock:
                    if unstarted in bridge.threads:
                        bridge.threads.remove(unstarted)
                release.set()
                live.join(3)
                self.assertFalse(live.is_alive())

    def test_status_warns_immediately_after_live_eof_without_claiming_shutdown(self):
        with closing_display() as (bridge, children, close_stdout):
            state = server.MeetingState()
            try:
                with patch('overlay_bridge.OverlayBridge', return_value=bridge):
                    self.assertTrue(state.overlay_show({})['shown'])
                close_stdout()
                for _ in range(2):
                    status = state.status(touch=False)
                    self.assertIsNone(status['overlay_shown'])
                    self.assertFalse(status['overlay_stop_confirmed'])
                    self.assertIn('click channel', status['overlay_cleanup_error'])
                    self.assertIn('Retry Hide', status['overlay_cleanup_error'])
                    self.assertIs(state.overlay, bridge, 'status must not change lifecycle ownership')
                self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
                status = state.status(touch=False)
                self.assertFalse(status['overlay_shown'])
                self.assertTrue(status['overlay_stop_confirmed'])
                self.assertIsNone(status['overlay_cleanup_error'])
            finally:
                state.overlay_hide()

    def test_eof_between_each_start_admission_guard_rejects_audio(self):
        for boundary in (1, 2, 3):
            with self.subTest(boundary=boundary), closing_display() as (bridge, children, close_stdout):
                starts = []
                audio = SimpleNamespace(start=lambda **kw: starts.append(kw), stop=lambda: None)
                state = server.MeetingState(audio_factory=lambda: audio)
                try:
                    with patch('overlay_bridge.OverlayBridge', return_value=bridge):
                        self.assertTrue(state.overlay_show({})['shown'])
                    guard = state._overlay_admission_guard
                    calls = []

                    @contextmanager
                    def gated_guard(ticket, *args, **kwargs):
                        calls.append(ticket)
                        if len(calls) == boundary:
                            self.assertIs(ticket[0], bridge)
                            self.assertNotIn(bridge.process, bridge._reader_failed)
                            close_stdout()  # actual EOF is published before admission
                        with guard(ticket, *args, **kwargs):
                            yield

                    with patch.object(state, '_overlay_admission_guard', gated_guard), \
                            patch.object(state, 'watchdog'):
                        with self.assertRaises(HTTPException) as denied:
                            state.start(True, 'native')
                    self.assertEqual(denied.exception.status_code, 409)
                    self.assertEqual(len(calls), boundary)
                    self.assertEqual(starts, [])
                    self.assertFalse(state.listening)
                    self.assertIsNone(state.overlay)
                    self.assertIs(state.pending_overlay, bridge)
                    self.assertIsNone(bridge.process.poll())
                    self.assertTrue(bridge._stdout_eof[bridge.process].is_set())
                    self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
                    with patch.object(state, 'watchdog'):
                        self.assertTrue(state.start(True, 'native')['listening'])
                    self.assertEqual(len(starts), 1, 'confirmed cleanup must permit legitimate retry')
                finally:
                    state.pause(retain_tail=False)
                    state.overlay_hide()


if __name__ == '__main__':
    import unittest
    unittest.main()
