"""Real-router lease tests: no hardware, providers or wall-clock sleeps."""
import threading
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

    def test_claim_stops_previous_capture_without_old_release_stopping_new_one(self):
        old = self.claim()
        self.start(pane=old, action=1)
        new = self.claim(2)  # close cleanup is still delayed in transport
        self.assertFalse(self.state.listening, 'claim must not inherit capture consent')
        self.audio.stop.assert_called_once()
        self.assertTrue(self.post('/heartbeat', {'pane': new})['accepted'])
        self.start(pane=new, action=1)
        self.assertFalse(self.post('/overlay/release', {'pane': old, 'action': 2})['accepted'])
        self.assertTrue(self.state.listening)
        self.assertEqual(self.audio.stop.call_count, 1)

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

    def test_claim_returns_live_capability_even_when_old_helper_hide_fails(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        display = Mock(process=None)
        display.hide_overlay.side_effect = RuntimeError('old helper pipe closed')
        self.state.overlay = display
        new = self.claim(2)
        self.assertFalse(self.state.listening)
        self.assertTrue(self.post('/heartbeat', {'pane': new})['accepted'])
        self.assertIs(self.state.pending_overlay, display)
        self.assertEqual(self.client.post('/start', json=dict(pane=new, action=1, consent=True, backend='macos')).status_code, 409)
        display.hide_overlay.side_effect = None
        display.hide_overlay.return_value = {'shown': False, 'stop_confirmed': True}
        hidden = self.post('/overlay/hide', {'pane': new, 'action': 2})
        self.assertTrue(hidden['overlay_stop_confirmed'])
        self.assertIsNone(self.state.pending_overlay)
        self.start(pane=new, action=3)

    def test_claim_stops_without_overlay_lock_and_preserves_liveness(self):
        import threading
        pane = self.claim()
        self.start(pane=pane, action=1)
        completed = threading.Event()
        workers = []
        def stop():
            def callback():
                # Models a shutdown callback needing overlay -> state locks.
                self.state.heartbeat(server.LeaseRequest(pane=pane))
                completed.set()
            worker = threading.Thread(target=self.checked_callback(callback))
            workers.append(worker)
            worker.start()
            self.checked_wait(completed, 1, 'claim must not join under overlay lock')
        self.audio.stop.side_effect = stop
        new = self.claim(2)
        for worker in workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertFalse(self.state.listening)
        self.assertTrue(self.post('/heartbeat', {'pane': new})['accepted'])
        self.start(pane=new, action=1)

    def test_stop_failure_is_visible_retried_and_blocks_new_capture(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.audio.stop.side_effect = [RuntimeError('signal not sent'), None]
        with patch.object(self.state, '_start_stop_retry_locked', create=True):
            result = self.post('/overlay/release', {'pane': pane, 'action': 2})
        self.assertTrue(result['accepted'])
        self.assertFalse(result['stop_confirmed'])
        status = self.client.get('/status').json()
        self.assertTrue(status['stopping'])
        self.assertEqual(status['capture']['state'], 'stopping')
        self.assertIn('not confirmed', status['error'])
        new = self.claim(2)
        response = self.client.post('/start', json={'pane': new, 'action': 1, 'consent': True, 'backend': 'macos'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.audio.start.call_count, 1)
        self.state._retry_stop(self.state.stop_sequence)
        self.assertFalse(self.client.get('/status').json()['stopping'])
        self.start(pane=new, action=2)
        stops = self.audio.stop.call_count
        self.assertFalse(self.post('/overlay/release', {'pane': pane, 'action': 3})['accepted'])
        self.state._retry_stop(self.state.stop_sequence - 1)
        self.assertEqual(self.audio.stop.call_count, stops)

    def test_stop_retries_are_bounded_and_current_owner_can_retry(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.audio.stop.side_effect = RuntimeError('signal not sent')
        with patch.object(self.state, '_start_stop_retry_locked', create=True):
            result = self.post('/pause', {'pane': pane, 'action': 2})
            self.assertFalse(result['stop_confirmed'])
            self.state._retry_stop(self.state.stop_sequence)
        self.assertEqual(self.audio.stop.call_count, 3)
        status = self.client.get('/status').json()
        self.assertTrue(status['stopping'])
        self.assertIn('Retry', status['error'])
        self.assertFalse(self.post('/heartbeat', {'pane': pane})['accepted'])
        self.audio.stop.side_effect = None
        result = self.post('/pause', {'pane': pane, 'action': 3})
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(self.client.get('/status').json()['stopping'])

    def test_clear_cannot_replace_adapter_when_its_stop_fails(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.audio.stop.side_effect = RuntimeError('signal not sent')
        with patch.object(self.state, '_start_stop_retry_locked'):
            response = self.client.post('/clear', json={'pane': pane, 'action': 2})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['listening'])
        self.assertEqual(self.audio.start.call_count, 1)
        self.assertTrue(self.client.get('/status').json()['stopping'])

    def test_show_expiring_during_helper_launch_never_refreshes_capture(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        seen = self.state.pane_seen
        self.now += 29
        display = Mock(lock=threading.RLock(), stopping=False,
                       _admission_lock=threading.RLock(), _lifecycle_epoch=0)
        display.show_overlay.side_effect = lambda: (self.tick(2), {'shown': True})[1]
        with patch('overlay_bridge.OverlayBridge', return_value=display):
            response = self.client.post('/overlay/show', json={'pane': pane, 'action': 2, 'scope': {}})
        # Expiry retires the pending startup before its Show commit.
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.state.listening)
        self.assertEqual(self.state.pane_seen, seen)
        self.assertFalse(self.post('/heartbeat', {'pane': pane})['accepted'])

    def test_watchdog_stop_failure_has_independent_retry(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.audio.stop.side_effect = [RuntimeError('signal not sent'), None]
        self.now += 31
        with patch.object(self.state, '_start_stop_retry_locked', create=True) as retry:
            server.MeetingState.watchdog(self.state, self.state.capture_generation)
        retry.assert_called_once()
        self.assertTrue(self.client.get('/status').json()['stopping'])
        self.state._retry_stop(self.state.stop_sequence)
        self.assertFalse(self.client.get('/status').json()['stopping'])

    def test_expired_clear_cannot_restart_capture(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.state.add_segment({'text': 'private'})
        seen = self.state.pane_seen
        self.now += 31
        result = self.post('/clear', {'pane': pane, 'action': 2})
        self.assertFalse(result['listening'])
        self.assertEqual(result['segments'], [])
        self.assertEqual(self.audio.start.call_count, 1)
        self.audio.stop.assert_called_once()
        self.assertEqual(self.state.pane_seen, seen)

    def test_failed_show_cannot_revive_expired_capture(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        seen = self.state.pane_seen
        self.now += 31
        display = Mock(lock=threading.RLock(), stopping=False,
                       _admission_lock=threading.RLock(), _lifecycle_epoch=0)
        display.show_overlay.return_value = {'shown': False}
        with patch('overlay_bridge.OverlayBridge', return_value=display):
            result = self.client.post('/overlay/show', json={'pane': pane, 'action': 2, 'scope': {}})
        self.assertEqual(result.status_code, 503)
        self.assertFalse(self.state.listening)
        self.audio.stop.assert_called_once()
        self.assertEqual(self.state.pane_seen, seen)
        self.assertFalse(self.post('/heartbeat', {'pane': pane})['accepted'])

    def test_valid_clear_preserves_capture_deadline_not_consent(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        seen = self.state.pane_seen
        self.now += 2
        result = self.post('/clear', {'pane': pane, 'action': 2})
        self.assertTrue(result['listening'])
        self.assertEqual(self.audio.start.call_count, 2)
        self.assertEqual(self.state.pane_seen, seen, 'Clear is not fresh capture consent')

    def test_clear_expiring_during_restart_preparation_cannot_start(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.now += 29
        real_get_audio = self.state.get_audio
        def delayed_audio(*args, **kwargs):
            self.now += 2
            return real_get_audio(*args, **kwargs)
        with patch.object(self.state, 'get_audio', side_effect=delayed_audio):
            result = self.post('/clear', {'pane': pane, 'action': 2})
        self.assertFalse(result['listening'])
        self.assertEqual(self.audio.start.call_count, 1)

    def test_clear_expiring_during_stop_cannot_restart_capture(self):
        pane = self.claim()
        self.start(pane=pane, action=1)
        self.now += 29
        self.audio.stop.side_effect = lambda: self.tick(2)
        result = self.post('/clear', {'pane': pane, 'action': 2})
        self.assertFalse(result['listening'])
        self.assertEqual(self.audio.start.call_count, 1)

    def test_explicit_show_reestablishes_expired_display_lease(self):
        pane = self.claim()
        self.now += 31
        display = Mock(lock=threading.RLock(), stopping=False,
                       _admission_lock=threading.RLock(), _lifecycle_epoch=0)
        display.show_overlay.return_value = {'shown': True}
        display.process.poll.return_value = None  # explicit live-helper evidence
        display.process.returncode = None
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
