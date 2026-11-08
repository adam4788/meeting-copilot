"""Offline citation/warning regressions against the real answer pipeline."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge import KnowledgeEngine
from server import MeetingState, make_router
from transcript_snapshot import build_snapshot


NUMERIC_WARNING = 'Answer citations are missing or invalid; verify against the listed evidence.'
TRANSCRIPT_WARNING = 'Answer transcript references are invalid; verify against the sent context.'
NO_EVIDENCE_WARNING = 'No matching local/session evidence found; transcript alone is not independent verification.'


class SyntheticProvider:
    def __init__(self, answer, failure=None):
        self.answer = answer
        self.failure = failure
        self.wire = []

    def __call__(self, messages):
        self.wire.append(json.loads(messages[-1]['content']))
        return self.answer

    def stream_chunks(self, messages):
        self.wire.append(json.loads(messages[-1]['content']))
        yield self.answer
        if self.failure:
            raise self.failure('synthetic-private-diagnostic')


class FallbackProvider(SyntheticProvider):
    def __init__(self, answer, *, setup):
        super().__init__(answer)
        self.setup = setup

    def stream_chunks(self, messages):
        self.wire.append(json.loads(messages[-1]['content']))
        if self.setup:
            raise NotImplementedError('synthetic-private-diagnostic')
        def chunks():
            raise NotImplementedError('synthetic-private-diagnostic')
            yield  # make this a lazy iterator
        return chunks()


class CitationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'alpha').mkdir()
        (self.root / 'alpha' / 'plan.txt').write_text(
            'The synthetic release plan is Monday.', encoding='utf-8')

    def ask(self, provider, *, project=True, segments=(), transcript=None, completed=False,
            engine_class=KnowledgeEngine):
        completion = provider.__call__ if completed else provider
        engine = engine_class(self.root, _completion=completion,
                              _history=lambda *a, **k: [], _standalone=True)
        state = MeetingState(engine_factory=lambda: engine)
        state.backend = 'native'
        for segment in segments:
            state.add_segment({'text': segment})
        app = FastAPI()
        app.include_router(make_router(state))
        request: dict = {'question': 'What is the synthetic release plan?' if project else 'unfindablesyntheticterm?'}
        if project:
            request['project'] = 'alpha'
            request['include_history'] = False
        if transcript is not None:
            request['transcript'] = transcript
        with TestClient(app) as client:
            admitted = client.post('/assist', json=request)
            self.assertEqual(admitted.status_code, 200, admitted.text)
            request_id = admitted.json()['request_id']
            deadline = time.monotonic() + 5
            while True:
                entry = client.get('/assist/' + request_id).json()
                if entry['status'] in ('done', 'error', 'cancelled'):
                    break
                self.assertLess(time.monotonic(), deadline, entry)
                time.sleep(.005)
            self.assertEqual(entry['status'], 'done', entry)
            result = client.get('/status').json()['result']
            self.assertIsNotNone(result)
            self.assertEqual(entry['warnings'], result['warnings'])
            return result, entry

    def test_interrupted_stream_validates_numeric_citations_on_displayed_partial(self):
        provider = SyntheticProvider('Synthetic claim [99].', failure=RuntimeError)
        result, entry = self.ask(provider)
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(result['answer'], 'Synthetic claim [99].')
        self.assertIn('partial answer', ' '.join(result['warnings']))
        self.assertIn(NUMERIC_WARNING, result['warnings'])
        self.assertNotIn('synthetic-private-diagnostic', json.dumps([entry, result]))

    def test_invented_references_are_invalid_with_no_evidence_or_snapshot(self):
        for mode in ('completed', 'stream', 'interrupted'):
            with self.subTest(mode=mode):
                provider = SyntheticProvider('Synthetic claim [1], attributed to [T1].',
                                             RuntimeError if mode == 'interrupted' else None)
                result, _ = self.ask(provider, project=False, completed=mode == 'completed')
                self.assertEqual(result['sources'], [])
                self.assertIsNone(result['transcript_snapshot'])
                self.assertEqual(provider.wire[0]['evidence'], [])
                self.assertEqual(provider.wire[0]['transcript'], '')
                for warning in (NUMERIC_WARNING, TRANSCRIPT_WARNING, NO_EVIDENCE_WARNING):
                    with self.subTest(warning=warning):
                        self.assertIn(warning, result['warnings'])
                        self.assertEqual(result['warnings'].count(warning), 1)

    def test_extremely_long_numeric_reference_does_not_turn_completed_answer_into_error(self):
        provider = SyntheticProvider('Synthetic claim [' + '9' * 5000 + '].')
        result, _ = self.ask(provider, completed=True)
        self.assertIn(NUMERIC_WARNING, result['warnings'])
        self.assertEqual(result['answer'], provider.answer)

    def test_interrupted_stream_reports_the_existing_answer_cap(self):
        provider = SyntheticProvider('Synthetic [99]. ' + 'x' * 12000, RuntimeError)
        result, _ = self.ask(provider)
        self.assertEqual(len(result['answer']), 12000)
        self.assertIn(NUMERIC_WARNING, result['warnings'])
        self.assertTrue(any('12,000-character' in warning for warning in result['warnings']))

    def test_reference_validation_matrix_across_answer_modes(self):
        # A snapshot ref establishes only which excerpt was supplied, not its truth.
        cases = [
            ('valid-both', 'Claim [1] [T1].', True, True, False, False, False),
            ('invalid-both', 'Claim [0] [T99].', True, True, True, True, False),
            ('missing-numeric', 'Claim [T1].', True, True, True, False, False),
            ('valid-transcript-only', 'Claim [T1].', False, True, False, False, True),
            ('no-refs-no-evidence', 'Insufficient evidence.', False, False, False, False, True),
            ('out-of-range', 'Claim [2].', True, False, True, False, False),
        ]
        for mode in ('completed', 'stream', 'interrupted'):
            for name, answer, project, has_snapshot, bad_numeric, bad_transcript, no_evidence in cases:
                with self.subTest(mode=mode, case=name):
                    provider = SyntheticProvider(answer, RuntimeError if mode == 'interrupted' else None)
                    result, _ = self.ask(provider, project=project,
                                         segments=('zyxmeetingcontext quxunicorn',) if has_snapshot else (),
                                         completed=mode == 'completed')
                    self.assertEqual(len(result['sources']), int(project))
                    self.assertEqual(NUMERIC_WARNING in result['warnings'], bad_numeric)
                    self.assertEqual(TRANSCRIPT_WARNING in result['warnings'], bad_transcript)
                    self.assertEqual(NO_EVIDENCE_WARNING in result['warnings'], no_evidence)
                    self.assertEqual(any('partial answer' in w for w in result['warnings']),
                                     mode == 'interrupted')
                    if has_snapshot:
                        self.assertEqual(result['transcript_snapshot']['provider_json'], provider.wire[0]['transcript'])
                    else:
                        self.assertIsNone(result['transcript_snapshot'])

    def test_plain_transcript_is_not_a_structured_snapshot(self):
        for mode in ('completed', 'stream', 'interrupted'):
            with self.subTest(mode=mode):
                provider = SyntheticProvider('Claim [1] [T1].', RuntimeError if mode == 'interrupted' else None)
                result, _ = self.ask(provider, segments=('unused live context',),
                                     transcript='unstructured zyx context', completed=mode == 'completed')
                self.assertEqual(provider.wire[0]['transcript'], 'unstructured zyx context')
                self.assertIsNone(result['transcript_snapshot'])
                self.assertIn(TRANSCRIPT_WARNING, result['warnings'])
                self.assertNotIn(NUMERIC_WARNING, result['warnings'])

    def test_unpassed_snapshot_cannot_validate_legacy_engine_transcript_refs(self):
        class LegacyEngine(KnowledgeEngine):
            supports_transcript_snapshot = False
        for mode in ('completed', 'stream', 'interrupted'):
            with self.subTest(mode=mode):
                provider = SyntheticProvider('Claim [1] [T1].', RuntimeError if mode == 'interrupted' else None)
                result, _ = self.ask(provider, segments=('zyx meeting context',), engine_class=LegacyEngine,
                                     completed=mode == 'completed')
                self.assertIn('zyx meeting context', provider.wire[0]['transcript'])
                self.assertIsNone(result['transcript_snapshot'])
                self.assertIn(TRANSCRIPT_WARNING, result['warnings'])
                self.assertNotIn(NUMERIC_WARNING, result['warnings'])

    def test_empty_snapshot_does_not_make_transcript_reference_valid(self):
        snapshot = build_snapshot([], backend='native', clean=lambda value: value)
        provider = SyntheticProvider('Claim [1] [T1].')
        engine = KnowledgeEngine(self.root, _completion=provider.__call__, _standalone=True)
        with patch.object(engine, 'search', return_value=[]):
            result = engine.answer('synthetic question', transcript_snapshot=snapshot)
        self.assertEqual(provider.wire[0]['transcript'], snapshot.provider_json)
        self.assertEqual(result['transcript_snapshot']['segments'], [])
        for warning in (NUMERIC_WARNING, TRANSCRIPT_WARNING, NO_EVIDENCE_WARNING):
            self.assertEqual(result['warnings'].count(warning), 1)

    def test_lazy_not_implemented_after_partial_does_not_fall_back(self):
        provider = SyntheticProvider('Partial claim [99] [T1].', NotImplementedError)
        result, _ = self.ask(provider, project=False)
        self.assertEqual(len(provider.wire), 1)
        self.assertTrue(result['streaming'])
        for warning in (NUMERIC_WARNING, TRANSCRIPT_WARNING, NO_EVIDENCE_WARNING):
            self.assertEqual(result['warnings'].count(warning), 1)
        self.assertIn('partial answer', ' '.join(result['warnings']))

    def test_lazy_attribute_error_after_partial_does_not_fall_back(self):
        class AttributeErrorEngine(KnowledgeEngine):
            def answer_stream(self, *args, **kwargs):
                def chunks():
                    yield 'Partial claim [1] [T1].'
                    raise AttributeError('synthetic-private-diagnostic')
                return {'sources': [], 'warnings': []}, chunks()
            def answer(self, *args, **kwargs):
                raise AssertionError('A partial stream must not trigger fallback')
        provider = SyntheticProvider('unused')
        result, entry = self.ask(provider, project=False, engine_class=AttributeErrorEngine)
        self.assertEqual(result['answer'], 'Partial claim [1] [T1].')
        for warning in (NUMERIC_WARNING, TRANSCRIPT_WARNING, NO_EVIDENCE_WARNING):
            self.assertEqual(result['warnings'].count(warning), 1)
        self.assertIn('partial answer', ' '.join(result['warnings']))
        self.assertNotIn('synthetic-private-diagnostic', json.dumps([entry, result]))

    def test_stream_setup_and_lazy_fallback_validate_once_without_duplicate_warning(self):
        for setup in (True, False):
            for project in (True, False):
                with self.subTest(setup=setup, project=project):
                    provider = FallbackProvider('Fallback claim [99] [T1].', setup=setup)
                    result, _ = self.ask(provider, project=project)
                    self.assertEqual(len(provider.wire), 2)
                    self.assertEqual(provider.wire[0], provider.wire[1])
                    self.assertFalse(result['streaming'])
                    self.assertTrue(result['completed_only'])
                    self.assertNotIn('partial answer', ' '.join(result['warnings']))
                    self.assertEqual(result['warnings'].count(NUMERIC_WARNING), 1)
                    self.assertEqual(result['warnings'].count(TRANSCRIPT_WARNING), 1)
                    self.assertEqual(result['warnings'].count(NO_EVIDENCE_WARNING), int(not project))

    def test_stream_header_reports_evidence_after_optional_web_retrieval(self):
        for has_web in (False, True):
            with self.subTest(has_web=has_web):
                provider = SyntheticProvider('Synthetic answer [1].')
                engine = KnowledgeEngine(self.root, _completion=provider, _standalone=True)
                web_sources = ([{'id': 'web:synthetic', 'title': 'synthetic', 'text': 'fixture'}]
                               if has_web else [])
                with patch.object(engine, 'search', return_value=[]), \
                     patch.object(engine, '_web', return_value=web_sources):
                    header, chunks = engine.answer_stream('synthetic question', web_query='public fixture')
                    self.assertEqual(list(chunks), ['Synthetic answer [1].'])
                self.assertEqual(header['sources'], web_sources)
                self.assertEqual(NO_EVIDENCE_WARNING in header['warnings'], not has_web)
                self.assertEqual(len(provider.wire[0]['evidence']), int(has_web))

    def test_large_and_leading_zero_references_are_checked_in_each_mode(self):
        for mode in ('completed', 'stream', 'interrupted'):
            for digits, invalid in (('9' * 5000, True), ('0' * 5000 + '1', False), ('０１', False)):
                with self.subTest(mode=mode, invalid=invalid, length=len(digits)):
                    provider = SyntheticProvider('Claim [' + digits + '].',
                                                 RuntimeError if mode == 'interrupted' else None)
                    result, _ = self.ask(provider, completed=mode == 'completed')
                    self.assertEqual(result['answer'], provider.answer)
                    self.assertEqual(NUMERIC_WARNING in result['warnings'], invalid)

    def test_cancelled_stream_does_not_publish_a_result_or_late_delta(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingProvider(SyntheticProvider):
            def stream_chunks(self, messages):
                self.wire.append(json.loads(messages[-1]['content']))
                yield 'Partial [99].'
                entered.set()
                release.wait(3)
                yield 'Late response.'
        provider = BlockingProvider('unused')
        engine = KnowledgeEngine(self.root, _completion=provider, _standalone=True)
        state = MeetingState(engine_factory=lambda: engine)
        request_id = state.assist('What is the synthetic release plan?', project='alpha',
                                  include_history=False)['request_id']
        try:
            self.assertTrue(entered.wait(2))
            current = state.assist_request(request_id)
            assert current is not None
            self.assertEqual(current['answer'], 'Partial [99].')
            self.assertEqual(state.cancel_assist(request_id)['status'], 'cancelled')
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while state.live_workers and time.monotonic() < deadline:
                time.sleep(.005)
        self.assertEqual(state.live_workers, 0)
        current = state.assist_request(request_id)
        assert current is not None
        self.assertEqual(current['status'], 'cancelled')
        self.assertNotIn('Late response', current['answer'])
        self.assertIsNone(state.result)
