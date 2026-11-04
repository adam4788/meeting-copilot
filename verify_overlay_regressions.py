"""One-shot mutation check: prove targeted regressions fail without each fix.
Only patches in-memory methods; never edits repository sources.
"""
import inspect
import io
import textwrap
import unittest
from unittest.mock import patch
import overlay_bridge as b
import server
from test_overlay_blockers import BlockerTests


def mutated(fn, old, new):
    source = textwrap.dedent(inspect.getsource(fn))
    if old not in source:
        raise AssertionError('mutation target not found: ' + old)
    ns = dict(fn.__globals__)
    exec(compile(source.replace(old, new, 1), '<regression-mutation>', 'exec'), ns)
    return ns[fn.__name__]


cases = [
    ('A wire identity', b.OverlayBridge, 'on_click_line',
     "        if json.loads(line).get('snapshot_id') != self.snapshot_id:\n            return False\n", '',
     'test_buffered_native_click_cannot_rebind_identical_question'),
    ('B owner identity', server.MeetingState, 'overlay_scope',
     "if (bridge is None or not isinstance(scope, dict) or not self.overlay_owner\n                or scope.get('owner') != self.overlay_owner):", 'if False:',
     'test_previous_owner_scope_is_rejected_even_with_higher_sequence'),
    ('B same-scope context', server.MeetingState, '_refresh_overlay_token_locked',
     'if callable(retire):', 'if False:',
     'test_same_scope_authorization_refresh_retires_pending_tap'),
    ('D synchronous clear retirement', server.MeetingState, 'overlay_clear_boundary',
     'self._retire_overlay_locked()', 'pass  # skip synchronous retirement',
     'test_clear_retires_overlay_without_waiting_for_poll'),
    ('E admission mapping fence', b.OverlayBridge, 'ask',
     '            self.row_by_request[request_id] = row_id', '            pass  # recreate unmapped admitted slot',
     'test_hide_at_admission_exit_cancels_before_provider_start'),
    ('D delivery fence', b.OverlayBridge, '_emit',
     '                self.pushed.append(snapshot)',
     '                self.lock.release()\n                try:\n                    self.pushed.append(snapshot)\n                finally:\n                    self.lock.acquire()',
     'test_clear_serializes_with_delivery_already_past_validation'),
    ('F preserve completed on pause', b.OverlayBridge, 'update',
     'if meeting_changed:\n', 'if True:\n',
     'test_pause_preserves_completed_but_retires_pending'),
    ('G swallowed assertions', b.OverlayBridge, 'ask',
     'except AssertionError:\n                raise  # Programming/test failures are not admission rejections.',
     'except AssertionError:\n                request_id = None',
     'test_admission_assertions_are_not_swallowed_as_rejection'),
    ('H stale non-dict result', b.OverlayBridge, 'update',
     '            if poller_seq is not None and poller_seq != self._poller_seq:\n                return False\n', '',
     'test_stale_non_dict_poller_result_does_not_change_state'),
    ('I bounded ID dedupe', b, 'suggest_questions',
     "str(segment_id or '').strip()[:200]", "str(segment_id or '').strip()",
     'test_long_explicit_ids_are_deduplicated_after_bounding'),
    ('H plain publication lock', b.OverlayBridge, 'ask',
     'transport._assist_locked(', 'transport.assist(',
     'test_nonreentrant_publication_lock_is_supported'),
]
for label, owner, name, old, new, test in cases:
    mutant = mutated(getattr(owner, name), old, new)
    output = io.StringIO()
    with patch.object(owner, name, mutant):
        result = unittest.TextTestRunner(stream=output).run(BlockerTests(test))
    if result.wasSuccessful():
        raise AssertionError(label + ': regression did NOT detect mutation')
    print(label + ': RED confirmed (' + str(len(result.failures)) + ' assertion failures, '
          + str(len(result.errors)) + ' errors)')
    # Ensure failures are the regression assertions, not setup/import errors.
    if result.errors:
        print(output.getvalue())
        raise AssertionError(label + ': unexpected test error')
result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(BlockerTests))
if not result.wasSuccessful():
    raise SystemExit(1)
print('Restored production methods: GREEN')
