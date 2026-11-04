"""Offline review bridge, production router/secret scopes; no network/hardware."""
import sys, os, json, socket, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
def forbidden(*args, **kwargs):
    raise AssertionError('Network forbidden in offline review')
socket.socket.connect = forbidden
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from unittest.mock import patch
from contextlib import ExitStack
from hermes_constants import pin_process_hermes_home
from hermes_cli.web_server_profiles import _config_profile_scope
home = Path(os.environ['HERMES_HOME'])
pin_process_hermes_home(str(home))
import server, meta_models, macos_audio, credentials
from test_overlay_bridge import _fake_overlay_spawn
class Audio:
    running = False
    key = ''
    def start(self, **kw):
        self.key = macos_audio.worker_env().get('MODEL_API_KEY', '')
        self.running = True
    def stop(self): self.running = False
    def status(self): return {'running': self.running, 'stop_confirmed': not self.running}
audio = Audio()
class FakeEngine:
    provider_failure = False
    engine_failure = False
    hold = threading.Event()
    hold.set()
    def answer_stream(self, question, **kwargs):
        if self.hold.is_set():
            raise NotImplementedError
        def chunks():
            yield 'Synthetic live partial'
            self.hold.wait(10)
            yield ' completed'
        return {}, chunks()
    def answer(self, question, **kwargs):
        if self.engine_failure:
            raise RuntimeError('synthetic-sensitive-engine\nTraceback: synthetic-stack')
        if self.provider_failure:
            return {'answer': '', 'sources': [], 'warnings': [],
                    'provider_error': 'Provider authentication failed (HTTP 401). Check your API key.'}
        return {'answer': 'Synthetic offline answer', 'sources': [], 'warnings': []}
    def projects(self): return []
state = server.MeetingState(macos_factory=lambda **kw: audio, engine_factory=FakeEngine)
owner_allowed = True
async def scoped():
    with _config_profile_scope('current'): yield
app = FastAPI(dependencies=[Depends(scoped)])
@app.middleware('http')
async def owner(request, next):
    if owner_allowed:
        request.state.native_http_principal = {'profile_id': str(home), 'capabilities': frozenset({'http:owner'})}
    return await next(request)
app.include_router(server.make_router(state))
@app.get('/review/probe')
def probe():
    raw = (home / '.env').read_text() if (home / '.env').exists() else ''
    transport = state.overlay.transport if state.overlay else None
    context = transport._poll_context if transport else None
    overlay_key = context.copy().run(meta_models.resolve_model_api_key) if context else ''
    return {'overlay_owner': state.overlay_owner,
            'saved_v1': 'fake-review-key-one' in raw, 'saved_v2': 'fake-review-key-two' in raw,
            'running': audio.running, 'capture_v1': audio.key == 'fake-review-key-one',
            'capture_v2': audio.key == 'fake-review-key-two',
            'overlay_v1': overlay_key == 'fake-review-key-one', 'overlay_v2': overlay_key == 'fake-review-key-two',
            'scoped_v2': meta_models.resolve_model_api_key() == 'fake-review-key-two',
            'process_unchanged': not os.environ.get('MODEL_API_KEY'),
            'private': not (home / '.env').exists() or (home / '.env').stat().st_mode & 0o777 == 0o600}
with ExitStack() as stack:
    stack.enter_context(_fake_overlay_spawn())
    stack.enter_context(patch.object(state, 'watchdog'))
    stack.enter_context(patch.object(state, '_start_overlay_lease_locked'))
    stack.enter_context(patch.object(state, '_macos_runtime_ready', return_value=True))
    stack.enter_context(patch.object(state, '_overlay_capability_available', return_value=True))
    client = stack.enter_context(TestClient(app))
    for line in sys.stdin:
        m = json.loads(line)
        if m.get('control') == 'owner':
            owner_allowed = m['value']; result = {'code': 200, 'body': {}}
        elif m.get('control') == 'lifecycle-fixture':
            state.listening = m['listening']
            state.add_segment({'text': 'Synthetic question?'})
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'adapter-fault':
            action = m['action']
            if action in ('start', 'pause'):
                stack.enter_context(patch.object(audio, 'start' if action == 'start' else 'stop', side_effect=RuntimeError('synthetic-sensitive-adapter-diagnostic')))
                stack.enter_context(patch.object(state, '_start_stop_retry_locked'))
            else:
                from types import SimpleNamespace
                def fail_forget(*args):
                    raise RuntimeError('synthetic-sensitive-clear-diagnostic')
                state.bridge = SimpleNamespace(note_cleared=fail_forget)
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'cleanup-recover':
            assert state.pending_overlay is not None
            bridge = state.pending_overlay
            bridge.hide_overlay = type(bridge).hide_overlay.__get__(bridge)
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'hold-answer':
            FakeEngine.hold.clear()
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'release-answer':
            FakeEngine.hold.set()
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'engine-failure':
            FakeEngine.engine_failure = m.get('value', True)
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'provider-failure':
            FakeEngine.provider_failure = True
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'drop-answers':
            with state.lock:
                state.requests.clear()
                state.result = None
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'segment':
            state.add_segment({'text': 'Synthetic question?'})
            result = {'code': 200, 'body': {}}
        elif m.get('control') == 'refresh-fault':
            assert state.overlay is not None
            stack.enter_context(patch.object(state.overlay.transport, 'refresh_credentials', side_effect=RuntimeError('synthetic-sensitive-fault')))
            result = {'code': 200, 'body': {}}
        elif m.get('control') in ('write-shutdown-fault', 'conflict-shutdown-fault'):
            assert state.overlay is not None
            writer = credentials._write
            def committed_then_failed(*args):
                writer(*args)
                raise RuntimeError('synthetic-sensitive-write-fault')
            def conflict(host, path, raw, action, key):
                path.write_bytes(raw + b'# synthetic competing edit\n')
                return writer(host, path, raw, action, key)
            stack.enter_context(patch.object(credentials, '_write', side_effect=conflict if m['control'] == 'conflict-shutdown-fault' else committed_then_failed))
            stack.enter_context(patch.object(state.overlay.transport, 'refresh_credentials', side_effect=RuntimeError('synthetic-sensitive-refresh-fault')))
            stack.enter_context(patch.object(state.overlay, 'hide_overlay', return_value={'stop_confirmed': False, 'error': 'synthetic cleanup denied'}))
            result = {'code': 200, 'body': {}}
        else:
            response = client.request(m.get('method', 'GET'), m['path'], **({'json': m['body']} if 'body' in m else {}))
            result = {'code': response.status_code, 'body': response.json()}
        print(json.dumps({'id': m['id'], **result}), flush=True)
    FakeEngine.hold.set()
    state.pause(retain_tail=False)
    state.overlay_hide()
