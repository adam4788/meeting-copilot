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
