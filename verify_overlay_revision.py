"""Offline mutation evidence for revision blockers; changes memory only."""
import inspect
import io
import textwrap
import unittest
from unittest.mock import patch
import overlay_bridge as bridge
import server
from test_overlay_revision import RevisionTests
from test_overlay_blockers import BlockerTests
from test_overlay_teardown import OverlayTeardownTests


def mutated(fn, old, new):
    source = textwrap.dedent(inspect.getsource(fn))
    if old not in source:
        raise AssertionError('missing mutation target: ' + old)
    namespace = dict(fn.__globals__)
    exec(compile(source.replace(old, new, 1), '<revision-mutation>', 'exec'), namespace)
    return namespace[fn.__name__]


cases = [
    ('autonomous retirement denies capture admission', server.MeetingState, '_start',
     'self._reconcile_overlay_admission()', 'pass  # ignore autonomous bridge failure',
     OverlayTeardownTests, 'test_autonomous_pipe_failure_blocks_start_until_confirmed_cleanup'),
    ('confirmed exit permits fresh Show', server.MeetingState, '_reconcile_overlay_admission',
     'if self.pending_overlay is bridge and alive is False:', 'if False:',
     OverlayTeardownTests, 'test_confirmed_child_exit_allows_fresh_show_without_extra_hide'),
    ('status process inspection cannot block Pause', server.MeetingState, 'status',
     'alive = self._overlay_process_alive(overlay.process)',
     'alive = overlay.process is not None and overlay.process.poll() is None',
     OverlayTeardownTests, 'test_stalled_process_poll_cannot_hold_state_lock_or_block_pause'),
    ('unchanged scope must preserve authorization', server.MeetingState, '_refresh_overlay_token_locked',
     'if not force and callable(matches) and matches() is True:', 'if False:',
     RevisionTests, 'test_unchanged_scope_sync_preserves_inflight_and_completed'),
    ('real principal must rotate authorization', server.MeetingState, '_refresh_overlay_token_locked',
     'if not force and callable(matches) and matches() is True:', 'if not force:',
     RevisionTests, 'test_real_principal_change_retires_even_identical_scope'),
    ('real scope must rotate binding', server.MeetingState, 'overlay_scope',
     'self._refresh_overlay_token_locked(force=changed)', 'self._refresh_overlay_token_locked(force=False)',
     RevisionTests, 'test_real_scope_change_rotates_binding_and_retires_completed'),
    ('pane action capability fence', server.MeetingState, '_accept_overlay_action_locked',
     'if (not pane or pane != self.overlay_pane', 'if False and (not pane or pane != self.overlay_pane',
     RevisionTests, 'test_old_pane_and_delayed_show_actions_are_fenced'),
    ('new meeting synchronous retirement', server.MeetingState, '_start',
     'boundary = self.overlay_clear_boundary() if switching else nullcontext()', 'boundary = nullcontext()',
     RevisionTests, 'test_start_new_or_switched_meeting_retires_overlay_synchronously'),
    ('completed pre-cleanup capture transition', bridge.OverlayBridge, 'update',
     "if not self.answers.get(key, {}).get('completed')", 'if True',
     RevisionTests, 'test_capture_transition_between_completed_publication_and_cleanup'),
    ('run writes actual OS pipe', bridge.OverlayBridge, 'run',
     'self._start_pusher_locked()', 'pass  # no writer pump',
     RevisionTests, 'test_run_forwards_snapshot_to_actual_os_pipe'),
    ('swallowed preflight timeout fails test', BlockerTests,
     'test_hide_during_provider_preflight_prevents_provider_call',
     'if not release.wait(2):', 'if True:  # deliberate swallowed callback failure',
     BlockerTests, 'test_hide_during_provider_preflight_prevents_provider_call'),
    ('legacy helper rejected before private snapshots', bridge.OverlayBridge, 'start_helper',
     'if not self._check_helper_protocol():', 'if False:',
     RevisionTests, 'test_legacy_helper_protocol_is_rejected_with_upgrade_instruction'),
]

if __name__ == '__main__':
    for label, owner, name, old, new, test_class, test in cases:
        output = io.StringIO()
        with patch.object(owner, name, mutated(getattr(owner, name), old, new)):
            result = unittest.TextTestRunner(stream=output).run(test_class(test))
        if result.wasSuccessful() or result.errors:
            raise AssertionError(label + ': expected assertion failure\n' + output.getvalue())
        print(label + ': RED confirmed')
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RevisionTests)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(OverlayTeardownTests))
    suite.addTest(BlockerTests('test_hide_during_provider_preflight_prevents_provider_call'))
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        raise SystemExit(1)
    print('Restored production: GREEN')
