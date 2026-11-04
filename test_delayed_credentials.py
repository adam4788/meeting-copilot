"""Deterministic host-scope -> router -> native click publication races."""
import importlib.util
import unittest
import test_overlay_credentials as overlay_tests
SCENARIO = overlay_tests.SCENARIO

RACE = r'''
    gate, proceed = threading.Event(), threading.Event()
    original = getattr(state, METHOD)
    replies, errors = [], []
    def gated(*args, **kwargs):
        try:
            assert meta_models.resolve_model_api_key() == 'fake-original-key', 'wrong gate snapshot'
            gate.set()
            assert proceed.wait(8), 'request gate timed out'
            return original(*args, **kwargs)
        except BaseException as exc:
            errors.append(type(exc).__name__)
            raise
    payload = ({'scope': {'owner': owner, 'seq': 1, 'answer_provider': 'meta', 'answer_model': DEFAULT_META_MODEL}}
               if METHOD == 'overlay_scope' else
               {'pane': pane, 'action': 2, 'scope': {'seq': 1, 'answer_provider': 'meta', 'answer_model': DEFAULT_META_MODEL}})
    try:
        with patch.object(state, METHOD, gated):
            worker = threading.Thread(target=lambda: replies.append(client.post(ROUTE, json=payload)))
            worker.start()
            assert gate.wait(5), 'request did not capture old scope'
            response = edit(ACTION)
            assert response.status_code == 200, 'edit failed'
            assert response.json()['overlay_credentials'] == 'refreshed', 'rebind failed'
            wanted = 'fake-saved-key' if ACTION == 'save' else os.environ.get('MODEL_API_KEY', '')
            assert overlay.transport._poll_context.copy().run(meta_models.resolve_model_api_key) == wanted, 'wrong rebound scope'
            proceed.set(); worker.join(5)
            assert not worker.is_alive() and not errors, 'gated request failed'
            assert replies[0].status_code == 200, 'fresh scope should permit request'
        click('post-edit', 'What is the new question after the delayed request?'); drain()
        assert seen == (['Bearer ' + wanted] if wanted else []), 'delayed publication resurrected old key'
        assert 'fake-' not in response.text + replies[0].text + client.get('/status').text, 'response leak'
    finally:
        proceed.set(); state.listening = False
        worker.join(5)
        state.overlay_hide()
        for thread in list(overlay.threads): thread.join(5)
'''

INITIAL = r'''
put(b'MODEL_API_KEY=fake-original-key\n')
with client, patch.object(meta_models.urllib.request, 'urlopen', side_effect=provider), _fake_overlay_spawn(), patch.object(state, '_start_overlay_lease_locked'):
    ticket = client.get('/overlay/lifecycle').json()
    pane = client.post('/overlay/claim', json={**ticket, 'client': 'credential-test', 'creation': 1}).json()['pane']
    state.listening = True
    gate, proceed = threading.Event(), threading.Event()
    original = state.overlay_show
    replies, errors = [], []
    def gated(*args, **kwargs):
        try:
            assert meta_models.resolve_model_api_key() == 'fake-original-key', 'wrong gate snapshot'
            gate.set(); assert proceed.wait(8), 'request gate timed out'
            return original(*args, **kwargs)
        except BaseException as exc:
            errors.append(type(exc).__name__)
            raise
    try:
        with patch.object(state, 'overlay_show', gated):
            worker = threading.Thread(target=lambda: replies.append(client.post('/overlay/show', json={'pane': pane, 'action': 1, 'scope': {'answer_provider': 'meta', 'answer_model': DEFAULT_META_MODEL}})))
            worker.start(); assert gate.wait(5), 'gate not reached'
            assert state.pending_overlay is None and state.overlay is None
            response = edit(ACTION)
            assert response.status_code == 200 and response.json()['overlay_credentials'] == 'not_shown'
            proceed.set(); worker.join(5)
            assert not worker.is_alive() and not errors, 'gated request failed'
        assert replies[0].status_code == 200
        overlay = state.overlay
        state.add_segment({'id': 'fresh', 'text': 'What is the fresh post-edit question?'})
        overlay.update(state.status(touch=False))
        assert overlay.on_click_line(json.dumps({'type': 'click', 'id': 'fresh', 'snapshot_id': overlay.snapshot_id}))
        for thread in list(overlay.threads):
            if '_drain_clicks' in thread.name:
                thread.join(5); assert not thread.is_alive()
        wanted = 'fake-saved-key' if ACTION == 'save' else ''
        assert seen == (['Bearer ' + wanted] if wanted else []), 'initial Show resurrected old key'
        assert 'fake-' not in response.text + replies[0].text + client.get('/status').text, 'response leak'
    finally:
        proceed.set(); worker.join(5)
        state.listening = False; state.overlay_hide()
'''

@unittest.skipUnless(importlib.util.find_spec('agent'), 'requires installed Hermes scope helpers')
class DelayedCredentialPublicationTests(unittest.TestCase):
    child = overlay_tests.OverlayCredentialTests.child

    def test_delayed_scope_and_reused_show_matrix(self):
        for method, route in [('overlay_scope', '/overlay/scope'), ('overlay_show', '/overlay/show')]:
            for action in ('save', 'remove'):
                for inherited in ('', 'fake-inherited-key'):
                    with self.subTest(method=method, action=action, inherited=bool(inherited)):
                        self.child('METHOD='+repr(method)+'\nROUTE='+repr(route)+'\nACTION='+repr(action)+'\n'+SCENARIO.split('    try:')[0]+RACE, inherited)

    def test_foreign_scoped_action_cannot_take_over_transport(self):
        for method in ('overlay_show', 'overlay_scope'):
            with self.subTest(method=method):
                self.child('METHOD='+repr(method)+'\n'+SCENARIO.split('    try:')[0] + r'''
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.web_server_profiles import _config_profile_scope
    foreign = home / 'foreign'
    foreign.mkdir()
    target = getattr(state, METHOD)
    def routed(*args, **kwargs):
        token = set_hermes_home_override(foreign)
        try:
            with _config_profile_scope('current'):
                return target(*args, **kwargs)
        finally: reset_hermes_home_override(token)
    try:
        before = overlay.transport._poll_context
        payload = ({'scope': {'owner': owner, 'seq': 1, 'project': 'changed'}} if METHOD == 'overlay_scope'
                   else {'pane': pane, 'action': 2, 'scope': {}})
        with patch.object(state, METHOD, routed):
            response = client.post('/overlay/scope' if METHOD == 'overlay_scope' else '/overlay/show', json=payload)
        assert response.status_code == 503, 'foreign profile took over retained transport'
        assert 'fake-' not in response.text
        assert overlay.transport._poll_context is None, 'identity mismatch did not fail closed'
    finally:
        state.listening = False; state.overlay_hide()
''')

    def test_refresh_unavailable_rejects_every_publication(self):
        for method in ('initial', 'overlay_show', 'overlay_scope'):
            for fault in ('raise', 'empty', 'noop', 'missing-api'):
                with self.subTest(method=method, fault=fault):
                    self.child('METHOD='+repr(method)+'\nFAULT='+repr(fault)+'\n'+SCENARIO.split('    try:')[0] + r'''
    from contextlib import contextmanager
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    @contextmanager
    def unavailable(*args):
        if FAULT == 'raise': raise RuntimeError('fake-sensitive-host-failure')
        if FAULT == 'noop':
            yield; return
        token = set_secret_scope(None)
        try: yield
        finally: reset_secret_scope(token)
    target = state.overlay_scope if METHOD == 'overlay_scope' else state.overlay_show
    def faulted(*args, **kwargs):
        # Fault only the fresh admission reload, after real request scope capture.
        if FAULT == 'missing-api':
            import agent.secret_scope as scopes
            with patch.dict(scopes.__dict__):
                del scopes.current_secret_scope_home
                return target(*args, **kwargs)
        with patch('hermes_cli.web_server_profiles._config_profile_scope', unavailable):
            return target(*args, **kwargs)
    try:
        if METHOD == 'initial': state.overlay_hide()
        route = '/overlay/scope' if METHOD == 'overlay_scope' else '/overlay/show'
        payload = ({'scope': {'owner': owner, 'seq': 1, 'project': 'changed'}} if METHOD == 'overlay_scope'
                   else {'pane': pane, 'action': 2, 'scope': {}})
        with patch.object(state, 'overlay_scope' if METHOD == 'overlay_scope' else 'overlay_show', faulted):
            response = client.post(route, json=payload)
        assert response.status_code == 503, 'fresh-scope failure accepted'
        assert 'fake-' not in response.text, 'scope failure reflected'
        assert state.overlay is None or state.overlay.transport._poll_context is None, 'failed reload kept admissions open'
        assert not seen, 'failed reload reached provider'
    finally:
        state.listening = False; state.overlay_hide()
''')

    def test_delayed_action_cannot_replace_newer_owner(self):
        for method, route in [('overlay_scope', '/overlay/scope'), ('overlay_show', '/overlay/show')]:
            scenario = RACE.replace("            proceed.set(); worker.join(5)", "            newer = client.post('/overlay/claim', json={**client.get('/overlay/lifecycle').json(), 'client': 'credential-test', 'creation': 2}).json()['pane']\n            assert state.overlay_pane == newer\n            proceed.set(); worker.join(5)")
            scenario = scenario.replace("        click('post-edit', 'What is the new question after the delayed request?'); drain()\n        assert seen == (['Bearer ' + wanted] if wanted else []), 'delayed publication resurrected old key'", "        assert replies[0].json()['accepted'] is False, 'old owner took over newer pane'\n        assert state.overlay is None and state.overlay_pane == newer\n        assert not seen, 'stale owner reached provider'")
            with self.subTest(method=method):
                self.child('METHOD='+repr(method)+'\nROUTE='+repr(route)+"\nACTION='save'\n"+SCENARIO.split('    try:')[0]+scenario)

    def test_uncertain_write_keeps_retirement_warning(self):
        self.child(SCENARIO.split('    try:')[0] + r'''
    writer = credentials._write
    def failed(*args):
        writer(*args)
        raise RuntimeError('fake-sensitive-writer-error')
    try:
        with patch.object(credentials, '_write', side_effect=failed), patch.object(overlay.transport, 'refresh_credentials', side_effect=RuntimeError('fake-sensitive-scope-error')), patch.object(overlay, 'hide_overlay', return_value={'stop_confirmed': False, 'error': 'fake-sensitive-cleanup-error'}):
            response = edit('save')
            assert response.status_code == 503
            assert response.json().get('overlay_credentials') == 'retired', 'retirement outcome lost on write failure'
            assert response.json().get('overlay_stop_confirmed') is False, 'shutdown warning lost on write failure'
            assert 'fake-' not in response.text, 'error leaked'
    finally:
        state.listening = False; state.overlay_hide()
''')

    def test_delayed_initial_show_matrix(self):
        for action in ('save', 'remove'):
            with self.subTest(action=action):
                self.child('ACTION='+repr(action)+'\n'+INITIAL)
