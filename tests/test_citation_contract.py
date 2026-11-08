"""Citation warnings must remain honest on every answer path (synthetic only)."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from knowledge import KnowledgeEngine
from server import MeetingState, make_router
from transcript_snapshot import build_snapshot, reference_warnings


class StreamingProvider:
    def __init__(self, answer, partial=False):
        self.answer, self.partial, self.messages = answer, partial, []

    def __call__(self, messages):
        raise AssertionError('unexpected completed fallback')

    def stream_chunks(self, messages):
        self.messages.append(json.loads(messages[-1]['content']))
        yield self.answer
        if self.partial:
            raise RuntimeError('synthetic interruption')


class CitationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mc-citations-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'alpha').mkdir()
        (self.root / 'alpha' / 'plan.txt').write_text('The synthetic release plan is Monday.', encoding='utf-8')

    def ask(self, provider, *, project):
        engine = KnowledgeEngine(self.root, _completion=provider,
                                 _history=lambda *args, **kwargs: [], _standalone=True)
        app = FastAPI()
        app.include_router(make_router(MeetingState(engine_factory=lambda: engine)))
        body: dict[str, object] = {'question': 'What is the synthetic release plan?' if project else 'unfindablesyntheticterm?'}
        if project:
            body.update(project='alpha', include_history=False)
        with TestClient(app) as client:
            admitted = client.post('/assist', json=body)
            self.assertEqual(admitted.status_code, 200, admitted.text)
            request_id = admitted.json()['request_id']
            deadline = time.monotonic() + 5
            while True:
                entry = client.get('/assist/' + request_id).json()
                if entry['status'] in ('done', 'error', 'cancelled'):
                    break
                self.assertLess(time.monotonic(), deadline, entry)
                time.sleep(.01)
            self.assertEqual(entry['status'], 'done', entry)
            return client.get('/status').json()['result']

    def test_complete_stream_flags_out_of_range_numeric_citation(self):
        result = self.ask(StreamingProvider('Synthetic claim [99].'), project=True)
        self.assertEqual(len(result['sources']), 1)
        self.assertIn('citations are missing or invalid', ' '.join(result['warnings']))

    def test_interrupted_stream_flags_out_of_range_numeric_citation(self):
        result = self.ask(StreamingProvider('Synthetic claim [99].', partial=True), project=True)
        self.assertEqual(len(result['sources']), 1)
        self.assertIn('partial answer', ' '.join(result['warnings']))
        self.assertIn('citations are missing or invalid', ' '.join(result['warnings']))

    def test_no_evidence_and_no_snapshot_flags_invented_references(self):
        provider = StreamingProvider('Synthetic claim [1], attributed to [T1].')
        result = self.ask(provider, project=False)
        self.assertEqual(result['sources'], [])
        self.assertIsNone(result['transcript_snapshot'])
        self.assertEqual(provider.messages[0]['evidence'], [])
        self.assertEqual(provider.messages[0]['transcript'], '')
        for expected in ('citations are missing or invalid', 'transcript references are invalid',
                         'No matching local/session evidence found'):
            self.assertIn(expected, ' '.join(result['warnings']))

    def test_interrupted_stream_without_evidence_flags_every_invented_reference(self):
        result = self.ask(StreamingProvider('A claim [1] and [T1].', partial=True), project=False)
        warnings = ' '.join(result['warnings'])
        self.assertIn('partial answer', warnings)
        self.assertIn('citations are missing or invalid', warnings)
        self.assertIn('transcript references are invalid', warnings)
        self.assertIn('No matching local/session evidence found', warnings)
        self.assertEqual(sum('No matching local/session evidence found' in w for w in result['warnings']), 1)

    def test_valid_citation_is_not_flagged_on_complete_or_partial_stream(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                result = self.ask(StreamingProvider('Synthetic release [1].', partial=partial), project=True)
                self.assertNotIn('citations are missing or invalid', ' '.join(result['warnings']))
                self.assertEqual(any('partial answer' in w for w in result['warnings']), partial)

    def test_completed_answer_warns_for_references_without_evidence(self):
        engine = KnowledgeEngine(self.root, _completion=lambda _: 'Invented [1] [T1].',
                                 _history=lambda *args, **kwargs: [], _standalone=True)
        result = engine.answer('unfindablesyntheticterm?')
        self.assertIn('citations are missing or invalid', ' '.join(result['warnings']))
        self.assertIn('transcript references are invalid', ' '.join(result['warnings']))
        self.assertIn('No matching local/session evidence found', ' '.join(result['warnings']))

    def test_valid_and_missing_references_are_distinct(self):
        snapshot = build_snapshot([{'speaker': 'Synthetic', 'text': 'Synthetic words.'}],
                                  backend='native', clean=lambda x: x)
        self.assertEqual(reference_warnings('Supported [1] and [T1].', 1, snapshot), [])
        self.assertIn('citations are missing or invalid', ' '.join(reference_warnings('No reference.', 1, snapshot)))
        self.assertNotIn('citations are missing or invalid', ' '.join(reference_warnings('No reference.', 0, None)))
        self.assertIn('transcript references are invalid', ' '.join(reference_warnings('Wrong [T2].', 0, snapshot)))
        self.assertIn('transcript references are invalid', ' '.join(reference_warnings('Wrong [T1].', 0, None)))

    def test_large_numeric_reference_cannot_crash_warning_validation(self):
        result = self.ask(StreamingProvider('Invalid [' + '9' * 5000 + '].'), project=True)
        self.assertIn('citations are missing or invalid', ' '.join(result['warnings']))


if __name__ == '__main__':
    unittest.main()
