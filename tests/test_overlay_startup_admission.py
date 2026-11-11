"""Startup/admission races using inert children and real OS pipes only."""
from tests.thread_test_support import mock_capture_preflight
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import overlay_bridge
import server
from tests.thread_test_support import CheckedThreads


class StartupAdmissionTests(CheckedThreads):
    def setUp(self):
        super().setUp()
        mock_capture_preflight(self)

    def require_supported_helper_platform(self):
        # These tests exercise real POSIX helper launch, not injected launch.
        # Keep the controller-only tests running in the Windows CI matrix.
        if overlay_bridge.sys.platform not in ('darwin', 'linux'):
            self.skipTest('real overlay helper launch requires macOS or Linux')

    def test_windows_helper_is_unsupported_without_spawning(self):
        bridge = overlay_bridge.OverlayBridge(helper=sys.executable)
        state = server.MeetingState()
        with patch.object(overlay_bridge, 'sys', SimpleNamespace(platform='win32')), \
                patch.object(overlay_bridge.subprocess, 'Popen') as spawn, \
                patch.object(overlay_bridge, 'OverlayBridge', return_value=bridge):
            self.assertFalse(bridge.helper_status()['available'])
            self.assertIn('not supported', bridge.helper_status()['error'])
            self.assertIsNone(bridge.start_helper())
            self.assertFalse(bridge.show_overlay()['shown'])
            with self.assertRaises(HTTPException) as rejected:
                state.overlay_show({})
            self.assertEqual(rejected.exception.status_code, 503)
            self.assertIn('not supported', rejected.exception.detail)
            spawn.assert_not_called()
            self.assertIsNone(bridge.process)
            self.assertIsNone(state.overlay)
            self.assertIsNone(state.pending_overlay)
            self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])

    def test_show_rejects_helper_exited_before_startup_commit(self):
        from tests.test_overlay_teardown import real_display
        with real_display(show=False) as (bridge, process):
            state = server.MeetingState()
            def start():
                bridge.process = process
                return process
            real_show = bridge.show_overlay
            def show_then_exit():
                result = real_show()
                process.terminate()
                process.wait(timeout=3)
                return result
            with patch('overlay_bridge.OverlayBridge', return_value=bridge), \
                    patch.object(bridge, 'start_helper', side_effect=start), \
                    patch.object(bridge, 'show_overlay', side_effect=show_then_exit), \
                    patch.object(state, '_start_overlay_lease_locked') as lease:
                with self.assertRaises(HTTPException) as rejected:
                    state.overlay_show({})
                self.assertEqual(rejected.exception.status_code, 503)
                self.assertIsNone(state.overlay)
                lease.assert_not_called()
            state.overlay_hide()

    def test_direct_show_does_not_report_cached_dead_launch(self):
        from tests.test_overlay_teardown import real_display
        with real_display(show=False) as (bridge, process):
            process.terminate()
            process.wait(timeout=3)
            def start():
                bridge.process = process
                return process
            with patch.object(bridge, 'start_helper', side_effect=start):
                self.assertFalse(bridge.show_overlay()['shown'])

    def test_reused_show_rejects_observed_exit_during_scope_update(self):
        from tests.test_overlay_teardown import real_display
        with real_display() as (bridge, process):
            state = server.MeetingState()
            state.overlay = bridge
            real_scope = bridge.set_scope
            def scope(value):
                result = real_scope(value)
                process.terminate()
                process.wait(timeout=3)
                return result
            with patch.object(bridge, 'set_scope', side_effect=scope), \
                    patch.object(state, '_start_overlay_lease_locked') as lease:
                with self.assertRaises(HTTPException) as rejected:
                    state.overlay_show({})
                self.assertEqual(rejected.exception.status_code, 503)
                self.assertIsNone(state.overlay)
                lease.assert_not_called()
            state.overlay_hide()

    def test_startup_liveness_probe_is_bounded_and_off_lifecycle_lock(self):
        from tests.test_overlay_teardown import real_display
        with real_display(show=False) as (bridge, process):
            state = server.MeetingState()
            entered, release = threading.Event(), threading.Event()
            errors = []
            real_poll = process.poll
            def poll():
                entered.set()
                self.checked_wait(release, 5, 'release startup liveness probe')
                return real_poll()
            def start():
                bridge.process = process
                return process
            def show():
                try:
                    state.overlay_show({})
                except HTTPException as exc:
                    errors.append(exc.status_code)
            worker = threading.Thread(target=self.checked_callback(show))
            try:
                with patch('overlay_bridge.OverlayBridge', return_value=bridge), \
                        patch.object(bridge, 'start_helper', side_effect=start), \
                        patch.object(process, 'poll', side_effect=poll):
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(state.lifecycle_lock.acquire(timeout=.2))
                    state.lifecycle_lock.release()
                    worker.join(1.5)
                    self.assertFalse(worker.is_alive(), 'Show blocked on process.poll')
                    self.assertEqual(errors, [503])
                    self.assertIsNone(state.overlay)
                    self.assertIs(state.pending_overlay, bridge)
            finally:
                release.set()
                worker.join(3)
                for job in state._overlay_probe_jobs.values():
                    self.assertTrue(job['done'].wait(2))
                if bridge._cleanup_job:
                    self.assertTrue(bridge._cleanup_job['done'].wait(2))
                if state._overlay_cleanup_job:
                    self.assertTrue(state._overlay_cleanup_job['done'].wait(2))
                state.overlay_hide()

    def test_retirement_between_reconcile_and_admission_denies_start_and_show(self):
        from tests.test_overlay_teardown import real_display
        for action in ('start', 'show'):
            with self.subTest(action=action), real_display(show=False, child_code=(
                    'import os,time; os.close(0); print("ready",flush=True); time.sleep(60)')) as (bridge, child):
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                bridge.process = child
                starts, results, errors = [], [], []
                state = server.MeetingState(audio_factory=lambda starts=starts: SimpleNamespace(
                    start=lambda **kw: starts.append(True), stop=lambda: None))
                state.overlay = bridge
                entered, release = threading.Event(), threading.Event()
                reconcile = state._reconcile_overlay_admission
                def gated(*args, reconcile=reconcile, entered=entered, release=release, **kwargs):
                    result = reconcile(*args, **kwargs)
                    entered.set()
                    self.checked_wait(release, 5, 'release admission')
                    return result
                def run(state=state, action=action, results=results, errors=errors):
                    try:
                        results.append(state.start(True, 'native') if action == 'start' else state.overlay_show({}))
                    except HTTPException as exc:
                        errors.append(exc.status_code)
                worker = threading.Thread(target=self.checked_callback(run), daemon=True)
                try:
                    with patch.object(state, 'watchdog'), \
                            patch.object(state, '_reconcile_overlay_admission', side_effect=gated), \
                            patch.object(child, 'terminate', side_effect=PermissionError('denied')), \
                            patch.object(child, 'kill', side_effect=PermissionError('denied')):
                        worker.start()
                        self.assertTrue(entered.wait(2))
                        # Actual EPIPE from the child's closed fd, not a mocked writer.
                        self.assertFalse(bridge._emit({'snapshot_id': bridge.snapshot_id, 'message': 'race'}))
                        self.assertTrue(bridge.stopping)
                        self.assertIsNotNone(bridge.cleanup_error)
                        release.set()
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(results, [])
                        self.assertEqual(errors, [409 if action == 'start' else 503])
                        self.assertEqual(starts, [])
                        self.assertIs(state.pending_overlay, bridge)
                        self.assertIsNone(state.overlay)
                        self.assertIsNone(child.poll())
                        self.assertIsNone(state.status(touch=False)['overlay_shown'])
                finally:
                    release.set()
                    worker.join(3)
                    state.pause(retain_tail=False)
                    state.overlay_hide()

    def test_clean_exit_between_reconcile_and_admission_does_not_block(self):
        import tempfile
        from pathlib import Path

        from tests.test_overlay_teardown import real_display
        for action in ('start', 'show'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp, \
                    real_display(show=False, child_code=(
                        'import os,time; from pathlib import Path; os.close(0); '
                        'print("ready",flush=True)\n'
                        f'while not Path({str(Path(tmp) / "exit")!r}).exists(): time.sleep(.01)\n')) as (bridge, child), \
                    real_display(show=False) as (fresh, fresh_child):
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                bridge.process = child
                state = server.MeetingState(audio_factory=lambda: SimpleNamespace(start=lambda **kw: None, stop=lambda: None))
                state.overlay = bridge
                entered, release = threading.Event(), threading.Event()
                results, errors = [], []
                original = state._reconcile_overlay_admission
                def gated(*args, original=original, entered=entered, release=release, **kwargs):
                    result = original(*args, **kwargs)
                    entered.set()
                    self.checked_wait(release, 3, 'clean exit gate')
                    return result
                def start_fresh():
                    fresh.process = fresh_child
                    return fresh_child
                def run(state=state, action=action, results=results, errors=errors):
                    try:
                        results.append(state.start(True, 'native') if action == 'start' else state.overlay_show({}))
                    except HTTPException as exc:
                        errors.append(exc.status_code)
                worker = threading.Thread(target=self.checked_callback(run), daemon=True)
                try:
                    with patch.object(state, 'watchdog'), \
                            patch.object(state, '_reconcile_overlay_admission', side_effect=gated), \
                            patch.object(overlay_bridge, 'OverlayBridge', return_value=fresh), \
                            patch.object(fresh, 'start_helper', side_effect=start_fresh):
                        worker.start()
                        self.assertTrue(entered.wait(2))
                        (Path(tmp) / 'exit').write_text('exit')
                        self.assertEqual(child.wait(timeout=2), 0)
                        bridge._broken_pipe_shutdown(child)
                        self.assertIsNone(bridge.process)
                        release.set()
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(errors, [], 'confirmed clean exit must not require Hide')
                        self.assertTrue(results[0]['listening' if action == 'start' else 'shown'])
                finally:
                    release.set()
                    worker.join(3)
                    state.pause(retain_tail=False)
                    state.overlay_hide()

    def test_delayed_spawn_cannot_block_watchdog_or_rebind_new_owner(self):
        self.require_supported_helper_platform()
        from tests.test_overlay_teardown import real_display
        spawned, release = threading.Event(), threading.Event()
        stopped = threading.Event()
        state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
            start=lambda **kw: None, stop=stopped.set))
        pane = state.overlay_claim()['pane']
        with patch.object(state, 'watchdog'):
            state.start(True, 'native', pane=pane, action=1)
        old = overlay_bridge.OverlayBridge(helper=sys.executable)
        old.helper_args = ['-u', '-c', 'print("meeting-overlay/2",flush=True)']
        original = subprocess.Popen
        children, errors = [], []
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            spawned.set()
            self.checked_wait(release, 6, 'release delayed Popen return')
            return child
        def show():
            try:
                state.overlay_show({}, pane=pane, action=2)
            except HTTPException as exc:
                errors.append(exc.status_code)
        worker = threading.Thread(target=self.checked_callback(show), daemon=True)
        try:
            with patch.object(overlay_bridge, 'OverlayBridge', return_value=old), \
                    patch.object(subprocess, 'Popen', side_effect=spawn):
                worker.start()
                self.assertTrue(spawned.wait(2))
                job = state._overlay_startup_job
                self.assertIs(state.pending_overlay, old)
                # Popen hasn't returned a handle yet. Absence is NOT confirmed.
                self.assertFalse(state.overlay_hide()['overlay_stop_confirmed'])
                state.pane_seen = server.time.monotonic() - 60
                state.watchdog(state.capture_generation)
                self.assertTrue(stopped.is_set())
                new_pane = state.overlay_claim()['pane']
                self.assertNotEqual(new_pane, pane)
                self.assertIs(state.pending_overlay, old)
                # Serialize the old caller behind a new, successful owner commit.
                with state.lifecycle_lock:
                    release.set()
                    self.assertTrue(job['done'].wait(2))
                    self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
                    with real_display(show=False) as (fresh, child):
                        def start():
                            fresh.process = child
                            return child
                        with patch.object(overlay_bridge, 'OverlayBridge', return_value=fresh), \
                                patch.object(fresh, 'start_helper', side_effect=start):
                            result = state.overlay_show({}, pane=new_pane, action=1)
                        self.assertTrue(result['shown'])
                        owner = state.overlay_owner
                        # Release outer lock while the fresh real child remains alive.
                        state.lifecycle_lock.release()
                        try:
                            worker.join(2)
                            self.assertFalse(worker.is_alive())
                            self.assertEqual(errors, [503])
                            self.assertIs(state.overlay, fresh)
                            self.assertEqual(state.overlay_owner, owner)
                            self.assertIsNone(child.poll())
                        finally:
                            state.lifecycle_lock.acquire()
                        self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
        finally:
            release.set()
            worker.join(4)
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
                for stream in (child.stdin, child.stdout):
                    if stream and not stream.closed:
                        stream.close()
            state.overlay_hide()
            state.pause(retain_tail=False)
            self.assertFalse(worker.is_alive())

    def test_startup_retirement_before_show_commit_retains_real_child(self):
        from tests.test_overlay_teardown import real_display
        with real_display(show=False, child_code=(
                'import os,time; os.close(0); print("ready",flush=True); time.sleep(60)')) as (bridge, child):
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            state = server.MeetingState()
            def start():
                bridge.process = child
                return child
            original = bridge.show_overlay
            def fail_before_return():
                result = original()
                self.assertTrue(result['shown'])
                self.assertFalse(bridge._emit({'snapshot_id': bridge.snapshot_id, 'message': 'failure before commit'}))
                self.assertTrue(bridge.stopping)
                return result
            try:
                with patch.object(overlay_bridge, 'OverlayBridge', return_value=bridge), \
                        patch.object(bridge, 'start_helper', side_effect=start), \
                        patch.object(bridge, '_start_pusher_locked'), patch.object(bridge, '_start_poller_locked'), \
                        patch.object(bridge, 'show_overlay', side_effect=self.checked_callback(fail_before_return)), \
                        patch.object(child, 'terminate', side_effect=PermissionError('denied')), \
                        patch.object(child, 'kill', side_effect=PermissionError('denied')):
                    with self.assertRaises(HTTPException) as rejected:
                        state.overlay_show({})
                    self.assertEqual(rejected.exception.status_code, 503)
                    self.assertIsNone(state.overlay)
                    self.assertIs(state.pending_overlay, bridge)
                    self.assertIs(bridge.process, child)
                    self.assertFalse(state.status(touch=False)['overlay_stop_confirmed'])
            finally:
                state.overlay_hide()

    def test_valid_real_protocol_start_show_reuse_and_clean_exit(self):
        self.require_supported_helper_platform()
        state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
            start=lambda **kw: None, stop=lambda: None))
        bridge = overlay_bridge.OverlayBridge(helper=sys.executable)
        bridge.helper_args = ['-u', '-c',
            'import sys; print("meeting-overlay/2",flush=True) if "--protocol-version" in sys.argv else list(sys.stdin)']
        try:
            with patch.object(overlay_bridge, 'OverlayBridge', return_value=bridge), patch.object(state, 'watchdog'):
                self.assertTrue(state.overlay_show({})['shown'])
                child = bridge.process
                self.assertTrue(state.start(True, 'native')['listening'])
                self.assertTrue(state.overlay_show({})['reused'])
                self.assertIs(bridge.process, child)
                state.pause(retain_tail=False)
                self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
                self.assertIsNotNone(child.poll())
                self.assertTrue(state.status(touch=False)['overlay_stop_confirmed'])
        finally:
            state.pause(retain_tail=False)
            state.overlay_hide()

    def test_protocol_timeout_retains_child_without_blocking_pause(self):
        self.require_supported_helper_platform()
        stopped, spawned, denied = threading.Event(), threading.Event(), threading.Event()
        state = server.MeetingState(audio_factory=lambda: SimpleNamespace(
            start=lambda **kw: None, stop=stopped.set))
        original_popen = subprocess.Popen
        bridge = overlay_bridge.OverlayBridge(helper=sys.executable)
        bridge.helper_args = ['-u', '-c', 'import time; time.sleep(60)']
        children, errors = [], []

        def spawn(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append((child, child.kill))
            def deny():
                denied.set()
                raise PermissionError('injected denied termination')
            child.kill = child.terminate = deny
            spawned.set()
            return child

        def show():
            try:
                state.overlay_show({})
            except HTTPException as exc:
                errors.append(exc)

        worker = threading.Thread(target=self.checked_callback(show), daemon=True)
        pause = threading.Thread(target=self.checked_callback(lambda: state.pause(retain_tail=False)), daemon=True)
        with patch.object(state, 'watchdog'), patch.object(overlay_bridge, 'OverlayBridge', return_value=bridge), \
                patch.object(subprocess, 'Popen', side_effect=spawn):
            state.start(True, 'native')
            try:
                worker.start()
                self.assertTrue(spawned.wait(2))
                pause.start()
                pause.join(.7)
                self.assertFalse(pause.is_alive(), 'protocol probe blocks Pause')
                self.assertTrue(stopped.is_set())
                self.assertTrue(denied.wait(4), 'real protocol timeout did not attempt cleanup')
                worker.join(2)
                self.assertFalse(worker.is_alive(), 'denied cleanup stranded Show')
                self.assertEqual([exc.status_code for exc in errors], [503])
                child = children[0][0]
                self.assertIsNone(child.poll())
                self.assertIs(bridge.process, child, 'unconfirmed protocol child was orphaned')
                self.assertIs(state.pending_overlay, bridge)
                status = state.status(touch=False)
                self.assertFalse(status['overlay_stop_confirmed'])
                self.assertIsNone(status['overlay_shown'])
                with self.assertRaises(HTTPException):
                    state.start(True, 'native')
                with self.assertRaises(HTTPException):
                    state.overlay_show({})
                self.assertEqual(len(children), 1)
            finally:
                for child, kill in children:
                    if child.poll() is None:
                        kill()
                    child.wait(timeout=2)
                startup = state._overlay_startup_job
                if startup is not None:
                    self.assertTrue(startup['done'].wait(3), 'startup worker leaked after emergency kill')
                    startup['worker'].join(1)
                worker.join(4)
                if pause.ident:
                    pause.join(2)
                state.overlay_hide()
                state.pause(retain_tail=False)
                self.assertFalse(worker.is_alive())
                self.assertFalse(pause.is_alive())
                for child, _ in children:
                    for stream in (child.stdin, child.stdout):
                        if stream and not stream.closed:
                            stream.close()
