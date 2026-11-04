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

    def test_validation_errors_never_echo_sensitive_body_or_diagnostic(self):
        _, client = self.client()
        marker = 'synthetic-sensitive-meeting-question'
        with client:
            for path, body in [('/assist', {'question': marker * 200}),
                               ('/assist', {'transcript': marker * 1000}),
                               ('/assist', {'question': {'private': marker}}),
                               ('/assist/cancel', {'request_id': marker * 20}),
                               ('/start', {'consent': {'private': marker}})]:
                with self.subTest(path=path, body_type=type(body)):
                    response = client.post(path, json=body)
                    self.assertEqual(response.status_code, 422)
                    host_error = f'HTTP {response.status_code} {response.text}'
                    self.assertNotIn(marker, host_error)
                    self.assertNotIn('input', response.text)
                    self.assertLess(len(response.text), 300)
            response = client.post('/assist', content='{"question":"' + marker,
                                   headers={'content-type': 'application/json'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(marker, response.text)

    def test_real_knowledge_engine_diagnostics_are_fixed_across_public_surfaces(self):
        from knowledge import KnowledgeEngine
        import tempfile
        from unittest.mock import patch
        private_type = type('synthetic-sensitive-exception-name', (RuntimeError,), {})
        def fail(*args, **kwargs):
            raise private_type(SECRET)
        for streaming in (False, True):
            with self.subTest(stream=streaming), tempfile.TemporaryDirectory() as home:
                class Completion:
                    supports_streaming = streaming
                    def __call__(self, *args, **kwargs): return fail()
                    def stream(self, *args, **kwargs): return fail()
                engine = KnowledgeEngine(project_root=home, hermes_home=home,
                                         _completion=Completion(), _search=fail, _history=None, _standalone=True)
                state, client = self.client(lambda: engine)
                output = io.StringIO()
                with patch.object(engine, 'search', return_value=[]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), client:
                    accepted = client.post('/assist', json={'question': 'Synthetic question?', 'web_query': 'public topic'})
                    for _ in range(200):
                        status = client.get('/status').json()
                        if not status['busy']: break
                        time.sleep(.005)
                    self.assertFalse(status['busy'])
                    entry = client.get('/assist/' + accepted.json()['request_id']).json()
                    combined = json.dumps([entry, status]) + output.getvalue()
                    self.assertNotIn('synthetic-sensitive', combined)
                    self.assertNotIn('Traceback', combined)
                    self.assertIn('Check answer setup', combined)
                    self.assertIn('Web search failed', combined)
                    self.assertNotIn('HTTP 401', combined)

    def test_capture_error_owns_status_not_stale_answer_retry(self):
        state, client = self.client()
        state.capture_error = 'Capture failed. Restart capture.'
        state.error_request = {'question': 'stale context'}
        with client:
            status = client.get('/status').json()
            self.assertEqual(status.get('error_source'), 'capture')
            self.assertIsNone(status['error_request'])

    def test_real_engine_success_keeps_generic_web_warning_and_history_failure_is_private(self):
        from knowledge import KnowledgeEngine
        from unittest.mock import patch
        import tempfile
        private_type = type('synthetic-sensitive-exception-name', (RuntimeError,), {})
        def fail(*args, **kwargs):
            raise private_type(SECRET)
        with tempfile.TemporaryDirectory() as home:
            engine = KnowledgeEngine(project_root=home, hermes_home=home, _standalone=True,
                                     _completion=lambda *a, **k: 'Synthetic answer', _search=fail)
            _, client = self.client(lambda: engine)
            output = io.StringIO()
            with patch.object(engine, 'search', return_value=[]), client, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                accepted = client.post('/assist', json={'question': 'Synthetic question?', 'web_query': 'public topic'})
                status = {}
                for _ in range(200):
                    status = client.get('/status').json()
                    if not status['busy']: break
                    time.sleep(.005)
                self.assertFalse(status['busy'])
                entry = client.get('/assist/' + accepted.json()['request_id']).json()
                self.assertEqual(entry['status'], 'done')
                self.assertIn('Web search failed; using local evidence only.', entry['warnings'])
                self.assertEqual(status['recent'][0]['warnings'], entry['warnings'])
                self.assertNotIn('synthetic-sensitive', json.dumps([entry, status]) + output.getvalue())
            history_engine = KnowledgeEngine(project_root=home, hermes_home=home, _standalone=True,
                                             _completion=lambda *a, **k: 'unused', _history=fail)
            with self.assertRaisesRegex(RuntimeError, '^History provider failed\\.$'):
                history_engine.search('Synthetic question?')

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
