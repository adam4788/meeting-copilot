"""Authorized production routes must not publish adapter exception diagnostics."""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import server

MARKER = 'synthetic-sensitive-adapter-diagnostic'


class LifecycleDiagnosticTests(unittest.TestCase):
    def test_authorized_adapter_errors_are_actionable_without_raw_text(self):
        for action in ('start', 'pause', 'clear', 'clear-overlay', 'clear-invalidation'):
            with self.subTest(action=action), ExitStack() as stack:
                class Audio:
                    running = False
                    def start(self, **kwargs):
                        if action == 'start':
                            raise RuntimeError(MARKER)
                        self.running = True
                    def stop(self):
                        if action == 'pause':
                            raise RuntimeError(MARKER)
                        self.running = False
                    def status(self):
                        return {'running': self.running, 'stop_confirmed': not self.running}
                audio = Audio()
                state = server.MeetingState(audio_factory=lambda **kw: audio)
                stack.enter_context(patch.object(state, 'watchdog'))
                stack.enter_context(patch.object(state, '_start_stop_retry_locked'))
                app = FastAPI()
                app.include_router(server.make_router(state))
                client = stack.enter_context(TestClient(app))
                ticket = client.get('/overlay/lifecycle').json()
                claim = client.post('/overlay/claim', json={**ticket, 'client': 'synthetic-renderer', 'creation': 1}).json()
                self.assertTrue(claim['pane'])
                body = {'pane': claim['pane'], 'action': 1}
                if action in ('start', 'pause'):
                    response = client.post('/start', json={**body, 'consent': True, 'backend': 'native'})
                    if action == 'pause':
                        self.assertEqual(response.status_code, 200)
                        response = client.post('/pause', json={**body, 'action': 2})
                        self.assertFalse(response.json()['stop_confirmed'])
                        self.assertTrue(audio.running)
                    else:
                        self.assertEqual(response.status_code, 503)
                        self.assertIn('Check', response.json()['detail'])
                else:
                    def fail(*args, **kwargs):
                        raise RuntimeError(MARKER)
                    if action == 'clear':
                        state.bridge = SimpleNamespace(note_cleared=fail)
                    else:
                        state.overlay = SimpleNamespace(
                            invalidate=fail if action == 'clear-invalidation' else lambda: None,
                            hide_overlay=fail if action == 'clear-overlay' else lambda: None)
                    response = client.post('/clear', json=body)
                    self.assertEqual(response.status_code, 200)
                status = client.get('/status')
                self.assertNotIn(MARKER, response.text + status.text)
                if action == 'pause':
                    self.assertIn('Retry Pause', status.text)
                elif action == 'clear':
                    self.assertIn('try Clear again', status.text)
                elif action.startswith('clear-'):
                    self.assertIn('Retry', response.text + status.text)
