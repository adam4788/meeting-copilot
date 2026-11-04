"""Real inert children and OS pipes; no overlay UI, capture, or providers."""
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

from overlay_bridge import OverlayBridge
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import server
from thread_test_support import CheckedThreads


@contextmanager
def real_display(show=True, child_code=None):
    process = subprocess.Popen(
        [sys.executable, '-u', '-c', child_code or 'import time; print("ready", flush=True); time.sleep(60)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)
    real_kill = process.kill
    bridge = OverlayBridge()
    ready = threading.Event()
    bridge.on_click_line = lambda line: (ready.set() or False)
    def start():
        bridge.process = process
        return process
    try:
        if show:
            with patch.object(bridge, 'start_helper', side_effect=start):
                assert bridge.show_overlay()['shown']
            assert ready.wait(2), 'real pipe reader did not consume ready line'
        yield bridge, process
    finally:
        if process.poll() is None:
            real_kill()
        process.wait(timeout=2)
        bridge.stop()
        for worker in bridge.threads:
            worker.join(2)
            assert not worker.is_alive(), worker.name
        bridge.stop()
        for worker in list(bridge._signal_jobs.values()) + ([bridge._cleanup_job['worker']] if bridge._cleanup_job else []):
            worker.join(2)
            assert not worker.is_alive(), worker.name
        assert process.poll() is not None
        for stream in (process.stdout, process.stdin):
            if stream and not stream.closed:
                stream.close()


class OverlayTeardownTests(CheckedThreads):
    def finish_denied_cleanup(self, bridge, state):
        # A bounded API return may precede worker completion. Drain the denied
        # attempt at its explicit gate before restoring real signals, so the
        # next retry tests a fresh attempt rather than reusing old evidence.
        outer = state._overlay_cleanup_job
        if outer is not None:
            self.assertTrue(outer['done'].wait(2))
        job = bridge._cleanup_job
        if job is not None:
            self.assertTrue(job['done'].wait(2))
            job['worker'].join(2)
            self.assertFalse(job['worker'].is_alive())

    def bounded(self, fn):
        result = []
        worker = threading.Thread(target=self.checked_callback(lambda: result.append(fn())), daemon=True)
        worker.start()
        worker.join(1.5)
        self.assertFalse(worker.is_alive(), 'overlay teardown exceeded bounded deadline')
        return result[0]

    def test_autonomous_pipe_failure_blocks_start_until_confirmed_cleanup(self):
        from fastapi import HTTPException
        from types import SimpleNamespace
        starts = []
        audio = SimpleNamespace(start=lambda **kw: starts.append(kw), stop=lambda: None)
        state = server.MeetingState(audio_factory=lambda: audio)
        with patch.object(state, 'watchdog'), real_display() as (bridge, process):
            state.overlay = bridge
            try:
                with patch.object(process, 'terminate', side_effect=PermissionError('denied')), \
                        patch.object(process, 'kill', side_effect=PermissionError('denied')), \
                        patch.object(bridge, '_write_frame', side_effect=BrokenPipeError('closed pipe')):
                    # The writer, not a controller Hide, discovers the failure.
                    bridge.message = 'force a distinct pipe frame'
                    bridge.push()
                    deadline = time.monotonic() + 2
                    while bridge.cleanup_error is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(bridge.stopping)
                    self.assertIsNotNone(bridge.cleanup_error)
                    self.assertIsNone(state.pending_overlay)
                    with self.assertRaises(HTTPException) as denied:
                        state.start(True, 'native')
                    self.assertEqual(denied.exception.status_code, 409)
                    self.assertEqual(starts, [])
                    self.assertIs(state.pending_overlay, bridge)
                    self.assertIsNone(state.status(touch=False)['overlay_shown'])
                    self.finish_denied_cleanup(bridge, state)
                self.assertTrue(self.bounded(bridge.hide_overlay)['stop_confirmed'])
                self.assertTrue(state.start(True, 'native')['listening'])
                self.assertIsNone(state.pending_overlay)
                self.assertIsNone(state.overlay_cleanup_error)
                self.assertEqual(len(starts), 1)
            finally:
                state.pause(retain_tail=False)

    def test_confirmed_child_exit_allows_fresh_show_without_extra_hide(self):
        for reaped in (False, True):
            with self.subTest(reaped=reaped), real_display(show=False, child_code='pass') as (old, child), \
                    real_display(show=False) as (fresh, replacement):
                # Real orderly exit (code 0), not a fake poll result.
                child.wait(timeout=2)
                self.assertEqual(child.returncode, 0)
                old.process = child
                old.invalidate()
                if reaped:
                    self.assertTrue(old.stop())
                state = server.MeetingState()
                state.overlay = old
                def launch():
                    fresh.process = replacement
                    return replacement
                try:
                    with patch('overlay_bridge.OverlayBridge', return_value=fresh), \
                            patch.object(fresh, 'start_helper', side_effect=launch), \
                            patch.object(state, '_start_overlay_lease_locked'):
                        try:
                            result = state.overlay_show({})
                        except HTTPException as exc:
                            self.fail(f'confirmed exit must allow fresh Show, got {exc}')
                    self.assertTrue(result['shown'])
                    self.assertFalse(result['reused'])
                    self.assertIs(state.overlay, fresh)
                    self.assertIsNone(state.pending_overlay)
                    self.assertIsNone(state.overlay_cleanup_error)
                finally:
                    old.stop()
                    if old._cleanup_job:
                        old._cleanup_job['worker'].join(2)
                    state.overlay_hide()

    def test_stalled_process_poll_cannot_hold_state_lock_or_block_pause(self):
        from types import SimpleNamespace
        stopped = threading.Event()
        audio = SimpleNamespace(start=lambda **kw: None, stop=stopped.set)
        state = server.MeetingState(audio_factory=lambda: audio)
        with patch.object(state, 'watchdog'), real_display() as (bridge, process):
            state.start(True, 'native')
            state.overlay = bridge
            entered, release = threading.Event(), threading.Event()
            real_poll = process.poll
            def stalled_poll():
                entered.set()
                self.checked_wait(release, 5, 'release status process probe')
                return real_poll()
            status_worker = threading.Thread(target=self.checked_callback(lambda: state.status(touch=False)), daemon=True)
            pause_worker = threading.Thread(target=self.checked_callback(lambda: state.pause(retain_tail=False)), daemon=True)
            try:
                with patch.object(process, 'poll', side_effect=stalled_poll) as probe:
                    status_worker.start()
                    self.assertTrue(entered.wait(1))
                    pause_worker.start()
                    pause_worker.join(.7)
                    self.assertFalse(pause_worker.is_alive(), 'Pause blocked behind status process.poll')
                    self.assertTrue(stopped.is_set())
                    status_worker.join(.7)
                    self.assertFalse(status_worker.is_alive(), 'status process inspection must be bounded')
                    for _ in range(3):
                        self.assertIsNone(state.status(touch=False)['overlay_shown'])
                    self.assertEqual(probe.call_count, 1, 'reuse the in-flight process inspection')
                    def denied_start():
                        with self.assertRaises(HTTPException) as denied:
                            state.start(True, 'native')
                        return denied.exception.status_code
                    self.assertEqual(self.bounded(denied_start), 409)
                    self.assertIs(state.pending_overlay, bridge)
                    self.assertEqual(probe.call_count, 1)
            finally:
                release.set()
                status_worker.join(2)
                if pause_worker.ident is not None:
                    pause_worker.join(2)
                state.pause(retain_tail=False)

    def test_clear_retains_pending_display_error_and_blocks_restart_until_retry(self):
        class Audio:
            is_running = False
            starts = 0
            def start(self, **callbacks):
                self.is_running = True
                self.starts += 1
                self.callbacks = callbacks
            def stop(self):
                self.is_running = False
        audio = Audio()
        state = server.MeetingState(audio_factory=lambda: audio)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client, patch.object(state, 'watchdog'), real_display() as (bridge, process):
            state.start(True, 'native')
            state.overlay = bridge
            with patch.object(process, 'terminate', side_effect=PermissionError('denied')), \
                    patch.object(process, 'kill', side_effect=PermissionError('denied')):
                for _ in range(2):
                    reply = self.bounded(lambda: client.post('/clear').json())
                    self.assertFalse(audio.is_running)
                    self.assertFalse(reply['listening'])
                    self.assertTrue(reply['stop_confirmed'], 'audio stop is independent')
                    self.assertFalse(reply['overlay_stop_confirmed'])
                    self.assertIsNone(reply['overlay_shown'])
                    self.assertIn('not confirmed', reply['overlay_cleanup_error'])
                    self.assertEqual(client.get('/status').json()['overlay_cleanup_error'], reply['overlay_cleanup_error'])
                    self.assertIs(state.pending_overlay, bridge)
                    self.assertIsNone(state.overlay)
                    self.assertIs(bridge.process, process)
                    self.assertFalse(bridge.update({'segments': [{'text': 'stale'}], 'listening': True}))
                    audio.callbacks['on_segment']({'text': 'late audio'})
                    self.assertEqual(list(state.segments), [])
                    self.assertEqual(client.post('/start', json={'consent': True, 'backend': 'native'}).status_code, 409)
                    from fastapi import HTTPException
                    with self.assertRaises(HTTPException) as denied:
                        state.overlay_show({})
                    self.assertEqual(denied.exception.status_code, 503)
                    self.assertEqual(audio.starts, 1)
                acquired = threading.Event()
                def take_lock():
                    with state.lifecycle_lock:
                        acquired.set()
                probe = threading.Thread(target=take_lock)
                probe.start(); probe.join(1)
                self.assertTrue(acquired.is_set())
            reply = self.bounded(lambda: client.post('/clear').json())
            self.assertTrue(reply['overlay_stop_confirmed'])
            self.assertFalse(reply['overlay_shown'])
            self.assertIsNone(reply['overlay_cleanup_error'])
            self.assertIsNone(state.pending_overlay)
            self.assertIsNotNone(process.poll())
            self.assertFalse(reply['listening'], 'cleanup retry must not resume old capture')
            self.assertEqual(list(state.segments), [])
            self.assertEqual(client.post('/start', json={'consent': True, 'backend': 'native'}).status_code, 200)
            state.pause(retain_tail=False)

    def test_hung_cleanup_does_not_block_capture_stop_watchdog_or_lifecycle_lock(self):
        from types import SimpleNamespace
        for action in ('clear', 'release', 'watchdog'):
            with self.subTest(action=action), real_display() as (bridge, process):
                stopped = threading.Event()
                audio = SimpleNamespace(start=lambda **kw: None, stop=stopped.set)
                state = server.MeetingState(audio_factory=lambda: audio)
                pane = state.overlay_claim()['pane']
                with patch.object(state, 'watchdog'):
                    state.start(True, 'native', pane=pane, action=1)
                state.overlay = bridge
                entered, release = threading.Event(), threading.Event()
                real_hide = bridge.hide_overlay
                def hung_hide():
                    entered.set()
                    self.checked_wait(release, 5, 'release hung cleanup')
                    return real_hide()
                app = FastAPI()
                app.include_router(server.make_router(state))
                try:
                    with TestClient(app) as client, patch.object(bridge, 'hide_overlay', side_effect=hung_hide) as hide:
                        if action == 'clear':
                            run = lambda: client.post('/clear', json={'pane': pane, 'action': 2}).json()
                        elif action == 'release':
                            run = lambda: state.overlay_release(pane=pane, action=2)
                        else:
                            state.pane_seen = time.monotonic() - 60
                            def run():
                                clock = SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=lambda _: None)
                                with patch.object(server, 'time', clock):
                                    return state.watchdog(state.capture_generation)
                        self.bounded(run)
                        self.assertTrue(entered.is_set())
                        self.assertTrue(stopped.is_set(), 'overlay cleanup cannot prevent capture stop')
                        status = state.status(touch=False)
                        self.assertTrue(status['stop_confirmed'])
                        self.assertFalse(status['overlay_stop_confirmed'])
                        self.assertIsNone(status['overlay_shown'])
                        self.assertIs(state.pending_overlay, bridge)
                        # A second caller must acquire lifecycle_lock promptly,
                        # and must reuse—not multiply—the hung cleanup job.
                        self.assertFalse(self.bounded(state.overlay_hide)['overlay_stop_confirmed'])
                        self.assertEqual(hide.call_count, 1)
                        self.assertIsNone(process.poll())
                        release.set()
                        self.assertTrue(state._overlay_cleanup_job['done'].wait(2))
                        # A late result is evidence, never an asynchronous state
                        # commit. The exact current retry publishes success.
                        self.assertIs(state.pending_overlay, bridge)
                        result = self.bounded(state.overlay_hide)
                        self.assertTrue(result['overlay_stop_confirmed'])
                        self.assertIsNone(state.pending_overlay)
                        self.assertFalse(state.listening)
                finally:
                    release.set()
                    if state._overlay_cleanup_job:
                        self.assertTrue(state._overlay_cleanup_job['done'].wait(2))
                    state.pause(retain_tail=False)

    def test_failed_launch_retains_process_and_never_reports_hidden(self):
        from fastapi import HTTPException
        with real_display(show=False) as (bridge, process):
            state = server.MeetingState()
            def start():
                bridge.process = process
                return process
            with patch('overlay_bridge.OverlayBridge', return_value=bridge), \
                    patch.object(bridge, 'start_helper', side_effect=start), \
                    patch.object(bridge, '_start_pusher_locked', side_effect=RuntimeError('startup fault')), \
                    patch.object(process, 'terminate', side_effect=PermissionError('denied')), \
                    patch.object(process, 'kill', side_effect=PermissionError('denied')):
                with self.assertRaises(HTTPException):
                    state.overlay_show({})
                self.assertIs(state.pending_overlay, bridge)
                self.assertIs(bridge.process, process)
                self.assertIsNone(state.status(touch=False)['overlay_shown'])
                self.assertFalse(state.status(touch=False)['overlay_stop_confirmed'])
            self.assertTrue(self.bounded(state.overlay_hide)['overlay_stop_confirmed'])

    def test_hung_terminate_does_not_prevent_real_kill(self):
        with real_display() as (bridge, process):
            entered, release = threading.Event(), threading.Event()
            def hung_term():
                entered.set()
                self.checked_wait(release, 5, 'release TERM')
            try:
                with patch.object(process, 'terminate', side_effect=hung_term), \
                        patch.object(process, 'wait', side_effect=AssertionError('must poll, never blocking wait')):
                    result = self.bounded(bridge.hide_overlay)
                    self.assertTrue(entered.is_set())
                    self.assertTrue(result['stop_confirmed'])
                    self.assertIsNotNone(process.poll())
                    self.assertIsNone(bridge.process)
            finally:
                release.set()

    def test_new_pane_claim_keeps_old_unconfirmed_display_for_retry(self):
        with real_display() as (bridge, process):
            state = server.MeetingState()
            state.overlay = bridge
            with patch.object(process, 'terminate', side_effect=PermissionError('denied')), \
                    patch.object(process, 'kill', side_effect=PermissionError('denied')):
                result = self.bounded(state.overlay_claim)
                self.assertTrue(result['pane'])
                self.assertIs(state.pending_overlay, bridge)
                self.assertIsNone(state.status(touch=False)['overlay_shown'])
                self.assertFalse(state.status(touch=False)['overlay_stop_confirmed'])
                self.assertIsNone(state.overlay)
            self.assertTrue(self.bounded(state.overlay_hide)['overlay_stop_confirmed'])

    def test_idle_overlay_lease_retains_unconfirmed_display_for_retry(self):
        with real_display() as (bridge, process):
            state = server.MeetingState()
            state.overlay = bridge
            state.pane_seen = time.monotonic() - 60
            seq = state.overlay_lease_seq
            with patch.object(process, 'terminate', side_effect=PermissionError('denied')), \
                    patch.object(process, 'kill', side_effect=PermissionError('denied')):
                self.assertTrue(self.bounded(lambda: state._overlay_hide_if_expired(bridge, seq)))
                self.assertIs(state.pending_overlay, bridge)
                status = state.status(touch=False)
                self.assertFalse(status['overlay_stop_confirmed'])
                self.assertIsNone(status['overlay_shown'])
                self.assertIsNone(state.overlay)
                self.assertIs(bridge.process, process)
                self.finish_denied_cleanup(bridge, state)
            self.assertTrue(self.bounded(state.overlay_hide)['overlay_stop_confirmed'])

    def test_retired_bridge_cannot_be_reused_by_controller(self):
        from fastapi import HTTPException
        with real_display() as (bridge, process):
            state = server.MeetingState()
            state.overlay = bridge
            with patch.object(process, 'terminate', side_effect=PermissionError('denied')), \
                    patch.object(process, 'kill', side_effect=PermissionError('denied')):
                self.assertFalse(self.bounded(bridge.hide_overlay)['stop_confirmed'])
                self.assertIsNone(state.status(touch=False)['overlay_shown'])
                with self.assertRaises(HTTPException):
                    state.overlay_show({})
                self.assertIs(state.pending_overlay, bridge)
                self.assertIsNone(state.overlay)
                self.assertFalse(self.bounded(state.overlay_hide)['overlay_stop_confirmed'])
            self.assertTrue(self.bounded(state.overlay_hide)['overlay_stop_confirmed'])

    def test_hung_signals_are_bounded_and_retries_do_not_spawn_more_signal_calls(self):
        with real_display() as (bridge, process):
            release = threading.Event()
            entered = {name: threading.Event() for name in ('terminate', 'kill')}
            def hung(name):
                entered[name].set()
                release.wait(5)
                raise PermissionError('released denied signal')
            try:
                with patch.object(process, 'terminate', side_effect=lambda: hung('terminate')) as term, \
                        patch.object(process, 'kill', side_effect=lambda: hung('kill')) as kill:
                    for _ in range(3):
                        self.assertFalse(self.bounded(bridge.hide_overlay)['stop_confirmed'])
                        self.assertTrue(bridge._cleanup_job['done'].wait(1))
                    self.assertTrue(all(event.is_set() for event in entered.values()))
                    self.assertEqual(term.call_count, 1)
                    self.assertEqual(kill.call_count, 1)
                    self.assertIs(bridge.process, process)
                    self.assertIsNotNone(process.stdout)
                    self.assertFalse(process.stdout.closed)
                    release.set()
                    for worker in bridge._signal_jobs.values():
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                self.assertTrue(self.bounded(bridge.hide_overlay)['stop_confirmed'])
            finally:
                release.set()

    def test_denied_signals_keep_real_pipe_and_handle_until_successful_retry(self):
        with real_display() as (bridge, process):
            with patch.object(process, 'terminate', side_effect=PermissionError('denied')), \
                    patch.object(process, 'kill', side_effect=PermissionError('denied')), \
                    patch.object(process.stdout, 'close', wraps=process.stdout.close) as close:
                for _ in range(2):
                    result = self.bounded(bridge.hide_overlay)
                    close.assert_not_called()
                    self.assertTrue(bridge._reader.is_alive())
                    self.assertFalse(result['stop_confirmed'])
                    self.assertIsNone(result['shown'], 'unknown visibility is not hidden')
                    self.assertIs(bridge.process, process)
                    self.assertIsNone(process.poll())
                    self.assertIsNotNone(process.stdout)
                    self.assertFalse(process.stdout.closed)
                    self.assertTrue(bridge.stopping)
                    self.assertFalse(bridge.update({'segments': [{'text': 'stale'}], 'listening': True}))
                    self.assertFalse(bridge.show_overlay()['shown'])
            result = self.bounded(bridge.hide_overlay)
            self.assertTrue(result['stop_confirmed'])
            self.assertFalse(result['shown'])
            self.assertIsNotNone(process.poll())
            self.assertIsNone(bridge.process)
