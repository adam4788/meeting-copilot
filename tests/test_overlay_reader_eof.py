"""EOF evidence tests with inert in-memory streams; portable to Windows."""
import io
import threading
from unittest.mock import Mock, patch

from overlay_bridge import OverlayBridge
from tests.thread_test_support import CheckedThreads


class ReaderEOFTests(CheckedThreads):
    def test_missing_pipe_evidence_cannot_confirm(self):
        bridge = OverlayBridge()
        process = Mock(stdout=io.StringIO(), stdin=None)
        process.poll.return_value = 0
        bridge.process = process
        try:
            self.assertFalse(bridge.hide_overlay()['stop_confirmed'])
            self.assertIs(bridge.process, process)
        finally:
            assert bridge._cleanup_job is not None
            bridge._cleanup_job['worker'].join(1)
            process.stdout.close()

    def test_recovery_never_reads_concurrent_or_replaced_pipe(self):
        bridge = OverlayBridge()
        stream = Mock(closed=False)
        stream.fileno.return_value = 42
        process = Mock(stdout=stream)
        bridge._stdout_eof[process] = threading.Event()
        bridge._stdout_pipes[process] = stream
        reader = Mock()
        reader.is_alive.return_value = True
        import stat
        with patch('sys.platform', 'darwin'), patch('os.fstat', return_value=Mock(st_mode=stat.S_IFIFO)), \
                patch('os.read') as read, patch('os.set_blocking', create=True) as nonblocking:
            bridge._recover_stdout_eof(process, reader)
            read.assert_not_called()
            nonblocking.assert_not_called()
            reader.is_alive.return_value = False
            process.stdout = Mock()
            bridge._recover_stdout_eof(process, reader)
            read.assert_not_called()
            nonblocking.assert_not_called()
        self.assertFalse(bridge._stdout_eof[process].is_set())

    def test_reader_io_error_is_not_eof_even_after_thread_exits(self):
        class BrokenStream(io.StringIO):
            def __next__(self):
                raise OSError('injected read failure, not EOF')
        bridge = OverlayBridge()
        process = Mock(stdout=BrokenStream(), stdin=None)
        process.poll.return_value = 0
        bridge.process = process
        eof = bridge._stdout_eof[process] = threading.Event()
        reader = bridge._reader = threading.Thread(target=bridge._read_clicks, args=(process,))
        reader.start()
        reader.join(1)
        self.assertFalse(reader.is_alive())
        self.assertFalse(eof.is_set())
        try:
            result = bridge.hide_overlay()
            self.assertFalse(result['stop_confirmed'])
            self.assertIs(bridge.process, process)
            self.assertFalse(process.stdout.closed)
            process.terminate.assert_not_called()
            process.kill.assert_not_called()
        finally:
            assert bridge._cleanup_job is not None
            bridge._cleanup_job['worker'].join(1)
            process.stdout.close()

    def test_retired_reader_records_only_its_own_real_eof(self):
        bridge = OverlayBridge()
        old = Mock(stdout=io.StringIO('retired line\n'), stdin=None)
        newer = Mock(stdout=io.StringIO('new line\n'), stdin=None)
        old.poll.return_value = 0
        bridge.process = newer
        old_eof = bridge._stdout_eof[old] = threading.Event()
        new_eof = bridge._stdout_eof[newer] = threading.Event()
        with self.subTest('old reader must not publish or consume newer stdout'):
            bridge.on_click_line = Mock()
            bridge._read_clicks(old)
            self.assertTrue(old_eof.is_set())
            self.assertFalse(new_eof.is_set())
            self.assertEqual(newer.stdout.tell(), 0)
            bridge.on_click_line.assert_not_called()
        old.stdout.close()
        newer.stdout.close()
