"""Inert real helpers: protocol bytes and live display stdout health."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from overlay_bridge import OverlayBridge
from test_overlay_descendants import wait_until
from thread_test_support import CheckedThreads
import server


@contextmanager
def inert_helper(code):
    bridge = OverlayBridge(helper=sys.executable)
    bridge.helper_args = ['-u', '-c', code]
    children = []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    try:
        with patch('overlay_bridge.subprocess.Popen', side_effect=spawn):
            yield bridge, children
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()  # owned original handle only, never numeric IDs/groups
            child.wait(timeout=3)
        bridge.stop()
        for worker in bridge.threads:
            worker.join(3)
            assert not worker.is_alive(), worker.name
        jobs = list(bridge._signal_jobs.values())
        if bridge._cleanup_job:
            jobs.append(bridge._cleanup_job['worker'])
        for worker in jobs:
            worker.join(3)
            assert not worker.is_alive(), worker.name
        for child in children:
            for stream in (child.stdout, child.stdin):
                if stream is not None and not stream.closed:
                    stream.close()


@contextmanager
def closing_display():
    with tempfile.TemporaryDirectory() as tmp:
        close = Path(tmp) / 'close'
        code = f"""
import os, sys, time
from pathlib import Path
if '--protocol-version' in sys.argv:
    print('meeting-overlay/2'); raise SystemExit
print('ready', flush=True)
deadline = time.monotonic() + 20
while not Path({str(close)!r}).exists() and time.monotonic() < deadline: time.sleep(.01)
os.close(1)
time.sleep(20)
"""
        with inert_helper(code) as (bridge, children):
            def close_stdout():
                close.touch()
                reader = bridge._reader
                assert reader is not None
                reader.join(3)
                assert not reader.is_alive(), 'actual stdout EOF must finish reader'
                process = bridge.process
                assert process is not None
                assert bridge._stdout_eof[process].is_set()
                assert process.poll() is None, 'display must still be alive'
            yield bridge, children, close_stdout


@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'POSIX pipe/select helper required')
class ChannelHealthTests(CheckedThreads):
    def test_fresh_direct_show_rejects_active_stdout_eof(self):
        with closing_display() as (bridge, children, close_stdout):
            push = bridge.push
            def closed_push():
                close_stdout()
                return push()
            with patch.object(bridge, 'push', side_effect=closed_push):
                self.assertFalse(bridge.show_overlay()['shown'])
            self.assertTrue(bridge.hide_overlay()['stop_confirmed'])

    def test_fresh_controller_show_rejects_active_stdout_eof(self):
        with closing_display() as (bridge, children, close_stdout):
            state = server.MeetingState()
            show = bridge.show_overlay
            def closed_show():
                result = show()
                self.assertTrue(result['shown'], 'healthy live pipe must be accepted first')
                close_stdout()  # EOF after direct Show, before controller commit
                return result
            try:
                with patch('overlay_bridge.OverlayBridge', return_value=bridge), \
                        patch.object(bridge, 'show_overlay', side_effect=self.checked_callback(closed_show)):
                    with self.assertRaises(HTTPException) as denied:
                        state.overlay_show({})
                self.assertEqual(denied.exception.status_code, 503)
                self.assertIsNone(state.overlay)
                self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
            finally:
                state.overlay_hide()

    def test_repeated_direct_show_rejects_active_stdout_eof(self):
        with closing_display() as (bridge, children, close_stdout):
            self.assertTrue(bridge.show_overlay()['shown'])
            self.assertTrue(bridge.show_overlay()['reused'])
            close_stdout()
            eof = bridge._stdout_eof[bridge.process]
            for _ in range(2):
                self.assertFalse(bridge.show_overlay()['shown'])
            self.assertTrue(eof.is_set(), 'unhealthy reader still owns valid teardown EOF')
            self.assertTrue(bridge.hide_overlay()['stop_confirmed'])

    def test_repeated_controller_show_rejects_active_stdout_eof(self):
        with closing_display() as (bridge, children, close_stdout):
            state = server.MeetingState()
            try:
                with patch('overlay_bridge.OverlayBridge', return_value=bridge):
                    self.assertTrue(state.overlay_show({})['shown'])
                    self.assertTrue(state.overlay_show({})['reused'])
                close_stdout()
                # Keep the inert live child owned so retries exercise retained
                # cleanup/admission, not a replacement healthy helper.
                process = bridge.process
                with patch.object(process, 'terminate', side_effect=PermissionError), \
                        patch.object(process, 'kill', side_effect=PermissionError):
                    for _ in range(2):
                        with self.assertRaises(HTTPException) as denied:
                            state.overlay_show({})
                        self.assertIn(denied.exception.status_code, (409, 503))
                        self.assertIsNone(state.overlay)
                        if state._overlay_cleanup_job:
                            self.assertTrue(state._overlay_cleanup_job['done'].wait(3))
                        if bridge._cleanup_job:
                            self.assertTrue(bridge._cleanup_job['done'].wait(3))
                self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
            finally:
                state.overlay_hide()

    def test_protocol_decode_is_strict_even_around_valid_token(self):
        code = "import os; os.write(1, b'\\xffmeeting-overlay/2\\n')"
        with inert_helper(code) as (bridge, children):
            self.assertFalse(bridge.show_overlay()['shown'])
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].wait(timeout=3), 0)
            self.assertTrue(bridge.hide_overlay()['stop_confirmed'])

    def test_protocol_read_exception_and_closed_handle_are_not_eof(self):
        popen = subprocess.Popen
        def close_and_fail(child, **kwargs):
            child.stdout.close()
            raise OSError('injected read failure, not EOF')
        with inert_helper('import time; time.sleep(20)') as (bridge, children), \
                patch.object(popen, 'communicate', close_and_fail):
            self.assertFalse(bridge.show_overlay()['shown'])
            child = children[0]
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdout.closed)
            self.assertIs(bridge.process, child, 'read failure cannot confirm cleanup')
            self.assertFalse(bridge._stdout_eof[child].is_set())
            self.assertFalse(bridge.hide_overlay()['stop_confirmed'])
            self.assertIs(bridge.process, child)

    def test_protocol_live_pipe_holder_retains_uncertainty_until_real_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = f"""
import os, time
from pathlib import Path
root = Path({tmp!r})
os.setsid()
root.joinpath('holder').write_text(str(os.getpid()))
deadline = time.monotonic() + 20
while not root.joinpath('release').exists() and time.monotonic() < deadline: time.sleep(.01)
"""
            code = f"""
import os, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-u', '-c', {holder!r}], stdin=subprocess.DEVNULL)
while not Path({str(root / 'holder')!r}).exists(): time.sleep(.01)
os.write(1, b'\\xff\\n')
"""
            with inert_helper(code) as (bridge, children):
                # A regression must not activate a real audio adapter either.
                audio = SimpleNamespace(start=lambda **kw: None, stop=lambda: None)
                state = server.MeetingState(audio_factory=lambda: audio)
                try:
                    with patch('overlay_bridge.OverlayBridge', return_value=bridge):
                        started = time.monotonic()
                        with self.assertRaises(HTTPException) as denied:
                            state.overlay_show({})
                        self.assertEqual(denied.exception.status_code, 503)
                        self.assertLess(time.monotonic() - started, 4)
                    self.assertEqual(len(children), 1)
                    child = children[0]
                    self.assertEqual(child.wait(timeout=3), 0)
                    self.assertIs(bridge.process, child, 'live holder must retain probe')
                    eof = bridge._stdout_eof[child]
                    self.assertFalse(eof.is_set())
                    for _ in range(2):
                        started = time.monotonic()
                        self.assertFalse(state.overlay_hide()['overlay_stop_confirmed'])
                        self.assertLess(time.monotonic() - started, 1.2)
                        self.assertFalse(eof.is_set())
                        os.kill(int(root.joinpath('holder').read_text()), 0)
                    self.assertIs(state.pending_overlay, bridge)
                    with self.assertRaises(HTTPException) as blocked:
                        state.start(True, 'native')
                    self.assertEqual(blocked.exception.status_code, 409)
                    root.joinpath('release').touch()
                    wait_until(lambda: state.overlay_hide()['overlay_stop_confirmed'])
                    self.assertTrue(eof.is_set())
                    self.assertIsNone(state.pending_overlay)
                finally:
                    root.joinpath('release').touch()
                    if root.joinpath('holder').exists():
                        pid = int(root.joinpath('holder').read_text())
                        def gone():
                            try:
                                os.kill(pid, 0)  # observation only; gate releases holder
                            except ProcessLookupError:
                                return True
                            return False
                        wait_until(gone)
                    state.pause(retain_tail=False)
                    state.overlay_hide()

    def test_malformed_protocol_exit_zero_allows_hide_and_new_start(self):
        code = "import os; os.write(1, b'\\xff\\n')"
        starts = []
        audio = SimpleNamespace(start=lambda **kw: starts.append(kw), stop=lambda: None)
        state = server.MeetingState(audio_factory=lambda: audio)
        with inert_helper(code) as (bridge, children), \
                patch('overlay_bridge.OverlayBridge', return_value=bridge), \
                patch.object(state, 'watchdog'):
            try:
                with self.assertRaises(HTTPException) as denied:
                    state.overlay_show({})
                self.assertEqual(denied.exception.status_code, 503)
                self.assertEqual(len(children), 1, 'incompatible probe must not launch display')
                self.assertEqual(children[0].wait(timeout=3), 0)
                started = time.monotonic()
                self.assertTrue(state.overlay_hide()['overlay_stop_confirmed'])
                self.assertLess(time.monotonic() - started, 1.2)
                self.assertIsNone(state.pending_overlay)
                self.assertIsNone(bridge.process)
                self.assertTrue(state.start(True, 'native')['listening'])
                self.assertEqual(len(starts), 1)
            finally:
                state.pause(retain_tail=False)
                state.overlay_hide()


if __name__ == '__main__':
    unittest.main()
