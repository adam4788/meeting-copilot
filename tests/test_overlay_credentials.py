"""The production overlay never admits provider work or retains credentials."""
import contextvars
import importlib.util
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

import overlay_bridge as bridge


class ReadOnlyOverlayTests(unittest.TestCase):
    def make_overlay(self):
        state = Mock()
        state.overlay_lock = threading.RLock()
        transport = bridge.BackendTransport(state, pane_token='synthetic-pane')
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=.01)
        overlay.update({'segments': [{'id': 'row', 'speaker': 'remote', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 1, 'capture_generation': 1})
        self.addCleanup(overlay.stop)
        return state, transport, overlay

    def test_clicks_and_direct_calls_never_admit_work_across_lifecycle_changes(self):
        state, transport, overlay = self.make_overlay()
        click = json.dumps({'type': 'click', 'id': 'row', 'snapshot_id': overlay.snapshot_id})
        for _ in range(2):
            self.assertFalse(overlay.on_click_line(click))
        with self.assertRaisesRegex(RuntimeError, 'Ask in the Meeting Copilot pane'):
            transport.assist('Who approves?', {}, 'row')
        overlay.retire_authorization()
        self.assertFalse(overlay.on_click_line(click))
        overlay.stop()
        self.assertFalse(overlay.on_click_line(click))
        state.assist.assert_not_called()
        self.assertFalse(overlay.request_by_row)
        other_state, other_transport, reopened = self.make_overlay()
        self.assertFalse(reopened.on_click_line(json.dumps({
            'type': 'click', 'id': 'row', 'snapshot_id': reopened.snapshot_id})))
        other_state.assist.assert_not_called()

    def test_snapshot_is_read_only_and_contains_no_secret(self):
        secret = contextvars.ContextVar('synthetic_secret', default=None)
        token = secret.set('synthetic-private-key')
        try:
            state, transport, overlay = self.make_overlay()
            snapshot = overlay.pushed[-1]
            self.assertFalse(snapshot['answer_actions'])
            self.assertIn('Read-only overlay', snapshot['message'])
            self.assertNotIn('synthetic-private-key', repr(vars(transport)))
            self.assertNotIn('synthetic-private-key', json.dumps(snapshot))
            state.assist.assert_not_called()
        finally:
            secret.reset(token)

    def test_status_pump_runs_in_empty_context(self):
        secret = contextvars.ContextVar('synthetic_pump_secret', default='absent')
        token = secret.set('synthetic-private-key')
        seen = []
        ready = threading.Event()
        def poll():
            seen.append(secret.get())
            ready.set()
            return {'segments': [], 'listening': False}
        overlay = bridge.OverlayBridge(transport=bridge.BackendTransport(Mock(), poll=poll), poll_interval=.01)
        try:
            overlay._start_poller_locked()
            self.assertTrue(ready.wait(2))
            self.assertEqual(seen[0], 'absent')
        finally:
            overlay.stop()
            secret.reset(token)

    def test_native_helper_environment_excludes_provider_keys(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-key', 'MODEL_API_KEY': 'synthetic-meta',
                                    'LC_SYNTHETIC_SECRET': 'synthetic-hidden'}):
            env = bridge._helper_environment()
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('MODEL_API_KEY', env)
        self.assertNotIn('LC_SYNTHETIC_SECRET', env)
        self.assertNotIn('synthetic-key', repr(env))

    @unittest.skipUnless(importlib.util.find_spec('agent'), 'requires Hermes scope API')
    def test_key_rotation_deletion_and_profile_switch_never_admit_overlay_work(self):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
        from pathlib import Path
        home = get_hermes_home().resolve()
        initial = set_secret_scope({'OPENAI_API_KEY': 'synthetic-old'}, profile_home=str(home))
        try:
            state, transport, overlay = self.make_overlay()
            click = json.dumps({'type': 'click', 'id': 'row', 'snapshot_id': overlay.snapshot_id})
        finally:
            reset_secret_scope(initial)
        for mapping in ({'OPENAI_API_KEY': 'synthetic-new'}, {}):
            token = set_secret_scope(mapping, profile_home=str(home))
            try:
                self.assertFalse(overlay.on_click_line(click))
            finally:
                reset_secret_scope(token)
        other = set_hermes_home_override(Path(home) / 'other-profile')
        try:
            self.assertFalse(overlay.on_click_line(click))
        finally:
            reset_hermes_home_override(other)
        state.assist.assert_not_called()
        self.assertNotIn('synthetic-old', repr(vars(transport)))
        self.assertNotIn('synthetic-new', json.dumps(overlay.pushed))

    def test_helper_startup_does_not_inherit_pane_context(self):
        from fastapi import HTTPException
        import server
        secret = contextvars.ContextVar('synthetic_startup_secret', default='absent')
        token = secret.set('synthetic-private-key')
        seen = []
        state = server.MeetingState()
        def show(_bridge):
            seen.append(secret.get())
            return {'shown': False, 'error': 'synthetic unavailable'}
        try:
            with patch.object(bridge.OverlayBridge, 'show_overlay', autospec=True, side_effect=show):
                with self.assertRaises(HTTPException):
                    state.overlay_show({})
            self.assertEqual(seen, ['absent'])
        finally:
            secret.reset(token)
