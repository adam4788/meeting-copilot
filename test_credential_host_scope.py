"""Installed Hermes request-scope regressions; synthetic keys, no provider/network.

Run with Hermes' Python. Scope/tokenizer/lock/writer are never stubbed. Only the
owner principal (normally authenticated by Hermes) and provider transports are
supplied by this test. Each scenario starts in a clean, private child HOME.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HARNESS = r'''
import io, json, os, wave
from pathlib import Path
from unittest.mock import patch
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hermes_constants import pin_process_hermes_home, set_hermes_home_override, reset_hermes_home_override
from agent import secret_scope
from hermes_cli.web_server_profiles import _config_profile_scope
home = Path(os.environ['HERMES_HOME'])
pin_process_hermes_home(str(home))
import credentials, meta_models, model_selection, macos_audio, meta_asr
path = home / '.env'
async def scope():
    with _config_profile_scope('current'):
        yield
app = FastAPI(dependencies=[Depends(scope)])
@app.middleware('http')
async def owner(request, next):
    request.state.native_http_principal = {'profile_id': str(home), 'capabilities': frozenset({'http:owner'})}
    return await next(request)
app.include_router(credentials.make_router())
expected = ''
retained = []
wave_path = home / 'synthetic.wav'
with wave.open(str(wave_path), 'wb') as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
    wav.writeframes(bytes(480))
def check_model(complete, key):
    def provider(request, timeout):
        assert request.get_header('Authorization') == 'Bearer ' + key, 'wrong model credential'
        return io.BytesIO(b'{"choices":[{"message":{"content":"synthetic answer"}}]}')
    with patch.object(meta_models.urllib.request, 'urlopen', side_effect=provider):
        assert complete([{'role':'user','content':'synthetic question'}]) == 'synthetic answer'
def check_asr(key, wanted):
    assert key == wanted, 'wrong ASR credential'
    if not wanted:
        return
    assert meta_asr.build_handshake(key)['authorization']['accessToken'] == 'Bearer ' + wanted
    def provider(request):
        assert request.get_header('Authorization') == 'Bearer ' + wanted, 'wrong ASR HTTP credential'
        return io.BytesIO(b'{"transcript":"synthetic transcript"}')
    assert meta_asr.transcribe_file(str(wave_path), key, opener=provider)[0]['text'] == 'synthetic transcript'
@app.get('/probe')
def probe():
    assert secret_scope.current_secret_scope() is not None
    assert meta_models.resolve_model_api_key() == expected, 'wrong scoped credential'
    assert macos_audio.worker_env().get('MODEL_API_KEY', '') == expected, 'wrong worker credential'
    check_asr(macos_audio._model_api_key(), expected)
    if expected:
        complete = model_selection.selected_completion('meta', None)
        check_model(complete, expected)
        retained.append((complete, macos_audio.worker_env(), expected))
    else:
        try:
            model_selection.selected_completion('meta', None)
        except ValueError:
            pass
        else:
            raise AssertionError('Meta accepted absent credential')
    return {'ok': True}
client = TestClient(app)
def put(raw):
    path.write_bytes(raw); path.chmod(0o600)
def status():
    response = client.get('/credentials/meta')
    assert response.status_code == 200, 'status refused'
    assert 'fake-' not in response.text, 'secret reflected'
    return response.json()
def edit(action, revision=None):
    body = {'action': action, 'revision': revision or status()['revision']}
    if action == 'save': body['key'] = 'fake-saved-key'
    response = client.post('/credentials/meta', json=body)
    assert 'fake-' not in response.text, 'secret reflected'
    return response
'''


@unittest.skipUnless(importlib.util.find_spec('agent'), 'requires installed Hermes scope helpers')
class InstalledHostScopeTests(unittest.TestCase):
    def child(self, script, inherited=''):
        with tempfile.TemporaryDirectory() as directory:
            env = {'HOME': directory, 'HERMES_HOME': directory, 'TMPDIR': directory,
                   'PATH': os.defpath, 'MODEL_API_KEY': inherited}
            result = subprocess.run([sys.executable, '-'], input=HARNESS + script,
                                    env=env, text=True, capture_output=True, timeout=45)
            # Tracebacks contain only test source/assertion labels, never values.
            self.assertNotIn('fake-', result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_ambiguous_files_refused_before_any_mutation(self):
        self.child(r'''
fixtures = [
    b'OTHER="first\nMODEL_API_KEY=fake-embedded-key\nlast"\nMODEL_API_KEY=fake-assignment\n',
    b'OTHER="first\nsecond"\n',
    b'\xef\xbb\xbfMODEL_API_KEY=fake-bom-key\n',
    b"'MODEL_API_KEY'=fake-quoted-name\n",
    b'export\tMODEL_API_KEY=fake-tab-export\n',
    b'OTHER="escaped\\nnewline"\n',
]
for raw in fixtures:
    put(raw)
    before = path.stat()
    revision = credentials._revision(path, raw)
    response = client.get('/credentials/meta')
    assert response.status_code == 503, 'ambiguous status must be unavailable'
    assert 'unsupported' in response.text.lower(), 'explicit unsupported-file report required'
    assert 'fake-' not in response.text, 'secret reflected'
    for action in ('save', 'remove'):
        response = edit(action, revision)
        assert response.status_code == 503, 'ambiguous edit must be refused'
        assert 'unsupported' in response.text.lower()
        assert path.read_bytes() == raw, 'unrelated bytes changed'
        after = path.stat()
        for field in ('st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
            assert getattr(after, field) == getattr(before, field), 'file metadata changed'
        # Reads may update atime; inode/permissions/content metadata must not change.
        assert not (home / '.env.lock').exists(), 'validation must precede lock creation'
        assert not list(home.glob('.env-stage-*')), 'validation must precede staging'
''')

    def test_actual_scoped_meta_and_asr_after_removal_no_residual(self):
        self.child(r'''
unrelated = b'# keep\r\nOTHER="single line" # keep\r\n\r\n'
put(unrelated + b'export MODEL_API_KEY="fake-first-key"\nMODEL_API_KEY=fake-last-key\n')
expected = 'fake-last-key'
assert client.get('/probe').json()['ok']
response = edit('remove')
assert response.status_code == 200 and response.json()['saved'] is False
assert path.read_bytes() == unrelated
assert 'MODEL_API_KEY' not in secret_scope.load_env_file(path), 'host tokenizer retained credential'
from dotenv import dotenv_values
assert 'MODEL_API_KEY' not in dotenv_values(path), 'dotenv retained credential'
expected = ''
assert client.get('/probe').json()['ok']
assert status()['request_configured'] is False
''')

    def test_actual_request_activation_status_and_retained_workers(self):
        for inherited in ('', 'fake-inherited-key'):
            with self.subTest(inherited=bool(inherited)):
                self.child(r'''
expected = os.environ['MODEL_API_KEY']
assert client.get('/probe').json()['ok']
before = status()
assert before['request_configured'] == bool(expected)
assert before['activation'] == 'host-scoped'
assert 'reload_required' not in before and 'runtime_override' not in before
response = edit('save')
assert response.status_code == 200
# POST runs under the pre-write snapshot; GET is a fresh actual request scope.
assert response.json()['request_configured'] == bool(expected)
expected = 'fake-saved-key'
assert client.get('/probe').json()['ok']
after = status()
assert after['request_configured'] is True and after['verified'] is False
assert after['process_configured'] == bool(os.environ['MODEL_API_KEY'])
assert after['process_differs_from_saved'] == bool(os.environ['MODEL_API_KEY'])
assert edit('remove').status_code == 200
expected = os.environ['MODEL_API_KEY']
assert client.get('/probe').json()['ok']
assert status()['request_configured'] == bool(expected)
# Previously constructed requests/workers are snapshots, not magically revoked.
for complete, worker, old_key in retained:
    check_model(complete, old_key)
    check_asr(worker.get('MODEL_API_KEY', ''), old_key)
assert meta_models.resolve_model_api_key() == expected, 'editor changed unscoped environment'
''', inherited)

    def test_actual_foreign_request_scope_never_borrows_owner_key(self):
        self.child(r'''
put(b'MODEL_API_KEY=fake-owner-disk-key\n')
foreign = home / 'profiles' / 'foreign'
foreign.mkdir(parents=True)
for raw in (b'', b'MODEL_API_KEY=fake-foreign-key\n'):
    (foreign / '.env').write_bytes(raw)
    token = set_hermes_home_override(str(foreign))
    try:
        expected = 'fake-foreign-key' if raw else ''
        with _config_profile_scope('current'):
            assert meta_models.resolve_model_api_key() == expected, 'foreign scope borrowed owner key'
            check_asr(macos_audio._model_api_key(), expected)
            assert macos_audio.worker_env().get('MODEL_API_KEY', '') == expected
            if expected:
                check_model(model_selection.selected_completion('meta', None), expected)
            try:
                credentials._host()
            except RuntimeError:
                pass
            else:
                raise AssertionError('foreign editor scope accepted')
    finally:
        reset_hermes_home_override(token)
assert os.environ['MODEL_API_KEY'] == 'fake-inherited-key'
''', 'fake-inherited-key')


def ui_bridge():
    """Private stdio bridge for real pane handlers + installed host scopes."""
    bridge = r'''
import sys
for line in sys.stdin:
    command = json.loads(line)
    if command['path'] == '/test/saved-effect':
        value = path.read_text() if path.exists() else ''
        result = {'saved': 'MODEL_API_KEY=fake-ui-key-only\n' in value,
                  'mode_private': not path.exists() or path.stat().st_mode & 0o777 == 0o600,
                  'runtime_unchanged': os.environ.get('MODEL_API_KEY', '') == ''}
        code = 200
    else:
        opts = command.get('opts') or {}
        response = client.request(opts.get('method', 'GET'), command['path'],
            **({'json': opts['body']} if 'body' in opts else {}))
        result, code = response.json(), response.status_code
    print(json.dumps({'status': code, 'body': result}), flush=True)
'''
    with tempfile.TemporaryDirectory() as directory:
        env = {'HOME': directory, 'HERMES_HOME': directory, 'TMPDIR': directory,
               'PATH': os.defpath, 'MODEL_API_KEY': ''}
        result = subprocess.run([sys.executable, '-u', '-c', HARNESS + bridge], env=env)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    if '--ui-bridge' in sys.argv:
        ui_bridge()
    else:
        unittest.main()
