"""Make background exceptions fail concurrency tests, not just print warnings."""
import threading
import unittest
from unittest.mock import patch


class CheckedThreads(unittest.TestCase):
    def checked_callback(self, callback):
        """Record before re-raising: production may catch callback exceptions."""
        def invoke(*args, **kwargs):
            try:
                return callback(*args, **kwargs)
            except BaseException as exc:
                self.worker_errors.append(exc)
                raise
        return invoke

    def checked_wait(self, event, timeout, label='callback gate'):
        def wait():
            if not event.wait(timeout):
                raise AssertionError(label + ' timed out')
        return self.checked_callback(wait)()

    def setUp(self):
        super().setUp()
        self.worker_errors = []
        hook = patch.object(threading, 'excepthook',
                            lambda args: self.worker_errors.append(args.exc_value))
        hook.start()
        self.addCleanup(hook.stop)
        self.addCleanup(lambda: self.assertEqual(self.worker_errors, [], 'background worker failures'))


def synthetic_ready_preflight(*args, **kwargs):
    """Ready report for lifecycle tests whose capture adapters are synthetic.

    Real readiness behavior belongs to the dedicated preflight test suites.
    """
    import time
    return {
        'version': 1,
        'ready': True,
        'checks': [{'id': 'synthetic_capture', 'label': 'Synthetic capture fixture',
                    'status': 'ready', 'detail': 'No device or provider is used.'}],
        'checked_at': time.time(),
        'valid_for_seconds': 15,
    }


def mock_capture_preflight(test_case):
    """Explicit, test-scoped readiness mock; restore it even if a test fails."""
    from server import MeetingState
    readiness = patch.object(MeetingState, 'capture_preflight',
                             side_effect=synthetic_ready_preflight)
    mocked = readiness.start()
    test_case.addCleanup(readiness.stop)
    return mocked
