"""Fresh-start and trusted scope regressions; synthetic homes/keys only."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


host_scope_test = unittest.skipUnless(importlib.util.find_spec('agent') is not None,
                                     'Hermes trusted scope integration requires the host package')


class CredentialRuntimeTests(unittest.TestCase):
    def child(self, script, inherited=None):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.env').write_text('MODEL_API_KEY=fake-disk-key\n')
            env = {'HOME': directory, 'HERMES_HOME': directory, 'TMPDIR': directory,
                   'PATH': os.defpath, 'HERMES_LAUNCHD_LABEL': 'ai.hermes.gateway.test-only'}
            if inherited is not None:
                env['MODEL_API_KEY'] = inherited
            result = subprocess.run([sys.executable, '-'], input=script, text=True,
                                    env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('fake-', result.stdout + result.stderr)

    @host_scope_test
    def test_fresh_start_nonempty_precedence_model_and_asr(self):
        for inherited in (None, '', '  \t ', 'fake-process-key'):
            with self.subTest(inherited=bool(inherited and inherited.strip())):
                self.child('''
import os, io, json
from unittest.mock import patch
expected = os.environ.get('MODEL_API_KEY', '').strip() or 'fake-disk-key'
import meta_models, model_selection, macos_audio, meta_asr
assert meta_models.resolve_model_api_key() == expected
assert macos_audio._model_api_key() == expected
assert meta_asr.build_handshake(macos_audio._model_api_key())['authorization']['accessToken'] == 'Bearer ' + expected
def fake(request, timeout):
    assert request.get_header('Authorization') == 'Bearer ' + expected
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'Synthetic answer'}}]}).encode())
with patch.object(meta_models.urllib.request, 'urlopen', side_effect=fake):
    assert model_selection.selected_completion('meta', None)([{'role': 'user', 'content': 'test'}]) == 'Synthetic answer'
import wave
path = os.path.join(os.environ['HOME'], 'synthetic.wav')
with wave.open(path, 'wb') as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(24000)
    wav.writeframes(bytes(480))
def fake_asr(request):
    assert request.get_header('Authorization') == 'Bearer ' + expected
    return io.BytesIO(json.dumps({'transcript': 'Synthetic transcript'}).encode())
assert meta_asr.transcribe_file(path, macos_audio._model_api_key(), opener=fake_asr)[0]['text'] == 'Synthetic transcript'
''', inherited)

    @host_scope_test
    def test_actual_launch_rejects_host_parser_disagreement(self):
        self.child(r'''
import os
from pathlib import Path
from agent import secret_scope
from hermes_constants import pin_process_hermes_home
from hermes_cli.web_server_profiles import _config_profile_scope
home = Path(os.environ['HERMES_HOME'])
pin_process_hermes_home(str(home))
# dotenv sees the target, but the installed host sees a different name.
(home / '.env').write_text("'MODEL_API_KEY'=fake-quoted-name\n")
assert 'MODEL_API_KEY' not in secret_scope.load_env_file(home / '.env')
import meta_models
assert not os.environ.get('MODEL_API_KEY', '').strip(), 'startup published unsupported key'
assert meta_models.resolve_model_api_key() == ''
with _config_profile_scope('current'):
    assert meta_models.resolve_model_api_key() == '', 'host scope acquired an unsupported launch key'
''')

    def test_launch_without_host_tokenizer_fails_closed(self):
        self.child(r'''
import os, sys
from unittest.mock import patch
# Emulate an absent host and an older host missing the tokenizer, at import time.
from types import SimpleNamespace
with patch.dict(sys.modules, {'agent': SimpleNamespace(secret_scope=SimpleNamespace(serves_routed_profile=lambda: False))}):
    import meta_models
assert not os.environ.get('MODEL_API_KEY', '').strip(), 'startup published a key without host parser'
''')
        self.child(r'''
import os, sys
from unittest.mock import patch
with patch.dict(sys.modules, {'agent': None}):
    import meta_models
assert not os.environ.get('MODEL_API_KEY', '').strip(), 'standalone startup published an unvalidated key'
''')

    @host_scope_test
    def test_trusted_host_scope_foreign_missing_and_owner(self):
        self.child('''
import os
from pathlib import Path
from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_context, reset_multiplex_context
from hermes_constants import pin_process_hermes_home, set_hermes_home_override, reset_hermes_home_override
own = os.environ['HERMES_HOME']
foreign = str(Path(own) / 'profiles' / 'foreign')
Path(foreign).mkdir(parents=True)
pin_process_hermes_home(own)
import meta_models, macos_audio
assert meta_models.resolve_model_api_key() == 'fake-owner-key'
for mapping in ({}, {'MODEL_API_KEY': 'fake-foreign-key'}):
    token = set_secret_scope(mapping, profile_home=foreign)
    try:
        expected = mapping.get('MODEL_API_KEY', '')
        assert meta_models.resolve_model_api_key() == expected
        assert macos_audio._model_api_key() == expected
    finally:
        reset_secret_scope(token)
token = set_hermes_home_override(foreign)
try:
    assert meta_models.resolve_model_api_key() == ''
    assert macos_audio._model_api_key() == ''
finally:
    reset_hermes_home_override(token)
token = set_multiplex_context(True)
try:
    assert meta_models.resolve_model_api_key() == ''
finally:
    reset_multiplex_context(token)
token = set_secret_scope({}, profile_home=own)
try:
    assert meta_models.resolve_model_api_key() == 'fake-owner-key'
finally:
    reset_secret_scope(token)
assert os.environ['MODEL_API_KEY'] == 'fake-owner-key'
''', 'fake-owner-key')

    @host_scope_test
    def test_asr_worker_thread_and_child_keep_request_scope(self):
        self.child('''
import os, sys, subprocess, threading
from pathlib import Path
from unittest.mock import patch
from agent.secret_scope import set_secret_scope, reset_secret_scope
from hermes_constants import pin_process_hermes_home
import macos_audio
own = os.environ['HERMES_HOME']
pin_process_hermes_home(own)
real_popen = subprocess.Popen
for mapping in ({}, {'MODEL_API_KEY': 'fake-foreign-key'}):
    expected = mapping.get('MODEL_API_KEY', '')
    token = set_secret_scope(mapping, profile_home=str(Path(own) / 'profiles' / 'foreign'))
    got, seen = [], threading.Event()
    def factory(command, **kwargs):
        script = "import meta_models,json; assert meta_models.resolve_model_api_key() == " + repr(expected) + "; print(json.dumps({'type':'segment','speaker':'you','text':'scope ok','timestamp':1}),flush=True)"
        return real_popen([sys.executable, '-u', '-c', script], **kwargs)
    capture = macos_audio.MacOSAudioCapture(provider='meta')
    try:
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), patch('subprocess.Popen', factory):
            capture.start(lambda row: (got.append(row), seen.set()))
            assert seen.wait(3), 'Worker did not retain trusted request scope'
            capture.stop(timeout=1)
        assert got[0]['text'] == 'scope ok'
    finally:
        capture.stop(timeout=1)
        reset_secret_scope(token)
''', 'fake-owner-key')

    @host_scope_test
    def test_owner_scope_can_seed_its_saved_key_on_first_import(self):
        self.child('''
import os
from agent.secret_scope import set_secret_scope
from hermes_constants import pin_process_hermes_home
own = os.environ['HERMES_HOME']
pin_process_hermes_home(own)
set_secret_scope({}, profile_home=own)
import meta_models
assert meta_models.resolve_model_api_key() == 'fake-disk-key'
''', '')

    @host_scope_test
    def test_foreign_import_never_seeds_global_environment(self):
        self.child('''
import os
from pathlib import Path
from hermes_constants import pin_process_hermes_home, set_hermes_home_override
own = os.environ['HERMES_HOME']
pin_process_hermes_home(own)
set_hermes_home_override(str(Path(own) / 'profiles' / 'foreign'))
import meta_models
assert not os.environ.get('MODEL_API_KEY', '').strip()
assert meta_models.resolve_model_api_key() == ''
''')

    def test_scope_errors_cannot_fall_back_to_process_key_in_asr(self):
        self.child('''
from unittest.mock import patch
import meta_models, macos_audio
with patch.object(meta_models, 'resolve_model_api_key', side_effect=RuntimeError('scope unavailable')):
    assert macos_audio._model_api_key() == ''
''', 'fake-owner-key')
