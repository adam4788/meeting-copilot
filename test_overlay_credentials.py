"""Overlay scope guards after retiring editor-triggered secret refresh.

The pane no longer edits keys or promises immediate publication. Explicit pane
show/scope actions retain context checks; workers keep admitted snapshots.
"""
import contextvars
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
