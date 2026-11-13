"""Read-only answer mirroring, synthetic state only; no model or device calls."""
import unittest
from unittest.mock import patch
import server
from overlay_bridge import BackendTransport, OverlayBridge


class OverlayFocusTests(unittest.TestCase):
    def setUp(self):
        self.state = server.MeetingState()
        self.bridge = OverlayBridge(transport=BackendTransport(self.state))

    def entry(self, key='one', **changes):
        self.state.answer_revision += 1
        value = dict(answer_revision=self.state.answer_revision, request_id=key, question='When is review?', answer='Friday.',
                     status='done', generation=self.state.generation, streaming=False,
                     warnings=[], sources=[{'title': 'Plan', 'text': 'PRIVATE EXCERPT'}],
                     request={'transcript': 'PRIVATE CONTEXT'}, error='PRIVATE ERROR')
        value.update(changes)
        self.state.requests[key] = value
        return value

    def snapshot(self):
        self.bridge.update(self.state.status(touch=False))
        return self.bridge.pushed[-1]

    def test_mirrors_only_allowlisted_display_fields_without_admission(self):
        self.entry()
        with patch.object(self.state, 'assist', side_effect=AssertionError('no model request')):
            result = self.snapshot()['focus_answer']
        self.assertEqual(result['answer'], 'Friday.')
        self.assertEqual(result['sources'], ['Plan'])
        self.assertNotIn('PRIVATE', str(result))
        self.assertFalse(self.bridge.pushed[-1]['answer_actions'])

    def test_latest_request_supersedes_previous_even_while_empty(self):
        self.entry()
        self.entry('two', status='queued', answer='')
        result = self.snapshot()['focus_answer']
        self.assertEqual(result['id'], 'two')
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['answer'], '')

    def test_streaming_then_cancelled_keeps_partial_with_honest_status(self):
        entry = self.entry(status='streaming', streaming=True, answer='Partial')
        self.assertEqual(self.snapshot()['focus_answer']['status'], 'streaming')
        entry.update(status='cancelled', cancelled=True)
        result = self.snapshot()['focus_answer']
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['answer'], 'Partial')

    def test_completed_partial_stream_is_labelled_interrupted(self):
        self.entry(status='done', warnings=['Stream interrupted; showing a partial answer. Retry for the complete response.'])
        self.assertEqual(self.snapshot()['focus_answer']['status'], 'interrupted')

    def test_oversized_multibyte_snapshot_stays_bounded(self):
        self.entry(answer='😀' * 13000, question='😀' * 4000)
        self.state.segments = [{'id': str(i), 'speaker': 'remote', 'text': 'What ' + str(i) + '😀' * 200 + '?'} for i in range(20)]
        result = self.snapshot()
        import json
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), self.bridge._SNAPSHOT_MAX_BYTES)
        self.assertTrue(result['truncated'])
        self.assertIn('shortened', result['focus_answer']['warnings'][0])

    def test_new_generation_and_clear_remove_answer(self):
        self.entry()
        self.snapshot()
        self.state.generation += 1
        self.assertIsNone(self.snapshot()['focus_answer'])
        self.entry('two')
        self.snapshot()
        self.state.requests.clear()
        self.assertIsNone(self.snapshot()['focus_answer'])

    def test_stale_status_cannot_restore_previous_response(self):
        self.entry()
        stale = self.state.status(touch=False)
        self.state.generation += 1
        self.snapshot()
        self.assertFalse(self.bridge.update(stale))
        self.assertIsNone(self.bridge.pushed[-1]['focus_answer'])

    def test_display_bounds_and_truncation_are_explicit(self):
        self.entry(answer='a' * 13000, question='q' * 5000,
                   warnings=['warning'] * 50, sources=[{'title': 't' * 100}] * 20)
        result = self.snapshot()['focus_answer']
        self.assertLessEqual(len(result['answer']), 12000)
        self.assertLessEqual(len(result['question']), 300)
        self.assertTrue(any('shortened' in w for w in result['warnings']))
        self.assertLessEqual(len(result['sources']), 3)

    def test_scope_retirement_clears_display_immediately(self):
        self.entry()
        self.snapshot()
        self.bridge.retire_authorization()
        self.bridge.push()
        self.assertIsNone(self.bridge.pushed[-1]['focus_answer'])
        self.assertIsNone(self.snapshot()['focus_answer'])
        self.entry('new')
        self.assertEqual(self.snapshot()['focus_answer']['id'], 'new')

    def test_scope_retires_even_an_unobserved_admission_and_late_completion(self):
        entry = self.entry(status='queued')
        self.bridge.set_scope({'project': 'different'})
        entry.update(status='done')
        self.assertIsNone(self.snapshot()['focus_answer'])
        self.entry('new')
        self.assertEqual(self.snapshot()['focus_answer']['id'], 'new')

if __name__ == '__main__':
    unittest.main()
