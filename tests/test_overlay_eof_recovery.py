"""Real inert EOF recovery; never launch UI or signal numeric child IDs."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from overlay_bridge import OverlayBridge
from tests.test_overlay_descendants import wait_until
from tests.thread_test_support import CheckedThreads
import server
from fastapi import HTTPException


@contextmanager
def failed_reader(escaped_holder=False):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        holder = f'''
import os, time
from pathlib import Path
root = Path({tmp!r})
os.setsid()
root.joinpath('holder').write_text(str(os.getpid()))
deadline = time.monotonic() + 20
while not root.joinpath('release').exists() and time.monotonic() < deadline: time.sleep(.01)
'''
        code = f'''
import os, subprocess, sys, time
from pathlib import Path
if '--protocol-version' in sys.argv:
    print('meeting-overlay/2'); raise SystemExit
root = Path({tmp!r})
if {escaped_holder!r}:
    subprocess.Popen([sys.executable, '-u', '-c', {holder!r}], stdin=subprocess.DEVNULL)
    while not root.joinpath('holder').exists(): time.sleep(.01)
os.write(1, b'\\xff\\n')
time.sleep(30)
'''
        bridge = OverlayBridge(helper=sys.executable)
        bridge.helper_args = ['-u', '-c', code]
        process = None
        try:
            bridge.show_overlay()
            process = bridge.process
            assert process is not None
            reader = bridge._reader
            assert reader is not None
            reader.join(3)
            assert not reader.is_alive(), 'invalid UTF-8 must terminate original reader'
            assert not bridge._stdout_eof[process].is_set()
            yield bridge, process, root
        finally:
            root.joinpath('release').touch()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
            if root.joinpath('holder').exists():
                pid = int(root.joinpath('holder').read_text())
                def gone():
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    return False
                wait_until(gone)
            bridge.stop()
            for worker in bridge.threads:
                worker.join(3)
                assert not worker.is_alive(), worker.name
            if bridge._cleanup_job:
                bridge._cleanup_job['worker'].join(3)
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()


@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'POSIX pipe/select helper required')
class EOFRecoveryTests(CheckedThreads):
    def test_decode_failure_recovers_only_after_actual_eof(self):
        with failed_reader(escaped_holder=True) as (bridge, process, root):
            eof = bridge._stdout_eof[process]
            process.terminate()
            process.wait(timeout=3)
            # Original reader is dead, leader/group gone; holder still owns pipe.
            for _ in range(2):
                started = time.monotonic()
                self.assertFalse(bridge.hide_overlay()['stop_confirmed'])
                self.assertLess(time.monotonic() - started, .8)
                self.assertFalse(eof.is_set())
                os.kill(int(root.joinpath('holder').read_text()), 0)
            root.joinpath('release').touch()
            # A direct raw read proves the kernel EOF, but is NOT bridge evidence.
            import select
            assert process.stdout is not None
            self.assertTrue(select.select([process.stdout], [], [], 3)[0])
            self.assertEqual(os.read(process.stdout.fileno(), 1), b'')
            self.assertFalse(eof.is_set())
            self.assertTrue(bridge.hide_overlay()['stop_confirmed'])
            self.assertTrue(eof.is_set())
            self.assertIsNone(bridge.process)

    def test_retired_startup_registers_pipe_before_publication(self):
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
            parent = f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, '-u', '-c', {holder!r}], stdin=subprocess.DEVNULL)
time.sleep(30)
"""
            bridge = OverlayBridge(helper=sys.executable)
            bridge.helper_args = ['-u', '-c', parent]
            state = server.MeetingState()
            born, publish = threading.Event(), threading.Event()
            children, results = [], []
            popen = subprocess.Popen
            def gated_spawn(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                wait_until(lambda: root.joinpath('holder').exists())
                born.set()
                assert publish.wait(3)
                return child
            def show():
                try:
                    results.append(state.overlay_show({}))
                except HTTPException as exc:
                    results.append(exc.status_code)
            worker = threading.Thread(target=self.checked_callback(show))
            try:
                with patch('overlay_bridge.OverlayBridge', return_value=bridge), \
                        patch.object(bridge, '_check_helper_protocol', return_value=True), \
                        patch('overlay_bridge.subprocess.Popen', side_effect=gated_spawn):
                    worker.start()
                    self.assertTrue(born.wait(3))
                    self.assertFalse(state.overlay_hide()['overlay_stop_confirmed'])
                    publish.set()
                    worker.join(4)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(results, [503])
                child = children[0]
                started = time.monotonic()
                self.assertFalse(state.overlay_hide()['overlay_stop_confirmed'])
                self.assertLess(time.monotonic() - started, 1.2)
                self.assertIs(state.pending_overlay, bridge)
                self.assertIs(bridge.process, child)
                self.assertIn(child, bridge._stdout_eof)
                self.assertFalse(bridge._stdout_eof[child].is_set())
                os.kill(int(root.joinpath('holder').read_text()), 0)
                root.joinpath('release').touch()
                wait_until(lambda: state.overlay_hide()['overlay_stop_confirmed'])
                self.assertIsNone(state.pending_overlay)
            finally:
                publish.set()
                root.joinpath('release').touch()
                worker.join(4)
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=3)
                if root.joinpath('holder').exists():
                    pid = int(root.joinpath('holder').read_text())
                    def gone():
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            return True
                        return False
                    wait_until(gone)
                state.overlay_hide()
                bridge.stop()
                for thread in bridge.threads:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                for child in children:
                    for stream in (child.stdout, child.stdin):
                        if stream is not None and not stream.closed:
                            stream.close()

    def test_controller_startup_commit_rejects_known_reader_failure(self):
        with failed_reader() as (bridge, process, root):
            state = server.MeetingState()
            # Model the failure arriving after bridge Show's health check but
            # before the controller commits its result. The real reader is dead.
            try:
                with patch('overlay_bridge.OverlayBridge', return_value=bridge), \
                        patch.object(bridge, 'show_overlay', return_value={'shown': True}):
                    with self.assertRaises(HTTPException) as denied:
                        state.overlay_show({})
                self.assertEqual(denied.exception.status_code, 503)
                self.assertIsNone(state.overlay)
            finally:
                state.overlay_hide()

    def test_known_reader_failure_rejects_direct_and_controller_show(self):
        with failed_reader() as (bridge, process, root):
            self.assertIsNone(process.poll())
            self.assertFalse(bridge.show_overlay().get('shown'))
            state = server.MeetingState()
            state.overlay = bridge
            with self.assertRaises(HTTPException) as denied:
                state.overlay_show({})
            self.assertEqual(denied.exception.status_code, 503)
            state.overlay_hide()


if __name__ == '__main__':
    unittest.main()
