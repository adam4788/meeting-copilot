"""Overlay scope guards after retiring editor-triggered secret refresh.

The pane no longer edits keys or promises immediate publication. Explicit pane
show/scope actions retain context checks; workers keep admitted snapshots.
"""
import contextvars
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fastapi import HTTPException
from overlay_bridge import BackendTransport

class OverlayCredentialTests(unittest.TestCase):
    def test_scope_capture_failure_is_sanitized_and_does_not_publish_transport(self):
        with patch.object(BackendTransport,'_fresh_action_context',side_effect=HTTPException(503,'Overlay credential scope unavailable. Reopen the pane and retry.')):
            with self.assertRaises(HTTPException) as error:BackendTransport(SimpleNamespace())
        self.assertEqual(error.exception.status_code,503)
        self.assertNotIn('secret',str(error.exception.detail).lower())
    def test_new_explicit_pane_action_replaces_retained_context(self):
        marker=contextvars.ContextVar('synthetic_profile')
        token=marker.set('first')
        try:
            with patch.object(BackendTransport,'_fresh_action_context',side_effect=lambda *a:contextvars.copy_context()):
                transport=BackendTransport(SimpleNamespace(),pane_token='first-pane')
                old=transport._poll_context.copy()
                marker.set('second')
                transport.refresh_pane_token('second-pane')
                self.assertEqual(old.run(marker.get),'first')
                self.assertEqual(transport._poll_context.run(marker.get),'second')
        finally:marker.reset(token)
    def test_failed_explicit_refresh_closes_future_admissions(self):
        with patch.object(BackendTransport,'_fresh_action_context',return_value=contextvars.copy_context()):transport=BackendTransport(SimpleNamespace(),pane_token='pane')
        with patch.object(BackendTransport,'_fresh_action_context',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):transport.refresh_pane_token('new-pane')
        self.assertIsNone(transport._poll_context)


class FreshHostScopeTests(unittest.TestCase):
    """Exercise the current host boundary with synthetic, isolated mappings."""

    def setUp(self):
        self.scope = contextvars.ContextVar('synthetic_scope', default=None)
        self.bound_home = contextvars.ContextVar('synthetic_scope_home', default=None)
        self.home = Path(tempfile.gettempdir()) / 'synthetic-hermes-profile'
        self.old = {'OPENAI_API_KEY': 'synthetic-old'}
        self.fresh = {'OPENAI_API_KEY': 'synthetic-new'}
        self.addCleanup(self.scope.reset, self.scope.set(self.old))
        self.addCleanup(self.bound_home.reset, self.bound_home.set(str(self.home)))

        @contextmanager
        def host_scope(profile):
            self.assertEqual(profile, 'current')
            token = self.scope.set(self.fresh)
            try:
                yield
            finally:
                self.scope.reset(token)

        agent = types.ModuleType('agent')
        agent.__path__ = []
        secrets = types.ModuleType('agent.secret_scope')
        secrets.current_secret_scope = self.scope.get
        secrets.current_secret_scope_home = self.bound_home.get
        agent.secret_scope = secrets
        cli = types.ModuleType('hermes_cli')
        cli.__path__ = []
        profiles = types.ModuleType('hermes_cli.web_server_profiles')
        profiles._config_profile_scope = host_scope
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: self.home
        self.modules = patch.dict(sys.modules, {
            'agent': agent, 'agent.secret_scope': secrets,
            'hermes_cli': cli, 'hermes_cli.web_server_profiles': profiles,
            'hermes_constants': constants,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_refresh_is_a_distinct_host_scope_and_preserves_admitted_snapshot(self):
        captured = BackendTransport._fresh_action_context(expected_home=self.home)
        self.assertIs(self.scope.get(), self.old)
        self.assertIs(captured.run(self.scope.get), self.fresh)
        self.assertEqual(self.old['OPENAI_API_KEY'], 'synthetic-old')

    def test_wrong_profile_or_reused_host_scope_fails_closed(self):
        with self.assertRaises(HTTPException) as mismatch:
            BackendTransport._fresh_action_context(expected_home=self.home / 'other')
        self.assertEqual(mismatch.exception.status_code, 503)
        self.fresh = self.old
        with self.assertRaises(HTTPException) as reused:
            BackendTransport._fresh_action_context(expected_home=self.home)
        self.assertEqual(reused.exception.status_code, 503)
        self.assertIs(self.scope.get(), self.old)
