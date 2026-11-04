"""Real, inert process families; no UI or signaling of unrelated processes."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from overlay_bridge import OverlayBridge
from fastapi import HTTPException
import server
from thread_test_support import CheckedThreads


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('fixture did not reach its explicit gate')
        time.sleep(.01)


@contextmanager
def real_family(inherit_stdout=True, parent_exits=False, escape_group=False):
    # The descendant exits on a private file gate (with a failsafe timeout).
    # Cleanup never sends a signal to a numeric descendant PID / group ID.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        child_code = f'''
import os, time
from pathlib import Path
root = Path({tmp!r})
if {escape_group!r}: os.setsid()
root.joinpath('child-ready').write_text(str(os.getpid()))
deadline = time.monotonic() + 20
while not root.joinpath('release').exists() and time.monotonic() < deadline:
    if root.joinpath('write').exists() and not root.joinpath('wrote').exists():
        print('{{"type":"error","code":"late-retired-line"}}', flush=True)
        root.joinpath('wrote').touch()
    time.sleep(.01)
'''
        parent_code = f'''
import subprocess, sys, time
from pathlib import Path
if '--protocol-version' in sys.argv:
    print('meeting-overlay/2', flush=True)
    raise SystemExit(0)
root = Path({tmp!r})
child = subprocess.Popen([sys.executable, '-u', '-c', {child_code!r}],
                         stdin=subprocess.DEVNULL,
                         stdout={"None" if inherit_stdout else "subprocess.DEVNULL"},
                         stderr=subprocess.DEVNULL)
while not root.joinpath('child-ready').exists(): time.sleep(.01)
print('family-ready', flush=True)
if {parent_exits!r}: raise SystemExit(0)
time.sleep(20)
'''
        bridge = OverlayBridge(helper=sys.executable)
        bridge.helper_args = ['-u', '-c', parent_code]
        ready = threading.Event()
        bridge.on_click_line = lambda line: (ready.set() or False)
        process = None
        try:
            assert bridge.show_overlay()['shown']
            process = bridge.process
            assert process is not None
            assert ready.wait(3), 'reader did not consume family readiness'
            if parent_exits:
                process.wait(timeout=3)
            yield bridge, process, root
        finally:
            root.joinpath('release').touch()
            if process is not None:
                if process.poll() is None:
                    process.kill()  # original owned Popen only
                process.wait(timeout=3)
                # Prove the actual family is gone, including the orphan, before
                # leaving the fixture. Signal zero observes but never kills.
                def group_gone():
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        return True
                    return False
                wait_until(group_gone)
            ready_file = root.joinpath('child-ready')
            if ready_file.exists():
                child_pid = int(ready_file.read_text())
                def child_gone():
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        return True
                    return False
                # Also covers the escaped-group fixture. This only observes
                # absence; ID reuse fails closed rather than killing anything.
                wait_until(child_gone)
            bridge.stop()
            for worker in bridge.threads:
                worker.join(3)
                assert not worker.is_alive(), worker.name
            if bridge._cleanup_job:
                bridge._cleanup_job['worker'].join(3)
                assert not bridge._cleanup_job['worker'].is_alive()
            for worker in bridge._signal_jobs.values():
                worker.join(3)
                assert not worker.is_alive()
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()


@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'POSIX process groups required')
class OverlayDescendantTests(CheckedThreads):
    def test_controller_does_not_infer_shutdown_from_exited_parent(self):
        with real_family(parent_exits=True) as (bridge, process, root):
            state = server.MeetingState()
            state.overlay = bridge
            status = state.status(touch=False)
            self.assertFalse(status['overlay_stop_confirmed'])
            self.assertIsNone(status['overlay_shown'])
            self.assertFalse(state.overlay_hide()['overlay_stop_confirmed'])
            self.assertIs(state.pending_overlay, bridge)
            with self.assertRaises(HTTPException) as denied:
                state.start(True, 'native')
            self.assertEqual(denied.exception.status_code, 409)
            with self.assertRaises(HTTPException) as denied:
                state.overlay_show({})
            self.assertEqual(denied.exception.status_code, 503)
            root.joinpath('release').touch()
            wait_until(lambda: state.overlay_hide()['overlay_stop_confirmed'])
            self.assertIsNone(state.pending_overlay)
            self.assertTrue(state.status(touch=False)['overlay_stop_confirmed'])

    def test_escaped_descendant_holding_stdout_prevents_confirmation(self):
        with real_family(escape_group=True) as (bridge, process, root):
            self.assertFalse(bridge.hide_overlay()['stop_confirmed'])
            self.assertIsNotNone(process.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
            reader = bridge._reader
            assert reader is not None
            self.assertTrue(reader.is_alive())
            self.assertIs(bridge.process, process)
            root.joinpath('release').touch()
            reader.join(3)
            self.assertFalse(reader.is_alive())
            self.assertTrue(bridge.hide_overlay()['stop_confirmed'])

    def test_escaped_child_closing_pipes_is_outside_confirmation_boundary(self):
        # This pins a limitation, not a promise of arbitrary tree containment.
        with real_family(inherit_stdout=False, escape_group=True) as (bridge, process, root):
            wait_until(lambda: bridge.hide_overlay()['stop_confirmed'])
            self.assertIsNone(bridge.process)
            os.kill(int(root.joinpath('child-ready').read_text()), 0)
            # Fixture cleanup releases its private gate, never signals that PID.

    def test_retired_line_is_not_eof_from_escaped_stdout_holder(self):
        with real_family(escape_group=True) as (bridge, process, root):
            assert bridge._reader is not None and process.stdout is not None
            bridge.invalidate()
            process.terminate()  # owned handle, never a numeric PID signal
            process.wait(timeout=3)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
            root.joinpath('write').touch()
            wait_until(lambda: root.joinpath('wrote').exists())
            # Give the actual reader a chance to consume the retired line.
            # Either a blocked drain or an exited reader must not imply EOF.
            bridge._reader.join(.15)
            for _ in range(2):
                started = time.monotonic()
                result = bridge.hide_overlay()
                self.assertLess(time.monotonic() - started, .8)
                os.kill(int(root.joinpath('child-ready').read_text()), 0)
                self.assertFalse(result['stop_confirmed'], 'retired line is not pipe EOF')
                self.assertIs(bridge.process, process)
                self.assertFalse(process.stdout.closed)
                self.assertIsNone(bridge._sticky_error)
                self.assertTrue(bridge.lock.acquire(timeout=.2))
                bridge.lock.release()
            root.joinpath('release').touch()
            bridge._reader.join(3)
            self.assertFalse(bridge._reader.is_alive())
            wait_until(lambda: bridge.hide_overlay()['stop_confirmed'])

    def test_parent_exit_does_not_confirm_live_descendant_or_reader(self):
        for inherit_stdout in (True, False):
            for parent_exits in (False, True):
                with self.subTest(inherit_stdout=inherit_stdout, parent_exits=parent_exits), \
                        real_family(inherit_stdout, parent_exits) as (bridge, process, root):
                    real_killpg = os.killpg
                    reader = bridge._reader
                    assert reader is not None
                    with patch.object(os, 'killpg', wraps=real_killpg) as group_probe:
                        result = bridge.hide_overlay()
                        self.assertIsNotNone(process.poll())
                        os.kill(int(root.joinpath('child-ready').read_text()), 0)
                        self.assertFalse(result['stop_confirmed'], 'parent exit is not descendant exit')
                        self.assertIsNone(result['shown'])
                        self.assertIs(bridge.process, process, 'retain uncertain family ownership')
                        self.assertIn('not confirmed', result['error'])
                        if inherit_stdout:
                            self.assertTrue(reader.is_alive())
                        self.assertFalse(bridge.show_overlay()['shown'])
                        assert bridge._cleanup_job is not None
                        self.assertTrue(bridge._cleanup_job['done'].wait(1))
                        bridge._cleanup_job['worker'].join(1)
                        self.assertFalse(bridge._cleanup_job['worker'].is_alive(), 'no infinite reader join')
                        self.assertTrue(group_probe.called)
                        self.assertTrue(all(c.args == (process.pid, 0) for c in group_probe.call_args_list),
                                        'never signal a potentially reused group ID')
                    root.joinpath('release').touch()
                    reader.join(3)
                    wait_until(lambda: bridge.hide_overlay()['stop_confirmed'])
                    self.assertIsNone(bridge.process)
                    self.assertFalse(reader.is_alive())

    def test_group_probe_denied_is_unconfirmed_and_retryable(self):
        with real_family() as (bridge, process, root):
            root.joinpath('release').touch()
            with patch.object(os, 'killpg', side_effect=PermissionError('denied')) as probe:
                self.assertFalse(bridge.hide_overlay()['stop_confirmed'])
                self.assertIs(bridge.process, process)
                probe.assert_called_with(process.pid, 0)
            wait_until(lambda: bridge.hide_overlay()['stop_confirmed'])
