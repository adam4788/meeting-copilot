"""EOF evidence tests with inert in-memory streams; portable to Windows."""
import io
import threading
from unittest.mock import Mock

from overlay_bridge import OverlayBridge
from thread_test_support import CheckedThreads


class ReaderEOFTests(CheckedThreads):
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
