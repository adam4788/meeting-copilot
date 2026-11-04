"""Offline follow-up mutation checks: memory-only Python, isolated JS copy."""
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import overlay_bridge as bridge
import server
from test_overlay_followup import FollowupTests
from test_pane_ordering import PaneOrderingTests
from test_overlay_bridge import ClickTests, BridgeLifecycleTests
from test_overlay_revision import RevisionTests
from verify_overlay_revision import mutated


CASES = [
    ('caught streaming publication assertion propagates', BridgeLifecycleTests,
     'test_fake_provider_streams_distinct_chunks_in_order',
     'return original_push()', "raise AssertionError('injected streaming publication failure')",
     BridgeLifecycleTests, 'test_fake_provider_streams_distinct_chunks_in_order'),
    ('delayed destructive controls require lifecycle authorization', server.MeetingState, 'control_boundary',
     'if not accepted:', 'if False:',
     PaneOrderingTests, 'test_delayed_destructive_requests_cannot_mutate_new_pane'),
    ('creation order not delayed GET arrival', server.MeetingState, 'overlay_claim',
     'creation <= self.overlay_creation', 'False',
     PaneOrderingTests, 'test_creation_order_wins_even_when_old_lookup_arrives_last'),
    ('different renderers cannot guess creation order', server.MeetingState, 'overlay_claim',
     'if self.overlay_client not in (None, client):', 'if False:',
     PaneOrderingTests, 'test_uncomparable_renderers_and_backend_restart_fail_closed'),
    ('capture capability admission', server.MeetingState, 'control_boundary',
     'and self._accept_overlay_action_locked(body.pane, body.action)', 'and True',
     FollowupTests, 'test_capture_start_is_ordered_with_release_and_new_owner'),
    ('release-only claim recovery', server.MeetingState, 'overlay_claim',
     'if self.overlay_pane is None and revision == self.overlay_revision - 1:', 'if False:',
     FollowupTests, 'test_release_only_ticket_conflict_recovers_but_newer_claim_wins'),
    ('never offer recovery over a live newer owner', server.MeetingState, 'overlay_claim',
     'if self.overlay_pane is None and revision == self.overlay_revision - 1:',
     'if revision == self.overlay_revision - 1:',
     FollowupTests, 'test_release_only_ticket_conflict_recovers_but_newer_claim_wins'),
    ('first capture is not a meeting switch', server.MeetingState, '_start',
     '(self.session_key is not None and self.session_key != session_key) or new_meeting',
     'self.session_key != session_key or new_meeting',
     FollowupTests, 'test_show_before_first_start_keeps_live_overlay_and_owner'),
    ('partial cancellation warns before status pump', bridge.OverlayBridge, 'ask',
     "['Answer cancelled; the text above is partial.'] if seen else []", '[]',
     FollowupTests, 'test_partial_cancelled_before_status_pump_does_not_look_completed'),
    ('unmapped partial retired on pause', bridge.OverlayBridge, 'update',
     "or not answer.get('completed')", '',
     FollowupTests, 'test_partial_cancelled_before_status_pump_does_not_look_completed'),
    ('caught cancel exception fails on test thread', ClickTests,
     'test_scope_push_uses_current_pane_settings_and_cancels_stale_rows',
     'cancellations.append(request_id)',
     "cancellations.append(request_id); raise AssertionError('injected caught cancellation failure')",
     ClickTests, 'test_scope_push_uses_current_pane_settings_and_cancels_stale_rows'),
    ('missing cancel side effect fails on test thread', ClickTests,
     'test_scope_push_uses_current_pane_settings_and_cancels_stale_rows',
     'cancellations.append(request_id)', 'pass',
     ClickTests, 'test_scope_push_uses_current_pane_settings_and_cancels_stale_rows'),
    ('caught publication assertion fails on test thread', RevisionTests,
     'test_capture_transition_between_completed_publication_and_cleanup',
     "self.assertEqual(overlay.answers['row']['answer'], 'Safe answer')",
     "self.assertEqual(overlay.answers['row']['answer'], 'injected wrong answer')",
     RevisionTests, 'test_capture_transition_between_completed_publication_and_cleanup'),
]


def run():
    for label, owner, name, old, new, test_class, test in CASES:
        output = io.StringIO()
        original = getattr(owner, name)
        with patch.object(owner, name, mutated(getattr(original, '__wrapped__', original), old, new)):
            result = unittest.TextTestRunner(stream=output).run(test_class(test))
        if result.wasSuccessful() or result.errors:
            raise AssertionError(label + ': expected assertion failure\n' + output.getvalue())
        print(label + ': RED confirmed', flush=True)

    root = Path(__file__).resolve().parent
    source = (root / 'desktop/plugin.js').read_text()
    js_cases = [
        ('capture waits for capability',
         "const pane = await claimOverlayPane(life);\n          if (!life.mounted) return false;\n          const ok = await act('/start'",
         "const pane = null;\n          const ok = await act('/start'", 'pending lifecycle'),
        ('creation is assigned before delayed GET', 'creation: life.creation',
         'creation: ++order.next', 'superseded plugin registration'),
        ('unregister never sends unscoped pause', 'for (const life of lifecycles) {',
         "api('/pause', {}).catch(() => {}); for (const life of lifecycles) {", 'pane Pause'),
        ('uncertain claim includes recovery guidance',
         ' Close and reopen this pane to retry lifecycle authorization.', '', 'failed lifecycle lookup'),
        ('empty-session source switch retires local overlay',
         'const sourceChanged = last && (', 'const sourceChanged = retained && last && (', 'empty-session source'),
        ('Clear retires local overlay', "if (path === '/clear') retireLocalOverlay();", '', 'Clear retires'),
        ('failed scope closes overlay', "const hidden = await api('/overlay/hide', { pane: life.pane, action });",
         'const hidden = {};', 'scope failure retires'),
        # Remove both invalidations, reproducing the old cache key mistake.
        ('intervening scope invalidates acknowledged key', 'life.syncedKey = null;', '', 'restoring narrow'),
    ]
    with tempfile.TemporaryDirectory(prefix='followup-mutation-', dir=root / '.test-tmp') as tmp:
        target = Path(tmp)
        (target / 'desktop').mkdir()
        shutil.copyfile(root / 'test_desktop.mjs', target / 'test_desktop.mjs')
        for label, old, new, pattern in js_cases:
            if old not in source:
                raise AssertionError('missing JS mutation target: ' + label)
            changed = source.replace(old, new)
            if label == 'intervening scope invalidates acknowledged key':
                changed = changed.replace('overlayActionAt.current.syncedKey = null;', '')
            (target / 'desktop/plugin.js').write_text(changed)
            result = subprocess.run(['node', '--test', '--test-name-pattern=' + pattern, 'test_desktop.mjs'],
                                    cwd=target, capture_output=True, text=True, timeout=20)
            output = result.stdout + result.stderr
            if result.returncode == 0 or 'AssertionError' not in output or 'TypeError' in output or 'SyntaxError' in output:
                raise AssertionError(label + ': expected assertion failure\n' + output)
            print(label + ': RED confirmed', flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FollowupTests)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(PaneOrderingTests))
    suite.addTest(ClickTests('test_scope_push_uses_current_pane_settings_and_cancels_stale_rows'))
    suite.addTest(RevisionTests('test_capture_transition_between_completed_publication_and_cleanup'))
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        raise SystemExit(1)
    subprocess.run(['node', '--test', 'test_desktop.mjs'], cwd=root, check=True)
    print('Restored follow-up production: GREEN')


if __name__ == '__main__':
    run()
