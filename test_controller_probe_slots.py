"""Controller probe lifetime regressions with inert children and fakes."""
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

import server
from thread_test_support import CheckedThreads


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
