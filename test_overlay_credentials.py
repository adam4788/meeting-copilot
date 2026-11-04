"""Real host scopes + router + native-click admission; synthetic provider only."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from test_credential_host_scope import HARNESS


SETUP = r'''
import threading
import server
from model_selection import selected_completion
from meta_models import DEFAULT_META_MODEL
from test_overlay_bridge import _fake_overlay_spawn
seen = []
entered, release = threading.Event(), threading.Event()
release.set()
class Engine:
    def supports_streaming(self, **kw): return False
    def answer(self, question, **kw):
        complete = selected_completion('meta', DEFAULT_META_MODEL)
        entered.set()
        assert release.wait(5), 'answer gate timed out'
        return {'answer': complete([{'role': 'user', 'content': question}])}
def provider(request, timeout):
    seen.append(request.get_header('Authorization'))
    return io.BytesIO(b'{"choices":[{"message":{"content":"synthetic answer"}}]}')
state = server.MeetingState(engine_factory=Engine)
app.include_router(server.make_router(state))
'''
SCENARIO = r'''
put(b'MODEL_API_KEY=fake-original-key\n')
with client, patch.object(meta_models.urllib.request, 'urlopen', side_effect=provider), _fake_overlay_spawn(), patch.object(state, '_start_overlay_lease_locked'):
    ticket = client.get('/overlay/lifecycle').json()
    pane = client.post('/overlay/claim', json={**ticket, 'client': 'credential-test', 'creation': 1}).json()['pane']
    state.listening = True
    state.add_segment({'id': 'old', 'text': 'What was the original question?'})
    shown = client.post('/overlay/show', json={'pane': pane, 'action': 1, 'scope': {'seq': 0, 'answer_provider': 'meta', 'answer_model': DEFAULT_META_MODEL}})
    assert shown.status_code == 200, 'show failed'
    overlay = state.overlay
    owner = state.overlay_owner
    def click(row, text):
        state.add_segment({'id': row, 'text': text})
        overlay.update(state.status(touch=False))
        assert overlay.on_click_line(json.dumps({'type': 'click', 'id': row, 'snapshot_id': overlay.snapshot_id})), 'click rejected'
    def drain():
        for thread in list(overlay.threads):
            if '_drain_clicks' in thread.name:
                thread.join(5)
                assert not thread.is_alive(), 'click drain timed out'
    try:
        release.clear()
        click('old', 'What was the original question?')
        assert entered.wait(5), 'old answer did not start'
        response = edit('save')
        assert response.status_code == 200, 'replacement failed'
        release.set(); drain()
        assert seen == ['Bearer fake-original-key'], 'existing answer snapshot was revoked'
        assert state.overlay is overlay and state.overlay_owner == owner and state.overlay_pane == pane, 'credential edit changed lifecycle ownership'
        click('new', 'What is the replacement question?'); drain()
        assert seen[-1] == 'Bearer fake-saved-key', 'fresh overlay question used retained key'
        assert edit('remove').status_code == 200, 'removal failed'
        before = len(seen)
        click('removed', 'What happens after removal?'); drain()
        inherited = os.environ.get('MODEL_API_KEY', '')
        if inherited:
            assert len(seen) == before + 1 and seen[-1] == 'Bearer ' + inherited, 'inherited key was revoked or saved key survived'
        else:
            assert len(seen) == before, 'removed saved key reached provider'
        assert state.listening, 'credential edit paused capture'
        assert state.overlay_owner == owner and state.overlay_pane == pane
        assert 'fake-' not in client.get('/status').text
    finally:
        release.set(); state.listening = False
        state.overlay_hide()
        for thread in list(overlay.threads): thread.join(5)
'''


@unittest.skipUnless(importlib.util.find_spec('agent'), 'requires installed Hermes scope helpers')
class OverlayCredentialTests(unittest.TestCase):
    def child(self, script, inherited=''):
        harness = HARNESS.replace('app.include_router(credentials.make_router())', SETUP)
        with tempfile.TemporaryDirectory() as directory:
            env = {'HOME': directory, 'HERMES_HOME': directory, 'TMPDIR': directory,
                   'PATH': os.defpath, 'MODEL_API_KEY': inherited}
            result = subprocess.run([sys.executable, '-'], input=harness + script,
                                    env=env, text=True, capture_output=True, timeout=50)
            self.assertNotIn('fake-', result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_edit_retires_pending_startup_without_publishing_old_context(self):
        self.child(r'''
import overlay_bridge
put(b'MODEL_API_KEY=fake-original-key\n')
started, proceed = threading.Event(), threading.Event()
with client, _fake_overlay_spawn(), patch.object(state, '_start_overlay_lease_locked'):
    ticket = client.get('/overlay/lifecycle').json()
    pane = client.post('/overlay/claim', json={**ticket, 'client': 'credential-test', 'creation': 1}).json()['pane']
    launch = overlay_bridge.OverlayBridge.start_helper
    def gated(bridge):
        started.set()
        assert proceed.wait(5), 'startup gate timed out'
        return launch(bridge)
    replies = []
    with patch.object(overlay_bridge.OverlayBridge, 'start_helper', gated):
        worker = threading.Thread(target=lambda: replies.append(client.post('/overlay/show', json={'pane': pane, 'action': 1, 'scope': {}})))
        worker.start()
        try:
            assert started.wait(5), 'startup not reached'
            bridge = state.pending_overlay
            response = edit('save')
            assert response.status_code == 200, 'save failed during startup'
            payload = response.json()
            assert payload['overlay_credentials'] == 'retired', 'pending startup retained old credentials'
            assert payload['overlay_stop_confirmed'] is False, 'pending launch claimed stopped'
            assert state.pending_overlay is bridge and state.overlay is None, 'startup ownership lost'
            assert state.overlay_pane == pane
        finally:
            proceed.set(); worker.join(5)
            assert not worker.is_alive(), 'show worker stuck'
            state.overlay_hide()
        assert replies[0].status_code == 503, 'retired startup published an old credential context'
        assert state.overlay is None and state.pending_overlay is None
''')

    def test_rebind_failure_retires_overlay_and_reports_unconfirmed_shutdown(self):
        for denied in (False, True):
            scenario = SCENARIO.split('    try:')[0] + r'''
    from contextlib import ExitStack
    before_seen = state.pane_seen
    try:
        with ExitStack() as faults:
            faults.enter_context(patch('hermes_cli.web_server_profiles._config_profile_scope', side_effect=RuntimeError('fake-sensitive-context-error')))
            if DENIED:
                faults.enter_context(patch.object(overlay, 'hide_overlay', return_value={'stop_confirmed': False, 'error': 'synthetic teardown failure'}))
            response = edit('save')
            assert response.status_code == 200, 'saved key incorrectly reported as not written'
            payload = response.json()
            assert payload['saved'] and payload['overlay_credentials'] == 'retired', 'missing retirement outcome'
            assert payload['overlay_stop_confirmed'] is not DENIED, 'dishonest shutdown outcome'
            assert 'fake-' not in response.text, 'credential exception reflected'
            assert state.overlay is None and state.overlay_owner is None, 'failed context stayed authorized'
            assert state.overlay_pane == pane and state.pane_seen == before_seen, 'credential update minted or renewed pane authority'
            assert state.listening, 'credential error stopped capture'
            assert not overlay.on_click_line(json.dumps({'type': 'click', 'id': 'old', 'snapshot_id': overlay.snapshot_id})), 'retired overlay accepted new work'
            assert not seen, 'retired overlay reached provider'
        assert status()['saved'], 'write lost on rebind failure'
    finally:
        state.listening = False
        state.overlay_hide()
        for thread in list(overlay.threads): thread.join(5)
'''
            with self.subTest(denied=denied):
                self.child(scenario.replace('DENIED', repr(denied)))

    def test_partial_writer_failure_still_rebinds_committed_credentials(self):
        scenario = SCENARIO.split('    try:')[0] + r'''
    writer = credentials._write
    def committed_then_failed(*args):
        writer(*args)
        raise RuntimeError('fake-sensitive-write-error')
    try:
        with patch.object(credentials, '_write', side_effect=committed_then_failed):
            response = edit('save')
        assert response.status_code == 503, 'partial write reported as success'
        assert 'fake-' not in response.text, 'writer exception leaked'
        assert status()['saved'], 'fault did not commit'
        click('new', 'What happens after an uncertain write?'); drain()
        assert seen == ['Bearer fake-saved-key'], 'uncertain write retained stale context'
        assert state.overlay_pane == pane and state.overlay_owner == owner
    finally:
        state.listening = False
        state.overlay_hide()
        for thread in list(overlay.threads): thread.join(5)
'''
        self.child(scenario)

    def test_shown_overlay_rebinds_fresh_questions_preserving_inflight_and_inherited(self):
        for inherited in ('', 'fake-inherited-key'):
            with self.subTest(inherited=bool(inherited)):
                self.child(SCENARIO, inherited)


if __name__ == '__main__':
    unittest.main()
