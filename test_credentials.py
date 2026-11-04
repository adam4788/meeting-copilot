"""Credential editor tests: synthetic keys, isolated homes, no live services."""
import contextlib
import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'HERMES_HOME': str(self.home), 'MODEL_API_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mod = importlib.import_module('credentials')
        # Only the host integration seams are substituted. All parsing, filesystem
        # validation, locking, atomic writes, routes and status logic run for real.
        self.mod._revision_secret = b'test-only-revision-key'
        self.host = patch.object(self.mod, '_host', return_value=self.fake_host())
        self.host.start()
        self.addCleanup(self.host.stop)
        if importlib.util.find_spec('agent') is None:
            # Portable filesystem/UI unit tests only; installed-host integration
            # tests run separately and NEVER substitute scope/tokenizer helpers.
            import io
            from dotenv import dotenv_values
            from types import SimpleNamespace
            scope = patch.object(self.mod, '_scope', return_value=SimpleNamespace(
                _parse_env_text=lambda text: dict(dotenv_values(stream=io.StringIO(text), interpolate=False)),
                current_secret_scope=lambda: None, get_secret=os.environ.get))
            scope.start()
            self.addCleanup(scope.stop)
        self.app = FastAPI()
        self.owner = True
        self.principal = {'profile_id': str(self.home), 'capabilities': frozenset({'http:owner'})}
        @self.app.middleware('http')
        async def auth(request, next):
            if self.owner:
                request.state.native_http_principal = self.principal
            return await next(request)
        self.app.include_router(self.mod.make_router())
        self.client = TestClient(self.app)

    def fake_host(self):
        import fcntl
        from types import SimpleNamespace
        @contextlib.contextmanager
        def lock(path):
            with open(str(path) + '.lock', 'a') as f:
                os.chmod(f.name, 0o600)
                fcntl.flock(f, fcntl.LOCK_EX)
                yield
        def write(path, lines, preserve_mode=False):
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, 'w', newline='') as f:
                    f.writelines(lines)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return SimpleNamespace(_env_write_lock=lock, _write_env_lines=write,
            require_env_writable=lambda *a: None, invalidate_env_cache=lambda: None)

    def status(self):
        r = self.client.get('/credentials/meta')
        self.assertEqual(r.status_code, 200)
        return r.json()

    def save(self, key='fake-meta-key-one', revision=None):
        return self.client.post('/credentials/meta', json={'action': 'save', 'key': key,
            'revision': revision if revision is not None else self.status()['revision']})

    def test_missing_tokenizer_has_actionable_secret_free_error(self):
        from types import SimpleNamespace
        with patch.object(self.mod, '_scope', return_value=SimpleNamespace()):
            response = self.client.get('/credentials/meta')
            self.assertEqual(response.status_code, 503)
            self.assertIn('credential_tokenizer_unavailable', response.text)
            self.assertIn('Upgrade Hermes', response.text)
            self.assertIn('service environment', response.text)
            self.assertNotIn('fake-', response.text)
            response = self.save(revision='0' * 64)
            self.assertEqual(response.status_code, 503)
            self.assertIn('credential_tokenizer_unavailable', response.text)
            self.assertFalse((self.home / '.env.lock').exists())

    def test_save_readback_remove_preserves_other_credentials_and_never_publishes(self):
        before = '# untouched\nOTHER_KEY="fake-other-key"\nSINGLE="single line"\n'
        path = self.home / '.env'
        path.write_text(before)
        path.chmod(0o600)
        response = self.save()
        self.assertEqual(response.status_code, 200)
        status = self.status()
        self.assertTrue(status['saved'])
        self.assertFalse(status['request_configured'])
        self.assertFalse(status['verified'])
        self.assertEqual(status['activation'], 'process')
        self.assertEqual(os.environ['MODEL_API_KEY'], '')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(path.read_text().startswith(before))
        self.assertNotIn('fake-', json.dumps(status))
        response = self.client.post('/credentials/meta', json={'action': 'remove', 'revision': status['revision']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path.read_text(), before)
        self.assertFalse(self.status()['saved'])

    def test_unscoped_process_key_is_unchanged_by_edit(self):
        with patch.dict(os.environ, {'MODEL_API_KEY': 'fake-inherited-key'}):
            self.assertEqual(self.save().status_code, 200)
            status = self.status()
            self.assertTrue(status['request_configured'])
            self.assertEqual(status['activation'], 'process')
            self.assertTrue(status['process_differs_from_saved'])
            self.assertEqual(os.environ['MODEL_API_KEY'], 'fake-inherited-key')

    def test_stale_revision_refuses_concurrent_edits(self):
        revision = self.status()['revision']
        self.assertEqual(self.save(revision=revision).status_code, 200)
        self.assertEqual(self.save('fake-new-key', revision).status_code, 409)
        self.assertNotIn('fake-new-key', (self.home / '.env').read_text())

    def test_malformed_revisions_are_rejected_before_lock_or_mutation(self):
        path = self.home / '.env'
        original = b'OTHER=fake-unrelated-key\nMODEL_API_KEY=fake-original-key\n'
        path.write_bytes(original)
        path.chmod(0o600)
        before = path.stat()
        for revision in ('é' * 64, '０' * 64, 'g' * 64, 'A' * 64, '0' * 63, '0' * 65, '0' * 63 + '\n'):
            for action in ('save', 'remove'):
                with self.subTest(revision=repr(revision), action=action):
                    body = {'action': action, 'revision': revision}
                    if action == 'save':
                        body['key'] = 'fake-replacement-key'
                    response = self.client.post('/credentials/meta', json=body)
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn('fake-', response.text)
                    self.assertEqual(path.read_bytes(), original)
                    after = path.stat()
                    for field in ('st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                        self.assertEqual(getattr(after, field), getattr(before, field))
                    self.assertFalse((self.home / '.env.lock').exists())
                    self.assertFalse(list(self.home.glob('.env-stage-*')))

    def test_untrusted_requests_cannot_read_or_write_even_with_spoofed_headers(self):
        self.owner = False
        for method in ('get', 'post'):
            r = getattr(self.client, method)('/credentials/meta', headers={'X-Native-Owner': 'true', 'Origin': 'https://evil.test'})
            self.assertEqual(r.status_code, 403)
        self.assertFalse((self.home / '.env').exists())

    def test_native_profile_identity_cannot_target_another_home(self):
        self.principal['profile_id'] = str(self.home / 'another-profile')
        self.assertEqual(self.client.get('/credentials/meta').status_code, 403)
        self.assertEqual(self.save(revision='0' * 64).status_code, 403)
        self.assertFalse((self.home / '.env').exists())

    def test_current_profile_selector_from_sdk_is_accepted_but_arbitrary_queries_are_not(self):
        self.assertEqual(self.client.get('/credentials/meta?profile=current').status_code, 200)
        self.assertEqual(self.client.get('/credentials/meta?key=fake-secret').status_code, 400)

    def test_bad_payloads_are_bounded_and_never_echoed(self):
        for payload in ({'action': 'save', 'key': 'fake-secret\nOTHER=value'},
                        {'action': 'save', 'key': {'secret': 'fake-secret'}},
                        {'action': 'save', 'key': 'fake-secret', 'profile': 'other'},
                        {'action': 'reveal'}, ['fake-secret']):
            r = self.client.post('/credentials/meta', json=payload)
            self.assertEqual(r.status_code, 400)
            self.assertNotIn('fake-secret', r.text)
        r = self.client.post('/credentials/meta', content='fake-secret' * 2000, headers={'content-type': 'application/json'})
        self.assertEqual(r.status_code, 413)
        self.assertNotIn('fake-secret', r.text)
        r = self.client.post('/credentials/meta', content='{"key":"fake-secret"', headers={'content-type': 'application/json'})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('fake-secret', r.text)

    def test_symlinks_hardlinks_and_unsafe_permissions_fail_closed(self):
        target = self.home / 'elsewhere'
        target.write_text('OTHER=fake-secret\n')
        target.chmod(0o600)
        path = self.home / '.env'
        path.symlink_to(target)
        self.assertEqual(self.client.get('/credentials/meta').status_code, 503)
        path.unlink()
        os.link(target, path)
        self.assertEqual(self.client.get('/credentials/meta').status_code, 503)
        path.unlink()
        path.write_text('MODEL_API_KEY=fake-secret\n')
        path.chmod(0o644)
        self.assertEqual(self.client.get('/credentials/meta').status_code, 503)
        self.assertEqual(target.read_text(), 'OTHER=fake-secret\n')

    def test_duplicate_definitions_remove_all_without_touching_other_lines(self):
        path = self.home / '.env'
        path.write_text('export MODEL_API_KEY="fake-first"\nOTHER=fake-other\nMODEL_API_KEY=fake-last\n')
        path.chmod(0o600)
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(path.read_text().count('MODEL_API_KEY='), 1)
        self.assertIn('OTHER=fake-other\n', path.read_text())

    def test_disk_failure_is_sanitized_and_does_not_change_runtime(self):
        with patch.object(self.mod, '_write', side_effect=OSError('fake-secret')):
            r = self.save()
        self.assertEqual(r.status_code, 503)
        self.assertNotIn('fake-secret', r.text)
        self.assertEqual(os.environ['MODEL_API_KEY'], '')

    def test_parallel_writers_only_one_revision_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        revision = self.status()['revision']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda key: self.save(key, revision).status_code,
                ['fake-parallel-one', 'fake-parallel-two']))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual((self.home / '.env').read_text().count('MODEL_API_KEY='), 1)

    def test_authenticated_dashboard_or_hosted_user_is_not_native_owner(self):
        self.owner = False
        @self.app.middleware('http')
        async def authenticated_not_owner(request, next):
            request.state.hosted_http_authenticated = True
            request.state.token_authenticated = True
            request.state.session = object()
            return await next(request)
        self.assertEqual(self.client.get('/credentials/meta').status_code, 403)

    def test_malformed_file_and_lock_symlinks_never_get_rewritten(self):
        path = self.home / '.env'
        path.write_text('OTHER="unterminated\n')
        path.chmod(0o600)
        self.assertEqual(self.client.get('/credentials/meta').status_code, 503)
        self.assertEqual(path.read_text(), 'OTHER="unterminated\n')
        path.unlink()
        (self.home / '.env.lock').symlink_to(self.home / 'elsewhere')
        self.assertEqual(self.client.get('/credentials/meta').status_code, 503)
        self.assertFalse((self.home / 'elsewhere').exists())

    def test_host_helpers_and_restart_provider_routing_in_isolated_child(self):
        import importlib.util
        import subprocess
        import sys
        if importlib.util.find_spec('hermes_cli') is None:
            self.skipTest('Hermes source not installed; unit and UI filesystem tests still run')
        # stdin script, not argv, and a clean process environment/home. No
        # credential or user config from the invoking session is forwarded.
        script = '''
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
import credentials
app = FastAPI()
@app.middleware('http')
async def owner(request, next):
    request.state.native_http_principal = {'profile_id': os.environ['HERMES_HOME'], 'capabilities': frozenset({'http:owner'})}
    return await next(request)
app.include_router(credentials.make_router())
client = TestClient(app)
status = client.get('/credentials/meta')
assert status.status_code == 200
response = client.post('/credentials/meta', json={'action': 'save', 'key': 'fake-host-integration-key', 'revision': status.json()['revision']})
assert response.status_code == 200
assert response.json()['saved'] and not response.json()['request_configured']
assert os.environ.get('MODEL_API_KEY', '') == ''
path = Path(os.environ['HERMES_HOME']) / '.env'
assert path.stat().st_mode & 0o777 == 0o600
# Simulate a fresh backend module load after the gateway restarts. This is
# the existing .env -> Meta answer provider / Muse Voice resolver path.
os.environ['HERMES_LAUNCHD_LABEL'] = 'ai.hermes.gateway.test-only'
import meta_models
assert meta_models.resolve_model_api_key() == 'fake-host-integration-key'
import meta_asr
assert meta_asr.build_handshake(meta_models.resolve_model_api_key())['authorization']['accessToken'] == 'Bearer fake-host-integration-key'
# Exercise the actual selected-provider routing against a fake HTTP transport.
from unittest.mock import patch
import json
import io
import model_selection
def provider(request, timeout):
    assert request.get_header('Authorization') == 'Bearer fake-host-integration-key'
    assert request.full_url == 'https://api.meta.ai/v1/chat/completions'
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'Synthetic answer'}}]}).encode())
with patch.object(meta_models.urllib.request, 'urlopen', side_effect=provider):
    result = model_selection.selected_completion('meta', meta_models.DEFAULT_META_MODEL)([{'role': 'user', 'content': 'Synthetic question'}])
    assert result == 'Synthetic answer'
assert client.get('/credentials/meta').json()['verified'] is False
'''
        env = {'HOME': str(self.home), 'HERMES_HOME': str(self.home),
               'PATH': os.defpath, 'TMPDIR': str(self.home), 'PYTHONIOENCODING': 'utf-8'}
        # Preserve only interpreter module-search setup, never environment keys.
        if os.environ.get('PYTHONPATH'):
            env['PYTHONPATH'] = os.environ['PYTHONPATH']
        result = subprocess.run([sys.executable, '-'], input=script, text=True,
                                capture_output=True, env=env, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, 'Isolated host integration failed (output intentionally withheld)')
        self.assertNotIn('fake-host-integration-key', result.stdout + result.stderr)

    def test_private_home_under_sticky_temp_parent_is_safe_but_shared_home_is_not(self):
        parent = self.home / 'sticky'
        parent.mkdir(mode=0o1777)
        parent.chmod(0o1777)
        nested = parent / 'private-home'
        nested.mkdir(mode=0o700)
        self.principal['profile_id'] = str(nested)
        with patch.dict(os.environ, {'HERMES_HOME': str(nested)}):
            self.assertEqual(self.client.get('/credentials/meta').status_code, 200)
            nested.chmod(0o777)
            self.assertEqual(self.client.get('/credentials/meta').status_code, 503)

    def test_macos_acls_refused_before_secret_temp_open_and_metadata_preserved(self):
        import subprocess
        import sys
        if sys.platform != 'darwin':
            self.skipTest('native macOS ACL regression')
        path = self.home / '.env'
        original = b'OTHER=fake-unrelated-key\n'
        for target, rule in ((path, 'everyone allow read'),
                             (self.home, 'everyone allow read,file_inherit,directory_inherit')):
            with self.subTest(target=target.name):
                path.write_bytes(original)
                path.chmod(0o600)
                revision = self.status()['revision']
                subprocess.run(['/bin/chmod', '+a', rule, str(target)], check=True, capture_output=True)
                try:
                    before = subprocess.check_output(['/bin/ls', '-lde', str(target)])
                    with patch.object(self.mod._host.return_value, '_write_env_lines') as writer:
                        response = self.save(revision=revision)
                        self.assertEqual(response.status_code, 503)
                        writer.assert_not_called()
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(subprocess.check_output(['/bin/ls', '-lde', str(target)]), before)
                    self.assertNotIn('fake-', response.text)
                finally:
                    subprocess.run(['/bin/chmod', '-N', str(target)], check=True, capture_output=True)

    def test_existing_extended_metadata_is_refused_not_discarded(self):
        path = self.home / '.env'
        path.write_bytes(b'OTHER=fake-unrelated-key\n')
        path.chmod(0o600)
        revision = self.status()['revision']
        name = 'user.meeting_copilot_test'
        import sys
        import subprocess
        if sys.platform == 'darwin':
            subprocess.run(['/usr/bin/xattr', '-w', name, 'preserve-me', str(path)], check=True)
        else:
            os.setxattr(path, name, b'preserve-me')
        with patch.object(self.mod._host.return_value, '_write_env_lines') as writer:
            self.assertEqual(self.save(revision=revision).status_code, 503)
            writer.assert_not_called()
        if sys.platform == 'darwin':
            self.assertEqual(subprocess.check_output(['/usr/bin/xattr', '-p', name, str(path)]).strip(), b'preserve-me')
        else:
            self.assertEqual(os.getxattr(path, name), b'preserve-me')
        self.assertEqual(path.read_bytes(), b'OTHER=fake-unrelated-key\n')

    def test_generated_metadata_preserved_or_refused_before_publication(self):
        path = self.home / '.env'
        original = b'OTHER=fake-unrelated-key\n'
        path.write_bytes(original)
        path.chmod(0o600)
        before = path.stat()
        provenance = self.mod._provenance(path)
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.mod._provenance(path), provenance)
        self.assertEqual((path.stat().st_uid, path.stat().st_gid, path.stat().st_mode),
                         (before.st_uid, before.st_gid, before.st_mode))
        raw = path.read_bytes()
        revision = self.status()['revision']
        # Simulate a different native provenance value on the staged candidate.
        with patch.object(self.mod, '_provenance', side_effect=[b'original', b'different']):
            self.assertEqual(self.save(revision=revision).status_code, 503)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.mod._provenance(path), provenance)
        self.assertFalse(list(self.home.glob('.env-stage-*')))

    def test_ambiguous_multiline_refused_not_silently_repaired(self):
        import subprocess
        import sys
        path = self.home / '.env'
        unrelated = 'OTHER="first\nMODEL_API_KEY=fake-embedded-key\nlast"\n'
        path.write_text(unrelated + 'export MODEL_API_KEY="fake-real-assignment"\n')
        path.chmod(0o600)
        original = path.read_bytes()
        self.assertEqual(self.client.get('/credentials/meta').status_code, 503)
        response = self.client.post('/credentials/meta', json={'action': 'remove', 'revision': self.mod._revision(path, original)})
        self.assertEqual(response.status_code, 503)
        self.assertIn('Unsupported credential file', response.json()['detail'])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.home / '.env.lock').exists())
        env = {'HOME': str(self.home), 'HERMES_HOME': str(self.home), 'PATH': os.defpath,
               'TMPDIR': str(self.home), 'HERMES_LAUNCHD_LABEL': 'ai.hermes.gateway.test-only'}
        result = subprocess.run([sys.executable, '-c',
            'import meta_models; assert meta_models.resolve_model_api_key() == ""'],
            env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'Startup resurrected removed key (output withheld)')

    def test_status_is_uncacheable_and_never_claims_provider_verification(self):
        self.assertEqual(self.save().status_code, 200)
        r = self.client.get('/credentials/meta')
        self.assertEqual(r.headers['cache-control'], 'no-store')
        self.assertFalse(r.json()['verified'])


def ui_bridge():
    """A real isolated filesystem + ASGI backend for the desktop event tests.

    StdIO is private to the test child; only status/booleans go out. Request
    bodies are never logged. This does NOT start an unguarded HTTP listener.
    """
    import sys
    case = CredentialTests()
    case.setUp()
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command['path'] == '/test/saved-effect':
                path = case.home / '.env'
                value = path.read_text() if path.exists() else ''
                result = {'saved': 'MODEL_API_KEY=fake-ui-key-only\n' in value,
                          'mode_private': not path.exists() or path.stat().st_mode & 0o777 == 0o600,
                          'runtime_unchanged': os.environ['MODEL_API_KEY'] == ''}
                status = 200
            else:
                opts = command.get('opts') or {}
                response = case.client.request(opts.get('method', 'GET'), command['path'],
                    **({'json': opts['body']} if 'body' in opts else {}))
                result, status = response.json(), response.status_code
            print(json.dumps({'status': status, 'body': result}), flush=True)
    finally:
        case.doCleanups()


if __name__ == '__main__':
    import sys
    if '--ui-bridge' in sys.argv:
        ui_bridge()
    else:
        unittest.main()
