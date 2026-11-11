"""Controller probe lifetime regressions with inert children and fakes."""
from tests.thread_test_support import mock_capture_preflight
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

import server
from tests.thread_test_support import CheckedThreads


class FakeDisplay:
    def __init__(self, process):
        self.process = process
        self.stopping = False
        self.scope_seq = 0

    def set_scope(self, scope):
        return {}

    def update(self, status):
        pass

    def show_overlay(self):
        return {'shown': True}

    def invalidate(self):
        self.stopping = True

    def hide_overlay(self):
        # Fake owned shutdown is independent of the stuck inspection wrapper.
        self.process = None
        return {'stop_confirmed': True}

    def helper_status(self):
        return {'available': True}


class ControllerProbeSlotTests(CheckedThreads):
    def setUp(self):
        super().setUp()
        mock_capture_preflight(self)

    @unittest.skipUnless(os.name == 'posix', 'healthy real-child evidence requires POSIX wait primitives')
    def test_sampled_alive_before_publication_requires_uncontradicted_evidence(self):
        # No reader runs for this inert child: model delayed EOF notification,
        # with wait() already recording exit on the exact Popen handle.
        for action in ('status', 'start', 'show', 'waiters'):
            for exited in (False, True):
                with self.subTest(action=action, exited=exited):
                    self._check_sampled_alive(action, exited)

    def test_exited_unreaped_child_cannot_supply_prejoin_sample(self):
        if not all(hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOWAIT')):
            self.skipTest('non-reaping waitid observation unavailable')
        for action in ('status', 'start', 'show', 'waiters'):
            with self.subTest(action=action):
                self._check_sampled_alive(action, True, unreaped=True)

    def _check_sampled_alive(self, action, exited, unreaped=False):
        starts, results, workers = [], {}, []
        state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
            start=lambda **kwargs: starts.append(True), stop=lambda: None))
        sampled, release, joined = (threading.Event() for _ in range(3))
        wait_lock = threading.Lock()
        seen = set()
        event_wait = threading.Event.wait
        child = subprocess.Popen(
            [sys.executable, '-c', 'import sys; sys.stdin.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stdin = child.stdin
        assert stdin is not None
        display = FakeDisplay(child)
        state.overlay = display
        real_poll = child.poll
        count = 4 if action == 'waiters' else 1

        def poll():
            workers.append(threading.current_thread())
            self.assertEqual(threading.current_thread().name, 'meeting-overlay-probe')
            for lock in (state.lock, state.lifecycle_lock, state.overlay_lock):
                self.assertFalse(lock._is_owned(), 'poll must not own control locks')  # pyright: ignore[reportAttributeAccessIssue]
            value = real_poll()
            if len(workers) == 1:
                self.assertIsNone(value, 'gate must follow an actual live sample')
                sampled.set()
                self.checked_wait(release, 5, 'release sampled-live poll')
            return value

        def observed_wait(event, timeout=None):
            name = threading.current_thread().name
            if (name.startswith('late-sample-caller-') and timeout == .05
                    and any(job['done'] is event for job in state._overlay_probe_jobs.values())):
                with wait_lock:
                    seen.add(name)
                    if len(seen) == count:
                        joined.set()
            return event_wait(event, timeout)

        def call(index):
            try:
                if action == 'status':
                    value = state.status(touch=False)
                elif action == 'start':
                    value = state.start(True, backend='native')
                elif action == 'show':
                    value = state.overlay_show({})
                else:
                    value = state._overlay_process_alive(child)
                results[index] = value
            except HTTPException as exc:
                results[index] = exc

        callers = [threading.Thread(target=self.checked_callback(lambda i=i: call(i)),
                                    name=f'late-sample-caller-{i}') for i in range(count)]
        try:
            with patch.object(child, 'poll', self.checked_callback(poll)), \
                    patch.object(threading.Event, 'wait', observed_wait), \
                    patch.object(state, '_start_overlay_lease_locked'):
                self.assertIsNone(state._overlay_process_alive(child))
                self.assertTrue(sampled.wait(1))
                job = state._overlay_probe_jobs[child]
                self.assertFalse(job['done'].is_set())
                if exited:
                    stdin.close()
                    if unreaped:
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            observed = os.waitid(os.P_PID, child.pid,
                                                 os.WEXITED | os.WNOWAIT | os.WNOHANG)
                            if observed is not None and observed.si_pid == child.pid:
                                break
                            time.sleep(.001)
                        else:
                            self.fail('owned child did not exit within observation deadline')
                        self.assertEqual(observed.si_status, 0)
                        self.assertIsNone(child.returncode, 'exit must remain unreaped')
                    else:
                        self.assertEqual(child.wait(timeout=3), 0)
                        self.assertEqual(child.returncode, 0)
                for caller in callers:
                    caller.start()
                self.assertTrue(joined.wait(1), 'late callers must join before publication')
                release.set()
                for caller in callers:
                    caller.join(2)
                    self.assertFalse(caller.is_alive())
                if action == 'waiters':
                    self.assertEqual(results, {i: None if exited else True for i in range(count)})
                    self.assertEqual(len(workers), 1, 'all waiters share one worker')
                elif action == 'status':
                    self.assertIs(results[0]['overlay_shown'], None if exited else True)
                    self.assertEqual(bool(results[0]['overlay_cleanup_error']), exited)
                    self.assertFalse(results[0]['overlay_stop_confirmed'])
                elif exited:
                    self.assertIsInstance(results[0], HTTPException)
                    self.assertEqual(results[0].status_code, 409 if action == 'start' else 503)
                    self.assertFalse(state.listening)
                    self.assertEqual(starts, [])
                    self.assertIs(state.pending_overlay, display)
                elif action == 'start':
                    self.assertTrue(results[0]['listening'])
                    self.assertEqual(starts, [True])
                else:
                    self.assertTrue(results[0]['shown'])
                    self.assertTrue(results[0]['reused'])
        finally:
            release.set()
            for caller in callers:
                if caller.ident is not None:
                    caller.join(2)
                    self.assertFalse(caller.is_alive())
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            state.overlay_hide()
            if state.listening:
                state.pause(retain_tail=False)
            if not stdin.closed:
                stdin.close()
            child.wait(timeout=3)

    @unittest.skipUnless(os.name == 'posix', 'healthy real-child evidence requires POSIX wait primitives')
    def test_known_exit_at_each_start_guard_requires_fresh_evidence(self):
        for boundary in (1, 2, 3):
            with self.subTest(boundary=boundary):
                self._check_exit_at_start_guard(boundary)

    def _check_exit_at_start_guard(self, boundary):
        starts, workers = [], []
        state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
            start=lambda **kwargs: starts.append(True), stop=lambda: None))
        child = subprocess.Popen(
            [sys.executable, '-c', 'import sys; sys.stdin.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stdin = child.stdin
        assert stdin is not None
        display = FakeDisplay(child)
        state.overlay = display
        release = threading.Event()
        guard, real_poll = state._overlay_admission_guard, child.poll
        calls = []

        def poll():
            workers.append(threading.current_thread())
            for lock in (state.lock, state.lifecycle_lock, state.overlay_lock):
                self.assertFalse(lock._is_owned())  # pyright: ignore[reportAttributeAccessIssue]
            if child.returncode is not None:
                self.checked_wait(release, 5, 'release admission refresh')
            return real_poll()

        @contextmanager
        def gated_guard(ticket, *args, **kwargs):
            calls.append(ticket)
            if len(calls) == boundary:
                stdin.close()
                self.assertEqual(child.wait(timeout=3), 0)
            with guard(ticket, *args, **kwargs):
                yield

        try:
            with patch.object(child, 'poll', self.checked_callback(poll)), \
                    patch.object(state, '_overlay_admission_guard', gated_guard):
                started = time.monotonic()
                with self.assertRaises(HTTPException) as denied:
                    state.start(True, backend='native')
                self.assertEqual(denied.exception.status_code, 409)
                self.assertLess(time.monotonic() - started, .5)
                self.assertEqual(starts, [])
                self.assertFalse(state.listening)
                self.assertIs(state.pending_overlay, display)
                self.assertEqual(len(calls), boundary)
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            state.overlay_hide()
            if state.listening:
                state.pause(retain_tail=False)
            if not stdin.closed:
                stdin.close()
            child.wait(timeout=3)

    def test_status_probe_shares_with_concurrent_start_or_show(self):
        for action in ('start', 'show'):
            with self.subTest(action=action):
                starts = []
                state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
                    start=lambda **kwargs: starts.append(True), stop=lambda: None))
                poll_entered, action_entered, release = (threading.Event() for _ in range(3))
                polls, results = [], {}
                event_wait = threading.Event.wait
                def observed_wait(event, timeout=None):
                    if (threading.current_thread().name == 'concurrent-action' and timeout == .05
                            and any(job['done'] is event for job in state._overlay_probe_jobs.values())):
                        action_entered.set()  # admitted before completion, now waiting
                    return event_wait(event, timeout)
                def poll():
                    polls.append(threading.current_thread())
                    poll_entered.set()
                    self.checked_wait(release, 2, 'release status poll')
                    return None
                process = Mock(returncode=None, poll=self.checked_callback(poll))
                display = FakeDisplay(process)
                state.overlay = display
                def run_action():
                    results['action'] = (state.start(True, backend='native') if action == 'start'
                                         else state.overlay_show({}))
                status_thread = threading.Thread(target=self.checked_callback(
                    lambda: results.update(status=state.status(touch=False))))
                action_thread = threading.Thread(target=self.checked_callback(run_action),
                                                 name='concurrent-action')
                try:
                    with patch.object(threading.Event, 'wait', observed_wait), \
                            patch.object(state, '_start_overlay_lease_locked'):
                        status_thread.start()
                        self.assertTrue(poll_entered.wait(1))
                        action_thread.start()
                        self.assertTrue(action_entered.wait(1))
                        # sleep(.025) can overshoot the entire 50ms budget on
                        # heavily loaded hosts; hold the gate for 25ms directly.
                        deadline = time.monotonic() + .025
                        while time.monotonic() < deadline:
                            pass
                        release.set()
                        status_thread.join(2)
                        action_thread.join(2)
                        self.assertFalse(status_thread.is_alive())
                        self.assertFalse(action_thread.is_alive())
                        self.assertTrue(results['status']['overlay_shown'])
                        self.assertIs(state.overlay, display)
                        self.assertIsNone(state.pending_overlay)
                        if action == 'start':
                            self.assertTrue(state.listening)
                            self.assertEqual(starts, [True])
                        else:
                            self.assertTrue(results['action']['shown'])
                        for worker in polls:
                            worker.join(2)
                            self.assertFalse(worker.is_alive())
                finally:
                    release.set()
                    status_thread.join(2)
                    if action_thread.ident is not None:
                        action_thread.join(2)
                    for worker in polls:
                        worker.join(2)
                    state.overlay_hide()
                    if state.listening:
                        state.pause(retain_tail=False)

    def test_multiple_waiters_share_only_inflight_result_and_time_out(self):
        for stalled in (False, True):
            with self.subTest(stalled=stalled):
                state = server.MeetingState()
                entered, release, all_waiting = (threading.Event() for _ in range(3))
                wait_lock = threading.Lock()
                waiters_seen, results, workers = set(), {}, []
                original_wait = threading.Event.wait
                def observed_wait(event, timeout=None):
                    if (threading.current_thread().name.startswith('probe-caller-')
                            and timeout == .05
                            and any(job['done'] is event for job in state._overlay_probe_jobs.values())):
                        with wait_lock:
                            waiters_seen.add(threading.current_thread().name)
                            if len(waiters_seen) == 4:
                                all_waiting.set()
                    return original_wait(event, timeout)
                def poll():
                    workers.append(threading.current_thread())
                    entered.set()
                    self.checked_wait(release, 5, 'release shared poll')
                    return None
                process = Mock(poll=self.checked_callback(poll))
                other = Mock(poll=Mock(return_value=None))
                def call(index):
                    results[index] = state._overlay_process_alive(process)
                callers = [threading.Thread(target=self.checked_callback(lambda i=i: call(i)),
                                            name=f'probe-caller-{i}') for i in range(4)]
                try:
                    with patch.object(threading.Event, 'wait', observed_wait):
                        callers[0].start()
                        self.assertTrue(entered.wait(1))
                        for caller in callers[1:]:
                            caller.start()
                        self.assertTrue(all_waiting.wait(1))
                        self.assertIsNone(state._overlay_process_alive(other))
                        other.poll.assert_not_called()
                        if not stalled:
                            release.set()
                        for caller in callers:
                            caller.join(2)
                            self.assertFalse(caller.is_alive())
                        self.assertEqual(results, {i: None if stalled else True for i in range(4)})
                        self.assertEqual(len(workers), 1)
                        if stalled:
                            self.assertIsNone(state._overlay_process_alive(process))
                        release.set()
                        workers[0].join(2)
                        self.assertFalse(workers[0].is_alive())
                        self.assertTrue(state._overlay_process_alive(other))
                        other.poll.assert_called_once()
                finally:
                    release.set()
                    for caller in callers:
                        if caller.ident is not None:
                            caller.join(2)
                    for worker in workers:
                        worker.join(2)
                    for job in state._overlay_probe_jobs.values():
                        job['worker'].join(2)
                        self.assertFalse(job['worker'].is_alive())

    def test_show_hide_caps_hung_probes_across_process_generations(self):
        state = server.MeetingState()
        entered, release = threading.Event(), threading.Event()
        workers, processes = [], []

        def stalled_poll():
            workers.append(threading.current_thread())
            entered.set()
            self.checked_wait(release, 10, 'release old controller poll')
            return None  # old healthy result must never approve a new child

        with patch('overlay_bridge.subprocess.Popen') as spawn, \
                patch('os.kill') as kill, patch('os.killpg', create=True) as killpg, \
                patch.object(state, '_start_overlay_lease_locked'):
            try:
                for _ in range(5):
                    process = Mock(returncode=None, poll=self.checked_callback(stalled_poll))
                    processes.append(process)
                    display = FakeDisplay(process)
                    with patch('overlay_bridge.OverlayBridge', return_value=display):
                        started = time.monotonic()
                        with self.assertRaises(HTTPException) as rejected:
                            state.overlay_show({})
                        self.assertEqual(rejected.exception.status_code, 503)
                        self.assertLess(time.monotonic() - started, .5, 'Show cannot wait for a hung poll')
                    self.assertTrue(entered.wait(1))
                    self.assertIsNone(state.overlay)
                    self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
                    self.assertEqual(len(workers), 1, 'controller-wide cap, not one slot per child')
                    self.assertEqual(len(state._overlay_probe_jobs), 1)
                self.assertEqual(len(set(processes)), 5)
                release.set()
                workers[0].join(2)
                self.assertFalse(workers[0].is_alive())
                dead = Mock(returncode=1, poll=Mock(return_value=1))
                self.assertFalse(state._overlay_process_alive(dead), 'do not reuse old healthy result')
                dead.poll.assert_called_once()
                display = FakeDisplay(Mock(returncode=None, poll=Mock(return_value=None)))
                with patch('overlay_bridge.OverlayBridge', return_value=display):
                    self.assertTrue(state.overlay_show({})['shown'], 'released slots allow healthy Show')
                self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                for job in state._overlay_probe_jobs.values():
                    job['worker'].join(2)
                    self.assertFalse(job['worker'].is_alive())
                state.overlay_hide()
                spawn.assert_not_called()
                kill.assert_not_called()
                killpg.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'healthy real-child evidence requires POSIX wait primitives')
    def test_completed_live_probe_cannot_authorize_later_status_or_start(self):
        for action in ('status', 'start'):
            with self.subTest(action=action):
                starts = []
                state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
                    start=lambda **kwargs: starts.append(True), stop=lambda: None))
                exiting, release = threading.Event(), threading.Event()
                workers = []
                test = self
                original_thread = threading.Thread

                class ExitGatedThread(original_thread):
                    def run(self):
                        try:
                            super().run()
                        finally:
                            if self.name == 'meeting-overlay-probe':
                                exiting.set()
                                test.checked_wait(release, 10, 'release completed probe')

                def make_thread(*args, **kwargs):
                    worker = ExitGatedThread(*args, **kwargs)
                    if kwargs.get('name') == 'meeting-overlay-probe':
                        workers.append(worker)
                    return worker

                child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                other = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                state.overlay = FakeDisplay(child)
                try:
                    with patch('server.threading.Thread', side_effect=make_thread):
                        self.assertIs(state._overlay_process_alive(child), True)
                        self.assertTrue(exiting.wait(2))
                        self.assertTrue(workers[0].is_alive())
                        child.stdin.close()
                        child.wait(timeout=3)
                        self.assertEqual(child.returncode, 0)
                        started = time.monotonic()
                        if action == 'status':
                            status = state.status(touch=False)
                            self.assertIsNone(status['overlay_shown'])
                            self.assertFalse(status['overlay_stop_confirmed'])
                            self.assertTrue(status['overlay_cleanup_error'])
                        else:
                            with self.assertRaises(HTTPException) as rejected:
                                state.start(True, backend='native')
                            self.assertEqual(rejected.exception.status_code, 409)
                            self.assertFalse(state.listening)
                            self.assertEqual(starts, [])
                        self.assertLess(time.monotonic() - started, .5)
                        self.assertIsNone(state._overlay_process_alive(child))
                        self.assertIsNone(state._overlay_process_alive(other))
                        self.assertEqual(len(workers), 1)
                        self.assertEqual(len(state._overlay_probe_jobs), 1)
                        release.set()
                        workers[0].join(2)
                        self.assertFalse(workers[0].is_alive())
                        self.assertIs(state._overlay_process_alive(child), False)
                        workers[-1].join(2)
                        self.assertIs(state._overlay_process_alive(other), True)
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                    state.overlay_hide()
                    if not child.stdin.closed:
                        child.stdin.close()
                    child.wait(timeout=3)
                    other.stdin.close()
                    other.wait(timeout=3)

    def test_completed_probe_holds_slot_until_actual_worker_exit(self):
        state = server.MeetingState()
        exiting, release = threading.Event(), threading.Event()
        workers = []
        test = self
        original_thread = threading.Thread

        class ExitGatedThread(original_thread):
            def run(self):
                try:
                    super().run()
                finally:
                    if self.name == 'meeting-overlay-probe':
                        exiting.set()
                        test.checked_wait(release, 5, 'release completed probe thread')

        def make_thread(*args, **kwargs):
            worker = ExitGatedThread(*args, **kwargs)
            workers.append(worker)
            return worker

        old = Mock(poll=Mock(return_value=None))
        new = Mock(poll=Mock(return_value=1))
        with patch('server.threading.Thread', side_effect=make_thread):
            try:
                self.assertTrue(state._overlay_process_alive(old))
                self.assertTrue(exiting.wait(1))
                self.assertTrue(workers[0].is_alive())
                self.assertIsNone(state._overlay_process_alive(new))
                new.poll.assert_not_called()
                self.assertEqual(len(workers), 1)
                release.set()
                workers[0].join(2)
                self.assertFalse(workers[0].is_alive())
                self.assertFalse(state._overlay_process_alive(new))
                new.poll.assert_called_once()
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
