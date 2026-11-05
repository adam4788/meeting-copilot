"""Reviewer reproductions: deterministic local-only lifecycle regressions."""
import threading
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import server
import overlay_bridge as bridge
from tests.thread_test_support import CheckedThreads
from tests.test_overlay_bridge import _fake_overlay_spawn
from tests.test_overlay_blockers import click


class FollowupTests(CheckedThreads):
    def test_provider_slot_failure_is_sanitized_and_persists(self):
        class Transport:
            def assist(self, question, scope, row_id, expected_generation=None):
                return 'slot'
            def assist_request(self, request_id):
                return {'row_id': 'a', 'status': 'error', 'answer': 'Partial',
                        'error': 'private provider diagnostic'}
            def cancel(self, request_id):
                pass
        status = {'segments': [{'id': 'a', 'text': 'Question?'}],
                  'listening': True, 'generation': 1, 'capture_generation': 1}
        overlay = bridge.OverlayBridge(transport=Transport())
        overlay.update(status)
        self.assertTrue(overlay.on_click_line(click(overlay, 'a')))
        for worker in list(overlay.threads):
            worker.join(2)
            self.assertFalse(worker.is_alive())
        overlay.update(status)
        row = overlay.pushed[-1]['questions'][0]
        self.assertEqual(row['answer'], 'Partial')
        self.assertTrue(any('partial' in warning.lower() and 'retry' in warning.lower()
                            for warning in row['warnings']))
        self.assertNotIn('private provider diagnostic', str(overlay.pushed[-1]))

    def test_admission_error_lifecycle_and_explicit_retry(self):
        class Transport:
            calls = 0
            def assist(self, question, scope, row_id, expected_generation=None):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError('private secret')
                return 'retry'
            def assist_request(self, request_id):
                return {'row_id': 'a', 'status': 'done', 'answer': 'Retry succeeded'}
            def cancel(self, request_id):
                pass

        status = {'segments': [{'id': 'a', 'text': 'Question?'},
                               {'id': 'b', 'text': 'Other?'}],
                  'listening': True, 'generation': 1, 'capture_generation': 1}
        for boundary in ('retry', 'delete', 'revise', 'pause', 'clear', 'meeting', 'scope'):
            with self.subTest(boundary=boundary):
                transport = Transport()
                overlay = bridge.OverlayBridge(transport=transport)
                overlay.update(status)
                overlay.answers['b'].update(answer='Other answer', completed=True)
                self.assertTrue(overlay.on_click_line(click(overlay, 'a')))
                for worker in list(overlay.threads):
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                self.assertTrue(overlay.answers['a'].get('admission_error'))
                self.assertEqual(transport.calls, 1)
                if boundary == 'retry':
                    self.assertTrue(overlay.on_click_line(click(overlay, 'a')))
                    for worker in list(overlay.threads):
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                    self.assertEqual(overlay.answers['a']['answer'], 'Retry succeeded')
                    self.assertEqual(transport.calls, 2)
                elif boundary == 'scope':
                    overlay.set_scope({'project': 'new'})
                    overlay.update(status)
                else:
                    changed = dict(status)
                    if boundary == 'delete':
                        changed['segments'] = status['segments'][1:]
                    elif boundary == 'revise':
                        changed['segments'] = [{'id': 'a', 'text': 'Corrected?'}, status['segments'][1]]
                    elif boundary == 'pause':
                        changed['listening'] = False
                    elif boundary == 'clear':
                        changed.update(segments=[], generation=2)
                    elif boundary == 'meeting':
                        changed['generation'] = 2
                    overlay.update(changed)
                    if boundary == 'delete':
                        overlay.update(status)
                self.assertNotIn('admission_error', overlay.answers.get('a', {}))
                if boundary in ('retry', 'delete', 'revise', 'pause'):
                    self.assertEqual(overlay.answers['b']['answer'], 'Other answer')
                if boundary == 'meeting':
                    self.assertIsNone(overlay.answers['b']['answer'])
                self.assertNotIn('private secret', str(overlay.pushed[-1]))
                overlay.stop()

    def test_real_backend_busy_and_stale_admission_are_sanitized(self):
        for kind in ('busy', 'stale'):
            with self.subTest(kind=kind):
                state = server.MeetingState()
                state.listening = True
                state.add_segment({'id': 'a', 'text': 'Question?'})
                token = bridge._PANE_ACTION.set('test-pane')
                try:
                    transport = bridge.BackendTransport(state, pane_token='test-pane')
                finally:
                    bridge._PANE_ACTION.reset(token)
                overlay = bridge.OverlayBridge(transport=transport)
                overlay.update(state.status(touch=False))
                if kind == 'busy':
                    state.requests['secret-id'] = {'request_id': 'secret-id', 'status': 'running'}
                else:
                    state.generation += 1  # backend advanced before bridge's next poll
                self.assertTrue(overlay.on_click_line(click(overlay, 'a')))
                for worker in list(overlay.threads):
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                overlay.update(dict(segments=[{'id': 'a', 'text': 'Question?'}],
                                    listening=True, generation=overlay._last_generation,
                                    capture_generation=overlay._last_capture_generation))
                warning = overlay.pushed[-1]['questions'][0]['warnings'][0]
                self.assertIn('tap', warning.lower())
                self.assertIn('retry', warning.lower())
                self.assertIn('another answer' if kind == 'busy' else 'changed', warning.lower())
                self.assertNotIn('secret-id', warning)
                self.assertEqual(state.request_seq, 0)
                overlay.stop()

    def test_reentrant_late_admission_failure_cannot_replace_newer_answer(self):
        class Transport:
            def assist(self, question, scope, row_id, expected_generation=None):
                overlay.update(dict(status, generation=2))
                overlay.answers[row_id].update(answer='New answer', completed=True)
                raise RuntimeError('private old failure')
            def cancel(self, request_id):
                pass
        overlay = bridge.OverlayBridge(transport=Transport())
        status = {'segments': [{'id': 'a', 'text': 'Question?'}],
                  'listening': True, 'generation': 1, 'capture_generation': 1}
        overlay.update(status)
        self.assertTrue(overlay.on_click_line(click(overlay, 'a')))
        for worker in list(overlay.threads):
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(overlay.answers['a']['answer'], 'New answer')
        self.assertNotIn('admission_error', overlay.answers['a'])
        self.assertNotIn('private old failure', str(overlay.pushed[-1]))

    def client(self, state):
        app = FastAPI()
        app.include_router(server.make_router(state))
        return TestClient(app)

    def test_release_only_ticket_conflict_recovers_but_newer_claim_wins(self):
        state = server.MeetingState()
        with self.client(state) as client:
            first_ticket = client.get('/overlay/lifecycle').json()
            # Legacy CAS is internal-only; HTTP claims require creation order.
            old = state.overlay_claim(first_ticket['revision'])['pane']
            ticket = client.get('/overlay/lifecycle').json()
            self.assertTrue(client.post('/overlay/release', json={'pane': old, 'action': 1}).json()['accepted'])
            retry = state.overlay_claim(ticket['revision'])
            self.assertEqual(retry.get('retry_revision'), state.overlay_revision)
            recovered = state.overlay_claim(retry['retry_revision'])['pane']
            self.assertNotEqual(old, recovered)
            self.assertFalse(client.post('/overlay/release', json={'pane': old, 'action': 999}).json()['accepted'])
            # Both original ticket and retry are CAS-bound, not a license to steal.
            for rev in (ticket['revision'], first_ticket['revision'], retry['retry_revision']):
                rejected = state.overlay_claim(rev)
                self.assertFalse(rejected['accepted'])
                self.assertNotIn('retry_revision', rejected)
            self.assertEqual(state.overlay_pane, recovered)

    def test_capture_start_is_ordered_with_release_and_new_owner(self):
        audio = Mock()
        state = server.MeetingState(macos_factory=lambda **kw: audio)
        with self.client(state) as client, patch.object(state, 'watchdog'):
            pane = state.overlay_claim()['pane']
            payload = {'consent': True, 'backend': 'macos', 'pane': pane, 'action': 1}
            self.assertEqual(client.post('/start', json=payload).status_code, 200)
            self.assertTrue(state.listening)
            self.assertTrue(state.overlay_release(pane, 2)['accepted'])
            self.assertFalse(state.listening)
            # A request transmitted before close can arrive after release.
            self.assertEqual(client.post('/start', json=payload).status_code, 409)
            self.assertFalse(state.listening)
            newer = state.overlay_claim()['pane']
            fresh = dict(payload, pane=newer)
            self.assertEqual(client.post('/start', json=fresh).status_code, 200)
            self.assertTrue(state.listening)
            self.assertFalse(state.overlay_release(pane, 999)['accepted'])
            self.assertEqual(client.post('/start', json=dict(payload, action=1000)).status_code, 409)
            self.assertTrue(state.listening)
            state.overlay_release(newer, 2)
        self.assertEqual(audio.start.call_count, 2)

    def test_show_before_first_start_keeps_live_overlay_and_owner(self):
        state = server.MeetingState(macos_factory=lambda **kw: Mock())
        with self.client(state) as client, _fake_overlay_spawn(), patch.object(state, '_start_overlay_lease_locked'):
            pane = state.overlay_claim()['pane']
            shown = client.post('/overlay/show', json={'pane': pane, 'action': 1, 'scope': {'project': 'alpha'}}).json()
            self.assertTrue(shown['shown'])
            overlay = state.overlay
            try:
                with patch.object(state, 'watchdog'):
                    res = client.post('/start', json={'consent': True, 'backend': 'macos', 'pane': pane, 'action': 2})
                self.assertEqual(res.status_code, 200)
                self.assertIs(state.overlay, overlay)
                self.assertEqual(state.overlay_owner, shown['owner'])
                self.assertFalse(overlay.stopping)
                state.add_segment({'id': 'row', 'text': 'Who approves?'})
                overlay.update(state.status(touch=False))
                self.assertEqual(overlay.questions['row'], 'Who approves?')
                self.assertTrue(overlay.listening)
            finally:
                state.overlay_release(pane, 3)
                overlay.stop()

    def test_partial_cancelled_before_status_pump_does_not_look_completed(self):
        engine = Mock()
        engine.supports_streaming.return_value = True
        finish_provider, seen, release = (threading.Event() for _ in range(3))
        def chunks():
            yield 'Incomplete'
            self.checked_wait(finish_provider, 3, 'provider finish')
        engine.answer_stream.return_value = ({}, chunks())
        state = server.MeetingState(engine_factory=lambda: engine)
        state.listening = True
        state.add_segment({'id': 'row', 'text': 'What is the plan?'})
        token = bridge._PANE_ACTION.set('followup')
        try:
            transport = bridge.BackendTransport(state, pane_token='followup')
        finally:
            bridge._PANE_ACTION.reset(token)
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.001)
        overlay.update(state.status(touch=False))
        original = overlay.push
        def push():
            if overlay.answers.get('row', {}).get('answer') == 'Incomplete' and not seen.is_set():
                seen.set()
                self.checked_wait(release, 3, 'partial publication')
            return original()
        try:
            with patch.object(overlay, 'push', self.checked_callback(push)):
                self.assertTrue(overlay.on_click_line(click(overlay)))
                self.assertTrue(seen.wait(2))
                state.pause()
                release.set()
                for worker in overlay.threads:
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(overlay.request_by_row, {})
            # Before status update, cancellation must already label partial text.
            partial = overlay.pushed[-1]['questions'][0]
            self.assertTrue(not partial['answer'] or any('partial' in w.lower() for w in partial['warnings']))
            overlay.update(state.status(touch=False))
            self.assertIsNone(overlay.pushed[-1]['questions'][0]['answer'])
        finally:
            release.set()
            finish_provider.set()
            overlay.stop()
            for worker in threading.enumerate():
                if worker.name.startswith('meeting-answer-'):
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
        engine.answer_stream.assert_called_once()
        self.assertEqual(self.worker_errors, [])
