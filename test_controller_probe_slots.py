"""Controller probe lifetime regressions with fake processes only."""
import threading
import time
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
