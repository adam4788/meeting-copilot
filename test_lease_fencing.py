"""Real-router lease tests: no hardware, providers or wall-clock sleeps."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import server
from thread_test_support import CheckedThreads


class LeaseFencingTests(CheckedThreads):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.audio = Mock()
        self.state = server.MeetingState(macos_factory=lambda **kw: self.audio)
        app = FastAPI()
        app.include_router(server.make_router(self.state))
        self.client = self.enterContext(TestClient(app))
        self.enterContext(patch.object(server, 'time', SimpleNamespace(
            monotonic=lambda: self.now, time=lambda: self.now, sleep=self.tick)))
        # Drive the real watchdog explicitly; no competing daemon clock.
        self.enterContext(patch.object(self.state, 'watchdog'))

    def tick(self, seconds):
        self.now += seconds

    def post(self, path, body):
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def claim(self, creation=1):
        ticket = self.client.get('/overlay/lifecycle').json()
        return self.post('/overlay/claim', dict(ticket, client='renderer', creation=creation))['pane']

    def start(self, **auth):
        self.post('/start', dict(consent=True, backend='macos', **auth))
        self.assertTrue(self.state.listening)

    def test_status_and_unscoped_heartbeat_cannot_renew_claimed_capture(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        seen = self.state.pane_seen
        def polling_tick(seconds):
            self.tick(seconds)
            self.assertEqual(self.client.get('/status').status_code, 200)
            self.assertFalse(self.post('/heartbeat', {})['accepted'])
        with patch.object(server.time, 'sleep', polling_tick):
            server.MeetingState.watchdog(self.state, self.state.capture_generation)
        self.assertEqual(self.now - seen, 32)
        self.assertEqual(self.state.pane_seen, seen)
        self.assertFalse(self.state.listening)
        self.audio.stop.assert_called_once()

    def test_expired_ping_cannot_resurrect_lease_before_watchdog_observes_it(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        seen = self.state.pane_seen
        self.now += 31
        self.assertFalse(self.post('/heartbeat', {'pane': pane})['accepted'])
        self.assertEqual(self.state.pane_seen, seen)
        server.MeetingState.watchdog(self.state, self.state.capture_generation)
        self.assertFalse(self.state.listening)
        self.audio.stop.assert_called_once()
        self.start(pane=pane, action=2)
        self.assertTrue(self.post('/heartbeat', {'pane': pane})['accepted'])

    def test_api_clients_use_explicit_current_revision_not_renderer_fallback(self):
        self.start()
        self.now += 10
        self.assertTrue(self.post('/heartbeat', {})['accepted'])
        self.post('/pause', {})
        old = self.client.get('/overlay/lifecycle').json()
        self.claim()
        ticket = self.client.get('/overlay/lifecycle').json()
        self.start(**ticket)
        self.now += 10
        self.assertFalse(self.post('/heartbeat', old)['accepted'])
        self.assertFalse(self.post('/heartbeat', {})['accepted'])
        self.assertTrue(self.post('/heartbeat', ticket)['accepted'])
        self.post('/clear', ticket)
        self.assertTrue(self.state.listening)
        self.post('/pause', ticket)
        self.assertFalse(self.state.listening)
        self.claim(2)
        self.assertFalse(self.post('/heartbeat', ticket)['accepted'])
        self.assertEqual(self.client.post('/start', json=dict(ticket, consent=True, backend='macos')).status_code, 409)

    def test_malformed_or_mixed_authorization_never_downgrades(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        ticket = self.client.get('/overlay/lifecycle').json()
        for body in ({'pane': pane, **ticket}, {'revision': ticket['revision']},
                     {'epoch': ticket['epoch']}, {'pane': 'retired'},
                     dict(ticket, epoch='previous-backend')):
            self.now += 1
            before = self.state.pane_seen
            self.assertFalse(self.post('/heartbeat', body)['accepted'])
            self.assertEqual(self.state.pane_seen, before)
        self.assertEqual(self.client.post('/heartbeat', json={'typo': pane}).status_code, 422)

    def test_explicit_show_reestablishes_expired_display_lease(self):
        pane = self.claim()
        self.now += 31
        display = Mock()
        display.show_overlay.return_value = {'shown': True}
        display.set_scope.return_value = {}
        with patch('overlay_bridge.OverlayBridge', return_value=display), patch.object(self.state, '_start_overlay_lease_locked'):
            self.post('/overlay/show', {'pane': pane, 'action': 1, 'scope': {}})
        self.assertEqual(self.state.pane_seen, self.now)
        self.assertTrue(self.post('/heartbeat', {'pane': pane})['accepted'])
        # A delayed obsolete Show cannot renew the new owner's lease.
        self.claim(2)
        self.now += 10
        before = self.state.pane_seen
        result = self.post('/overlay/show', {'pane': pane, 'action': 999, 'scope': {}})
        self.assertFalse(result['accepted'])
        self.assertEqual(self.state.pane_seen, before)

    def test_reload_cannot_take_over_even_after_expiry_and_release(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.now += 31
        server.MeetingState.watchdog(self.state, self.state.capture_generation)
        self.post('/overlay/release', {'pane': pane, 'action': 2})
        for renderer in ('reloaded', 'old-delayed-other-renderer'):
            ticket = self.client.get('/overlay/lifecycle').json()
            result = self.post('/overlay/claim', dict(ticket, client=renderer, creation=999))
            self.assertEqual(result['reason'], 'different_renderer')
        # Same renderer still has creation ordering and can recover explicitly.
        fresh = self.claim(2)
        self.start(pane=fresh, action=1)
