"""Deterministic follow-up regressions; no live app, capture or providers."""
from tests.thread_test_support import mock_capture_preflight
import json
import os
import select
import sys
import unittest
import threading
from unittest.mock import Mock, patch

import overlay_bridge as bridge
import server
from tests.thread_test_support import CheckedThreads
from tests import test_overlay_blockers as blockers
from tests.test_overlay_blockers import click


class RevisionTests(CheckedThreads):
    def setUp(self):
        super().setUp()
        mock_capture_preflight(self)

    backend = blockers.BlockerTests.backend
    join = blockers.BlockerTests.join

    def test_http_read_only_overlay_and_owner_cleanup(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tests.test_overlay_bridge import _fake_overlay_spawn
        engine = Mock()
        state = server.MeetingState(engine_factory=lambda: engine)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with _fake_overlay_spawn(), patch.object(state, '_start_overlay_lease_locked'), TestClient(app) as client:
            def post(path, payload):
                response = client.post(path, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()
            ticket = client.get('/overlay/lifecycle').json()
            ticket.update(client='revision-test', creation=1)
            pane = post('/overlay/claim', ticket)['pane']
            state.listening = True
            state.add_segment({'id': 'row', 'text': 'Who approves?'})
            shown = post('/overlay/show', {'pane': pane, 'action': 1, 'scope': {'seq': 0}})
            self.assertTrue(shown['shown'])
            overlay = state.overlay
            try:
                overlay.update(state.status(touch=False))
                self.assertFalse(overlay.on_click_line(click(overlay)))
                self.assertEqual(state.requests, {})
                self.assertTrue(post('/overlay/scope', {'scope': {'owner': shown['owner'], 'seq': 1}})['accepted'])
                new_ticket = client.get('/overlay/lifecycle').json()
                new_ticket.update(client='revision-test', creation=2)
                newer = post('/overlay/claim', new_ticket)['pane']
                self.assertFalse(post('/overlay/hide', {'pane': pane, 'action': 999})['accepted'])
                self.assertFalse(post('/overlay/release', {'pane': pane, 'action': 1000})['accepted'])
                self.assertTrue(post('/overlay/release', {'pane': newer, 'action': 1})['accepted'])
            finally:
                state.overlay_hide()
                overlay.stop()
                self.join(overlay)
        engine.answer.assert_not_called()
        self.assertEqual(self.worker_errors, [])


    def test_scope_change_rotates_binding_and_clears_display(self):
        state, overlay, engine = self.backend()
        overlay.answers['row']['answer'] = 'Synthetic old display'
        token = overlay.transport._pane_token
        identity = overlay.snapshot_id
        state.overlay_scope({'owner': state.overlay_owner, 'project': 'narrow', 'seq': 1})
        self.assertNotEqual(overlay.transport._pane_token, token)
        self.assertNotEqual(overlay.snapshot_id, identity)
        self.assertIsNone(overlay.answers['row']['answer'])
        self.assertEqual(state.overlay_scope_value['project'], 'narrow')
        self.assertFalse(overlay.on_click_line(click(overlay)))
        engine.answer.assert_not_called()


    def test_start_new_or_switched_meeting_retires_overlay_synchronously(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                state, overlay, engine = self.backend()
                state.listening = False
                state.session_key = ('meetily', 'old')
                state.bridge = Mock()
                overlay.answers['row']['answer'] = 'PRIVATE OLD'
                before = overlay.snapshot_id
                with patch.object(state, 'watchdog'):
                    state.start(True, recording_id='old' if explicit else 'new', new_meeting=explicit)
                count = len(overlay.pushed)
                overlay.push()
                self.assertEqual(overlay.answers, {})
                self.assertEqual(overlay.segments, [])
                self.assertIsNone(state.overlay)
                self.assertNotEqual(overlay.snapshot_id, before)
                self.assertEqual(len(overlay.pushed), count)

    def test_new_meeting_waits_for_bridge_without_holding_state_lock(self):
        state, overlay, _ = self.backend()
        state.listening = False
        state.session_key = ('meetily', 'old')
        state.bridge = Mock()
        acquired = threading.Event()
        original_lock = overlay.lock
        starter = None
        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is starter:
                    acquired.set()
                return original_lock.__enter__()
            def __exit__(self, *args):
                return original_lock.__exit__(*args)
        overlay.lock = ObservedLock()
        starter = threading.Thread(target=self.checked_callback(
            lambda: state.start(True, recording_id='new')), daemon=True)
        # Keep the real cleanup worker; suppress only the unrelated watchdog.
        with patch.object(state, 'watchdog'):
            with original_lock:
                starter.start()
                self.assertTrue(acquired.wait(2), 'Start must synchronously fence the old bridge')
                available = state.lock.acquire(blocking=False)
                if available:
                    state.lock.release()
                self.assertTrue(available, 'state -> bridge lock inversion')
            starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertEqual(overlay.answers, {})
        self.assertEqual(self.worker_errors, [])

    def test_old_pane_and_delayed_show_actions_are_fenced(self):
        state = server.MeetingState()
        state._overlay_helper_path = lambda: None
        from tests.test_overlay_bridge import _fake_overlay_spawn
        with _fake_overlay_spawn(), patch.object(state, '_start_overlay_lease_locked'):
            first = state.overlay_claim()['pane']
            a = state.overlay_show({}, pane=first, action=1)
            second = state.overlay_claim()['pane']
            b = state.overlay_show({}, pane=second, action=1)
            current = state.overlay
            self.assertFalse(state.overlay_hide(pane=first, action=2)['accepted'])
            self.assertFalse(state.overlay_show({}, pane=first, action=3)['accepted'])
            self.assertIs(state.overlay, current)
            self.assertNotEqual(a['owner'], b['owner'])
            self.assertTrue(state.overlay_release(second, 3)['accepted'])
            self.assertFalse(state.overlay_show({}, pane=second, action=2)['accepted'])
            self.assertIsNone(state.overlay)

    def test_legacy_helper_protocol_is_rejected_with_upgrade_instruction(self):
        if bridge.sys.platform not in ('darwin', 'linux'):
            self.skipTest('real POSIX helper script requires macOS or Linux')
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            helper = Path(directory) / 'legacy-overlay'
            helper.write_text('#!/bin/sh\nif [ "$1" = "--protocol-version" ]; then exit 2; fi\nexec sleep 30\n')
            helper.chmod(0o700)
            overlay = bridge.OverlayBridge(helper=str(helper))
            try:
                result = overlay.show_overlay()
                self.assertFalse(result['shown'])
                self.assertIn('prepare_macos.py', result['error'])
                self.assertIn('protocol', result['error'].lower())
                self.assertIsNone(overlay.process)
            finally:
                overlay.stop()


    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'POSIX pipe/select required')
    def test_run_forwards_snapshot_to_actual_os_pipe(self):
        reader, writer = os.pipe()
        stream = os.fdopen(writer, 'wb', buffering=0)
        process = Mock(stdin=stream, stdout=None)
        process.poll.return_value = None
        process.wait.return_value = 0
        transport = Mock()
        transport.poll_status.return_value = {
            'generation': 1, 'capture_generation': 1, 'listening': True,
            'segments': [{'id': 'row', 'text': 'Who approves?'}]}
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.001)
        overlay.process = process
        runner = threading.Thread(target=overlay.run, daemon=True)
        runner.start()
        try:
            ready, _, _ = select.select([reader], [], [], 1)
            self.assertTrue(ready, 'run() queued snapshots but never wrote the pipe')
            frame = json.loads(os.read(reader, 65536).splitlines()[0])
            self.assertEqual(frame['questions'][0]['question'], 'Who approves?')
            self.assertTrue(frame['snapshot_id'])
        finally:
            overlay.stop()
            runner.join(2)
            stream.close()
            os.close(reader)
        self.assertFalse(runner.is_alive())
