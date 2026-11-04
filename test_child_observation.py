"""Real inert children: unreaped exit, native wait contention and fallbacks."""
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import overlay_bridge
import server
from thread_test_support import CheckedThreads


@contextmanager
def inert_child():
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    assert child.stdin is not None
    try:
        yield child
    finally:
        if not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=3)


def observe_exit(child):
    """Observe, never reap, our exact child with a bounded native wait."""
    child.stdin.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        if result is not None and result.si_pid == child.pid:
            assert result.si_status == 0
            assert child.returncode is None
            return
        time.sleep(.001)
    raise AssertionError('owned child failed to exit')


class ChildObservationTests(CheckedThreads):
    def test_unsupported_native_observation_cannot_authorize_capture(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from test_controller_probe_slots import FakeDisplay
        starts = []
        state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
            start=lambda **kwargs: starts.append(True), stop=lambda: None))
        with inert_child() as child:
            display = FakeDisplay(child)
            state.overlay = display
            try:
                # No global platform mutation and no invented healthy evidence.
                # Windows lacks the POSIX primitives; this exercises that refusal.
                with patch.object(overlay_bridge, 'os', SimpleNamespace(name='nt')):
                    self.assertIsNone(state._overlay_process_alive(child))
                    status = state.status(touch=False)
                    self.assertIsNone(status['overlay_shown'])
                    self.assertFalse(status['overlay_stop_confirmed'])
                    with self.assertRaises(HTTPException) as denied:
                        state.start(True, backend='native')
                    self.assertEqual(denied.exception.status_code, 409)
                    self.assertFalse(state.listening)
                    self.assertEqual(starts, [])
                    self.assertIsNone(child.poll(), 'refusal must not kill a live child')
            finally:
                state.overlay_hide()
                for job in state._overlay_probe_jobs.values():
                    job['worker'].join(2)
                    self.assertFalse(job['worker'].is_alive())

    def require_waitid(self):
        if not all(hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOWAIT', 'WNOHANG')):
            self.skipTest('native non-reaping waitid unavailable')

    def test_native_wait_lock_contention_is_not_healthy_exit_evidence(self):
        self.require_waitid()
        with inert_child() as child:
            # Real Popen.poll returns None without even asking the OS when a
            # different wait() owns this lock. Exercise that native behavior.
            getattr(child, '_waitpid_lock').acquire()
            try:
                self.assertIsNone(child.poll())
                self.assertIs(overlay_bridge._fresh_child_observation(child), True)
                observe_exit(child)
                self.assertIsNone(child.poll())
                self.assertIsNone(overlay_bridge._fresh_child_observation(child))
                self.assertIsNone(child.returncode)
            finally:
                getattr(child, '_waitpid_lock').release()

    def test_posix_fallback_observes_owned_child_and_records_reaping(self):
        self.require_waitid()
        with inert_child() as child:
            with patch.object(overlay_bridge.os, 'waitid', None):
                self.assertIs(overlay_bridge._fresh_child_observation(child), True)
            observe_exit(child)
            with patch.object(overlay_bridge.os, 'waitid', None):
                self.assertIsNone(overlay_bridge._fresh_child_observation(child))
                self.assertEqual(child.returncode, 0)

    def test_missing_primitives_errors_and_contended_fallback_are_unknown(self):
        self.require_waitid()
        with inert_child() as child:
            for failure in (ChildProcessError(), PermissionError(), NotImplementedError()):
                with self.subTest(failure=type(failure).__name__), \
                        patch.object(overlay_bridge.os, 'waitid', side_effect=failure):
                    self.assertIsNone(overlay_bridge._fresh_child_observation(child))
            with patch.object(overlay_bridge.os, 'waitid', None), \
                    patch.object(overlay_bridge.os, 'waitpid', None):
                self.assertIsNone(overlay_bridge._fresh_child_observation(child))
            # Capability simulation only, not a claim of native Windows QA.
            with patch.object(overlay_bridge.os, 'waitid', None), \
                    patch.object(overlay_bridge.os, 'name', 'nt'):
                self.assertIsNone(overlay_bridge._fresh_child_observation(child))
            getattr(child, '_waitpid_lock').acquire()
            try:
                started = time.monotonic()
                with patch.object(overlay_bridge.os, 'waitid', None):
                    self.assertIsNone(overlay_bridge._fresh_child_observation(child))
                self.assertLess(time.monotonic() - started, .2)
            finally:
                getattr(child, '_waitpid_lock').release()
            self.assertIsNone(child.poll(), 'uncertainty must not signal/kill a healthy child')

    def test_native_observation_before_late_join_must_be_repeated(self):
        self.require_waitid()
        for direct in (False, True):
            for exited in (False, True):
                with self.subTest(direct=direct, exited=exited):
                    self.check_late_native_join(direct, exited)

    def test_returned_direct_show_rejects_unreaped_exit_with_delayed_reader(self):
        self.require_waitid()
        for exited in (False, True):
            with self.subTest(exited=exited):
                self.check_late_native_join(True, exited, returned_show=True)

    def check_late_native_join(self, direct, exited, returned_show=False):
        controller = overlay_bridge.OverlayBridge() if direct else server.MeetingState()
        probe = getattr(controller, '_show_process_alive' if direct else '_overlay_process_alive')
        # The maps are pruned/replaced by production; use a live accessor.
        def current_jobs():
            return getattr(controller, '_probe_jobs' if direct else '_overlay_probe_jobs')
        sampled, release, joined = (threading.Event() for _ in range(3))
        real_observe, event_wait = overlay_bridge._fresh_child_observation, threading.Event.wait
        calls, results, seen = [], {}, set()
        seen_lock = threading.Lock()

        def observe(child):
            calls.append(threading.current_thread())
            for name in ('lock', 'lifecycle_lock', 'overlay_lock'):
                lock = getattr(controller, name, None)
                if lock is not None:
                    self.assertFalse(lock._is_owned(), 'OS observation must run off control locks')
            value = real_observe(child)
            if len(calls) == 1:
                self.assertIs(value, True)
                sampled.set()
                self.checked_wait(release, 5, 'release native observation')
            return value

        def observed_wait(event, timeout=None):
            name = threading.current_thread().name
            if (name.startswith('native-waiter-') and timeout == .05
                    and any(job['done'] is event for job in current_jobs().values())):
                with seen_lock:
                    seen.add(name)
                    if len(seen) == 4:
                        joined.set()
            return event_wait(event, timeout)

        with inert_child() as child:
            if returned_show:
                assert isinstance(controller, overlay_bridge.OverlayBridge)
                controller.process = child
            call = (lambda: getattr(controller, 'show_overlay')()) if returned_show else lambda: probe(child)
            callers = [threading.Thread(target=self.checked_callback(
                lambda i=i: results.update({i: call()})), name=f'native-waiter-{i}')
                for i in range(4)]
            try:
                with patch.object(overlay_bridge, '_fresh_child_observation', self.checked_callback(observe)), \
                        patch.object(threading.Event, 'wait', observed_wait):
                    self.assertIsNone(probe(child))
                    self.assertTrue(sampled.wait(1))
                    if exited:
                        observe_exit(child)
                    for caller in callers:
                        caller.start()
                    self.assertTrue(joined.wait(1))
                    release.set()
                    for caller in callers:
                        caller.join(2)
                        self.assertFalse(caller.is_alive())
                    if returned_show:
                        assert isinstance(controller, overlay_bridge.OverlayBridge)
                        self.assertEqual(len(results), 4)
                        for result in results.values():
                            self.assertIs(result['shown'], None if exited else True)
                            if exited:
                                self.assertFalse(result['stop_confirmed'])
                                self.assertTrue(result['error'])
                            else:
                                self.assertTrue(result['reused'])
                        self.assertIs(controller.process, child)
                        self.assertFalse(controller.stopping, 'a query must not retire a healthy bridge')
                    else:
                        self.assertEqual(results, {i: None if exited else True for i in range(4)})
                    self.assertGreaterEqual(len(calls), 2, 'late joins require a fresh OS observation')
                    self.assertEqual(len(set(calls)), 1, 'one worker serves all waiters')
                    self.assertIsNone(child.returncode, 'waitid must not reap the native child')
            finally:
                release.set()
                for caller in callers:
                    if caller.ident is not None:
                        caller.join(2)
                for job in current_jobs().values():
                    job['worker'].join(2)
                    self.assertFalse(job['worker'].is_alive())

    def test_stalled_native_observation_keeps_global_slot_and_deadlines(self):
        self.require_waitid()
        for direct in (False, True):
            with self.subTest(direct=direct), inert_child() as child, inert_child() as other:
                controller = overlay_bridge.OverlayBridge() if direct else server.MeetingState()
                probe = getattr(controller, '_show_process_alive' if direct else '_overlay_process_alive')
                entered, release = threading.Event(), threading.Event()
                workers = []
                def stalled(process):
                    workers.append(threading.current_thread())
                    entered.set()
                    self.checked_wait(release, 5, 'release blocked OS wrapper')
                    return True
                try:
                    with patch.object(overlay_bridge, '_fresh_child_observation', self.checked_callback(stalled)):
                        started = time.monotonic()
                        self.assertIsNone(probe(child))
                        self.assertTrue(entered.wait(1))
                        for _ in range(3):
                            self.assertIsNone(probe(other))
                            self.assertIsNone(probe(child))
                        self.assertLess(time.monotonic() - started, .8)
                        self.assertEqual(len(workers), 1)
                        # Observation must not own lifecycle/publication locks.
                        for name in ('lock', 'lifecycle_lock', 'overlay_lock'):
                            lock = getattr(controller, name, None)
                            if lock is not None:
                                self.assertTrue(lock.acquire(timeout=.1))
                                lock.release()
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
