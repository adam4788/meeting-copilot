"""Probe accounting and actionable compile failures, without devices or Swift."""
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

import verify_native_backpressure as probe


class ProbeTests(unittest.TestCase):
    def process(self, attempted):
        process = Mock(returncode=0)
        row = dict(type='pcm', samples=171, native_dropped_chunks=2)
        process.communicate.return_value = (
            json.dumps(row).encode() + b'\n',
            json.dumps(dict(attempted=attempted, pending=0)).encode())
        process.poll.return_value = 0
        return process

    def test_received_plus_reported_matches_attempted(self):
        with patch.object(probe.subprocess, 'Popen', return_value=self.process(3)):
            result = probe.measure('synthetic')
        self.assertEqual(result['received'], 1)
        self.assertEqual(result['reported_drops'], 2)

    def test_unaccounted_loss_fails_verification(self):
        with patch.object(probe.subprocess, 'Popen', return_value=self.process(4)):
            with self.assertRaises(AssertionError):
                probe.measure('synthetic')

    def test_compile_failure_keeps_diagnostic_and_never_runs_helper(self):
        failed = subprocess.CompletedProcess([], 1, stdout='', stderr='synthetic SDK diagnostic')
        with patch.object(probe.subprocess, 'run', return_value=failed), \
                patch.object(probe.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'synthetic SDK diagnostic'):
                probe.main()
        spawn.assert_not_called()
