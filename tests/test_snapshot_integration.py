"""Synthetic provider-wire and backend-lifecycle proof; no devices or network."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from knowledge import KnowledgeEngine
from server import MeetingState, make_router, _AnswerRejected


class SnapshotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wire = []

    def state(self, provider):
        engine = KnowledgeEngine(Path(self.temp.name), _completion=provider, _history=lambda *args, **kwargs: [], _standalone=True)
        state = MeetingState(engine_factory=lambda: engine)
        state.backend = 'native'
        return state

    def wait(self, state):
        end = time.monotonic() + 4
        while state.live_workers and time.monotonic() < end:
            time.sleep(.005)
        self.assertEqual(state.live_workers, 0)

    def provider(self, messages):
        self.wire.append(json.loads(messages[-1]['content']))
        return 'Review the original excerpt [T1].'

    def test_completed_wire_snapshot_and_replay_are_exact_despite_correction(self):
        state = self.state(self.provider)
        state.backend = 'meetily'
        state.add_segment(dict(id='meetily:fictional:1', speaker='meeting', text='Original Friday proposal', timestamp=12))
        admitted = state.assist('What was proposed?')
        self.wait(state)
        first = state.result
        snap = first['transcript_snapshot']
        self.assertEqual(snap['provider_json'], self.wire[0]['transcript'])
        self.assertEqual(snap['segments'][0]['timestamp_kind'], 'meetily_audio_start_time_uninterpreted')
        self.assertIn('Original Friday proposal', snap['provider_json'])
        state.add_segment(dict(id='meetily:fictional:1', speaker='meeting', text='Corrected Monday proposal', timestamp=12))
        state.assist(**first['request'])
        self.wait(state)
        self.assertEqual(self.wire[0]['transcript'], self.wire[1]['transcript'])
        self.assertEqual(state.result['transcript_snapshot']['snapshot_id'], snap['snapshot_id'])
        state.assist('What changed?')
        self.wait(state)
        self.assertIn('Corrected Monday', self.wire[2]['transcript'])
        self.assertNotEqual(snap['snapshot_id'], state.result['transcript_snapshot']['snapshot_id'])
        self.assertEqual(state.assist_request(admitted['request_id'])['transcript_snapshot'], snap)

    def test_replay_changes_and_cross_clear_context_fail_before_provider(self):
        state = self.state(self.provider)
        state.add_segment(dict(text='Original', timestamp=100))
        state.assist('Question')
        self.wait(state)
        req = dict(state.result['request'])
        with self.assertRaises(_AnswerRejected):
            state.assist(**dict(req, transcript='altered'))
        with self.assertRaises(_AnswerRejected):
            state.assist(**dict(req, answer_provider='openai', answer_model='gpt-test'))
        app = FastAPI(); app.include_router(make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/clear').status_code, 200)
        self.assertEqual(state.transcript_snapshots, {})
        with self.assertRaises(_AnswerRejected):
            state.assist(**req)
        self.assertEqual(len(self.wire), 1)

    def test_clear_suppresses_late_provider_and_releases_registry(self):
        entered, release = threading.Event(), threading.Event()
        def complete(messages):
            self.provider(messages); entered.set(); release.wait(3)
            return 'Late [T1]'
        state = self.state(complete)
        state.add_segment(dict(text='Sensitive old meeting'))
        state.assist('Question')
        try:
            self.assertTrue(entered.wait(2))
            app = FastAPI(); app.include_router(make_router(state))
            with TestClient(app) as client:
                self.assertEqual(client.post('/clear').status_code, 200)
            self.assertEqual(state.transcript_snapshots, {})
        finally:
            release.set(); self.wait(state)
        self.assertIsNone(state.result)
        self.assertEqual(state.requests, {})

    def test_stream_and_partial_stream_keep_exact_snapshot_and_flag_bad_refs(self):
        for partial in (False, True):
            outer = self
            class Provider:
                def __call__(self, messages):
                    raise AssertionError('unexpected completed fallback')
                def stream_chunks(self, messages):
                    outer.wire.append(json.loads(messages[-1]['content']))
                    yield 'Answer [T99]'
                    if partial:
                        raise RuntimeError('synthetic interruption')
            state = self.state(Provider())
            state.add_segment(dict(text='Current context', timestamp=100))
            state.assist('Question'); self.wait(state)
            self.assertIsNotNone(state.result, state.error)
            self.assertEqual(state.result['transcript_snapshot']['provider_json'], self.wire[-1]['transcript'])
            self.assertTrue(any('transcript references are invalid' in w for w in state.result['warnings']))
            if partial:
                self.assertTrue(any('partial answer' in w for w in state.result['warnings']))

    def test_plain_text_retry_does_not_claim_structured_provenance(self):
        state = self.state(self.provider)
        state.add_segment(dict(text='Unrelated live context'))
        state.assist('Question', transcript='Explicit plain text context')
        self.wait(state)
        self.assertIsNone(state.result['transcript_snapshot'])
        self.assertEqual(self.wire[-1]['transcript'], 'Explicit plain text context')
        self.assertNotIn('replay_request_id', state.result['request'])

    def test_provider_error_never_claims_completed_context_result(self):
        def fail(messages):
            self.provider(messages)
            raise RuntimeError('synthetic failure')
        state = self.state(fail)
        state.add_segment(dict(text='Original'))
        state.assist('Question'); self.wait(state)
        self.assertIsNone(state.result)
        self.assertTrue(state.error)
        self.assertIsNone(next(iter(state.requests.values())).get('transcript_snapshot'))

    def test_long_context_matches_exact_budgeted_provider_text(self):
        state = self.state(self.provider)
        for i in range(100):
            state.add_segment(dict(text=str(i)+' '+('word '*700), timestamp=i))
        state.assist('Question'); self.wait(state)
        snap = state.result['transcript_snapshot']
        self.assertEqual(snap['provider_json'], self.wire[-1]['transcript'])
        self.assertLessEqual(len(snap['provider_json']), 6000)
        self.assertGreater(snap['omitted_segments'], 0)
        self.assertEqual(json.loads(snap['provider_json'])['segments'], snap['segments'])

    def test_new_meeting_clears_snapshots_and_rejects_old_replay(self):
        class Capture:
            def start(self, on_segment, on_status=None): pass
            def stop(self): pass
            def status(self): return {'running': False}
        state = self.state(self.provider)
        state.audio_factory = lambda **kwargs: Capture()
        state.add_segment(dict(text='Previous meeting', timestamp=100))
        state.assist('Question'); self.wait(state)
        req = dict(state.result['request'])
        previous_generation = state.generation
        state.start(True, backend='native', new_meeting=True)
        try:
            self.assertGreater(state.generation, previous_generation)
            self.assertEqual(state.transcript_snapshots, {})
            self.assertEqual(state.requests, {})
            with self.assertRaises(_AnswerRejected):
                state.assist(**req)
        finally:
            state.pause()

    def test_snapshot_registry_is_bounded_with_request_eviction(self):
        state = self.state(self.provider)
        state.add_segment(dict(text='Current meeting', timestamp=100))
        for i in range(12):
            state.assist('Question '+str(i)); self.wait(state)
        self.assertLessEqual(len(state.transcript_snapshots), 8)
        self.assertEqual(set(state.transcript_snapshots), set(state.requests))
        old = next(iter(state.requests.values()))['request']
        # Removing metadata from a replay is not permission to reconstruct it
        # from the current live buffer; legacy/plain text has no mapped snapshot.
        old = dict(old); old.pop('replay_request_id')
        state.assist(**old); self.wait(state)
        self.assertIsNone(state.result['transcript_snapshot'])

    def test_ingestion_fallback_clock_is_not_exported_as_capture_time(self):
        state = self.state(self.provider)
        state.add_segment(dict(text='No timing supplied'))
        self.assertIsInstance(state.segments[0]['timestamp'], (int, float))
        state.assist('Question'); self.wait(state)
        row = state.result['transcript_snapshot']['segments'][0]
        self.assertIsNone(row['timestamp'])
        self.assertEqual(row['timestamp_kind'], 'unavailable')

    def test_optional_web_planner_uses_same_frozen_window_as_final_generation(self):
        for streaming in (False, True):
            calls = []
            class Provider:
                def __call__(self, messages):
                    payload = json.loads(messages[-1]['content']); calls.append(payload)
                    if 'sources' in payload:
                        return '{"needs_web":false,"query":""}'
                    return 'Final [T1]'
                def stream_chunks(self, messages):
                    if not streaming:
                        raise NotImplementedError('synthetic completed-only')
                    calls.append(json.loads(messages[-1]['content']))
                    return iter(['Final [T1]'])
            provider = Provider()
            if not streaming:
                # Plain callable has no stream capability, but still plans once.
                provider = provider.__call__
            state = self.state(provider)
            for i in range(25):
                state.add_segment(dict(text='Segment '+str(i)+' '+('word '*80), timestamp=i))
            state.assist('Question', web_fallback=True); self.wait(state)
            self.assertIsNotNone(state.result, state.error)
            self.assertEqual(len(calls), 2)
            snap = state.result['transcript_snapshot']['provider_json']
            self.assertEqual(calls[0]['transcript'], snap)
            self.assertEqual(calls[1]['transcript'], snap)
