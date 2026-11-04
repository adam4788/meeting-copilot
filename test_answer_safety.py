"""Offline answer contract/privacy regression tests (synthetic diagnostics only)."""
import contextlib
import io
import json
import time
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import server

SECRET = 'synthetic-sensitive-engine-secret\nTraceback: synthetic-stack'


class AnswerSafetyTests(unittest.TestCase):
    def client(self, factory=None):
        state = server.MeetingState(engine_factory=factory)
        app = FastAPI()
        app.include_router(server.make_router(state))
        return state, TestClient(app)

    def test_missing_sentinel_is_opt_in_and_legacy_404_remains(self):
        _, client = self.client()
        with client:
            self.assertEqual(client.get('/assist/gone').status_code, 404)
            response = client.get('/assist/gone?missing=sentinel-v1')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'contract': 'answer-progress-v1', 'status': 'missing'})

    def test_actual_engine_exceptions_and_provider_diagnostics_are_not_published_or_logged(self):
        for mode in ('factory', 'answer', 'provider'):
            with self.subTest(mode=mode):
                class Engine:
                    def answer(self, *args, **kwargs):
                        if mode == 'answer':
                            raise RuntimeError(SECRET)
                        return {'answer': '', 'provider_error': SECRET}
                def factory():
                    if mode == 'factory':
                        raise RuntimeError(SECRET)
                    return Engine()
                state, client = self.client(factory)
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), client:
                    accepted = client.post('/assist', json={'question': 'Synthetic question?', 'transcript': 'Synthetic snapshot'})
                    self.assertEqual(accepted.status_code, 200)
                    status = {}
                    for _ in range(200):
                        status = client.get('/status').json()
                        if not status['busy']:
                            break
                        time.sleep(.005)
                    self.assertFalse(status['busy'])
                    entry = client.get('/assist/' + accepted.json()['request_id'] + '?missing=sentinel-v1').json()
                    self.assertEqual(entry['status'], 'error')
                    self.assertEqual(entry['request']['transcript'], 'Synthetic snapshot')
                    self.assertEqual(status['error_request'], entry['request'])
                    for payload in (entry, status):
                        text = json.dumps(payload)
                        self.assertNotIn('synthetic-sensitive', text)
                        self.assertNotIn('Traceback', text)
                        self.assertIn('Check answer setup', text)
                self.assertNotIn('synthetic-sensitive', output.getvalue())
                self.assertNotIn('Traceback', output.getvalue())

    def test_partial_stream_exception_preserves_answer_not_exception_class_or_message(self):
        private_type = type('synthetic-sensitive-exception-name', (RuntimeError,), {})
        class Engine:
            def answer_stream(self, *args, **kwargs):
                def chunks():
                    yield 'Synthetic partial answer'
                    raise private_type(SECRET)
                return {}, chunks()
        _, client = self.client(Engine)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), client:
            accepted = client.post('/assist', json={'question': 'Synthetic question?'})
            status = {}
            for _ in range(200):
                status = client.get('/status').json()
                if not status['busy']:
                    break
                time.sleep(.005)
            self.assertFalse(status['busy'])
            entry = client.get('/assist/' + accepted.json()['request_id']).json()
            self.assertEqual(entry['status'], 'done')
            self.assertEqual(entry['answer'], 'Synthetic partial answer')
            self.assertIn('Retry for the complete response', json.dumps(status))
            self.assertNotIn('synthetic-sensitive', json.dumps([entry, status]))
            self.assertNotIn('Traceback', json.dumps([entry, status]))
        self.assertEqual(output.getvalue(), '')

    def test_rejection_envelope_is_explicit_and_legacy_http_still_works(self):
        _, client = self.client()
        with client:
            self.assertEqual(client.post('/assist', json={}).status_code, 400)
            response = client.post('/assist', json={'response_contract': 'answer-admission-v1'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'contract': 'answer-admission-v1', 'accepted': False, 'reason': 'invalid_request'})
