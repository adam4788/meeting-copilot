"""Windows capability/skip simulations, NOT native Windows execution."""
import os
from pathlib import Path
import runpy
import select
import sys
import unittest
from unittest.mock import patch

import overlay_bridge
import test_overlay_startup_admission


class OverlayPortabilityTests(unittest.TestCase):
    def test_posix_pipe_tests_are_explicitly_skipped_on_simulated_windows(self):
        cases = {
            'test_overlay_blockers.py': ('BlockerTests', [
                'test_blocked_real_pipe_cannot_stall_scope_and_hide',
                'test_real_fd_writer_roundtrip_and_full_pipe_deadline']),
            'test_overlay_revision.py': ('RevisionTests', [
                'test_run_forwards_snapshot_to_actual_os_pipe']),
            'test_overlay_teardown.py': ('OverlayTeardownTests', None),
        }
        # Reload declarations under a simulated platform, not the actual OS.
        # Guard before running anything, so a missing skip cannot spawn a child.
        with patch.object(sys, 'platform', 'win32'):
            suite = unittest.TestSuite()
            for filename, (class_name, names) in cases.items():
                cls = runpy.run_path(str(Path(__file__).with_name(filename)))[class_name]
                names = names or unittest.defaultTestLoader.getTestCaseNames(cls)
                for name in names:
                    self.assertTrue(getattr(cls, '__unittest_skip__', False) or
                                    getattr(getattr(cls, name), '__unittest_skip__', False),
                                    f'{filename}:{name} requires an explicit POSIX gate')
                    suite.addTest(cls(name))
            count = suite.countTestCases()
            with patch.dict(os.__dict__), patch.object(select, 'select', side_effect=OSError(
                    10038, 'socket-only select simulation')) as selection:
                os.__dict__.pop('set_blocking', None)  # Python 3.11 Windows capability
                result = unittest.TestResult()
                suite.run(result)
                self.assertTrue(result.wasSuccessful(), (result.failures, result.errors))
                self.assertEqual(len(result.skipped), count)
                selection.assert_not_called()

    def test_unsupported_windows_path_needs_no_posix_capabilities_simulation(self):
        for has_set_blocking in (False, True):  # 3.11 absent; 3.12 pipe API present
            with self.subTest(has_set_blocking=has_set_blocking), patch.dict(os.__dict__), \
                    patch.object(select, 'select', side_effect=OSError(
                        10038, 'socket-only select simulation')) as selection:
                if has_set_blocking:
                    # The unsupported path must never need this capability.
                    os.set_blocking = lambda *args: self.fail('unexpected pipe setup')
                else:
                    os.__dict__.pop('set_blocking', None)
                case = test_overlay_startup_admission.StartupAdmissionTests(
                    'test_windows_helper_is_unsupported_without_spawning')
                result = unittest.TestResult()
                case.run(result)
                self.assertTrue(result.wasSuccessful(), (result.failures, result.errors))
                self.assertEqual(result.skipped, [])
                selection.assert_not_called()
