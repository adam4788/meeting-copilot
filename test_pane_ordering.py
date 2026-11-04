"""Offline HTTP lifecycle reproductions; assertions run on the test thread."""
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import server
from thread_test_support import CheckedThreads


class PaneOrderingTests(CheckedThreads):
    def client(self, state):
        app = FastAPI()
        app.include_router(server.make_router(state))
        return TestClient(app)

    def test_delayed_destructive_requests_cannot_mutate_new_pane(self):
        for path in ('/pause', '/clear'):
            with self.subTest(path=path):
                state = server.MeetingState(macos_factory=lambda **kw: Mock())
                with self.client(state) as client, patch.object(state, 'watchdog'):
                    old = state.overlay_claim()['pane']
                    delayed = {'pane': old, 'action': 1}
                    new = state.overlay_claim()['pane']
                    self.assertEqual(client.post('/start', json={
                        'consent': True, 'backend': 'macos', 'pane': new, 'action': 1}).status_code, 200)
                    state.add_segment({'text': 'New meeting'})
                    # Plugin unregister is now this exact capability release,
                    # never a second unscoped /pause.
                    stale_unregister = client.post('/overlay/release', json={'pane': old, 'action': 999})
                    self.assertFalse(stale_unregister.json()['accepted'])
                    self.assertTrue(state.listening)
                    for payload in (delayed, {}, {'pane': old}, {'action': 5}, {'revision': 0, 'pane': new}, {'typo': new}):
                        response = client.post(path, json=payload)
                        self.assertIn(response.status_code, (409, 422), response.text)
                        self.assertTrue(state.listening)
                        self.assertEqual(len(state.segments), 1)
                    valid = client.post(path, json={'pane': new, 'action': 2})
                    self.assertEqual(valid.status_code, 200, valid.text)
                    if path == '/pause':
                        self.assertFalse(state.listening)
                    else:
                        self.assertEqual(len(state.segments), 0)
                    state.pause()

    def test_creation_order_wins_even_when_old_lookup_arrives_last(self):
        state = server.MeetingState()
        with self.client(state) as client:
            # Pane 1 was created first but its GET is delayed in transport.
            ticket2 = client.get('/overlay/lifecycle').json()
            newer = client.post('/overlay/claim', json={**ticket2, 'client': 'renderer', 'creation': 2}).json()
            self.assertIn('pane', newer)
            ticket1 = client.get('/overlay/lifecycle').json()
            stale = client.post('/overlay/claim', json={**ticket1, 'client': 'renderer', 'creation': 1}).json()
            self.assertFalse(stale.get('accepted', True), stale)
            self.assertEqual(state.overlay_pane, newer['pane'])
            # A genuinely newer pane can replace it even with an older GET.
            newest = client.post('/overlay/claim', json={**ticket2, 'client': 'renderer', 'creation': 3}).json()
            self.assertIn('pane', newest)
            self.assertFalse(state.overlay_release(newer['pane'], 1)['accepted'])
            # Missing ordering must not silently downgrade a claimed renderer.
            unscoped = client.post('/overlay/claim', json={'revision': state.overlay_revision}).json()
            self.assertFalse(unscoped.get('accepted', True), unscoped)
            state.overlay_release(newest['pane'], 1)
            replay = client.post('/overlay/claim', json={**client.get('/overlay/lifecycle').json(),
                                                       'client': 'renderer', 'creation': 1}).json()
            self.assertFalse(replay.get('accepted', True), replay)

    def test_uncomparable_renderers_and_backend_restart_fail_closed(self):
        state = server.MeetingState()
        with self.client(state) as client:
            ticket = client.get('/overlay/lifecycle').json()
            first = client.post('/overlay/claim', json={**ticket, 'client': 'one', 'creation': 1}).json()
            self.assertIn('pane', first)
            other = client.post('/overlay/claim', json={**client.get('/overlay/lifecycle').json(),
                                                      'client': 'two', 'creation': 99}).json()
            self.assertFalse(other.get('accepted', True), other)
            state.overlay_release(first['pane'], 1)
            other = client.post('/overlay/claim', json={**client.get('/overlay/lifecycle').json(),
                                                      'client': 'two', 'creation': 100}).json()
            self.assertFalse(other.get('accepted', True), 'release cannot erase creation ordering')
        with self.client(server.MeetingState()) as restarted:
            response = restarted.post('/overlay/claim', json={**ticket, 'client': 'one', 'creation': 2}).json()
            self.assertFalse(response.get('accepted', True), 'old backend epoch cannot authorize a new backend')

    def test_legacy_clients_work_before_panes_then_use_explicit_revision(self):
        state = server.MeetingState()
        with self.client(state) as client:
            self.assertEqual(client.post('/pause').status_code, 200)
            self.assertEqual(client.post('/clear', json={}).status_code, 200)
            old_revision = client.get('/overlay/lifecycle').json()
            pane = state.overlay_claim()['pane']
            self.assertEqual(client.post('/pause', json=old_revision).status_code, 409)
            current = client.get('/overlay/lifecycle').json()
            self.assertEqual(client.post('/clear', json=current).status_code, 200)
            state.overlay_release(pane, 1)
            self.assertEqual(client.post('/pause').status_code, 409, 'release must not reopen legacy admission')
            self.assertEqual(client.post('/pause', json=current).status_code, 409)
            fresh = client.get('/overlay/lifecycle').json()
            self.assertEqual(client.post('/pause', json={**fresh, 'epoch': 'previous-backend'}).status_code, 409)
            self.assertEqual(client.post('/pause', json={'revision': fresh['revision']}).status_code, 409)
            self.assertEqual(client.post('/pause', json=fresh).status_code, 200)
