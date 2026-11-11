"""Backend admission and discovery use synthetic probes and HTTP responses only."""
import json
import time
import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import byok
import capture_preflight
import server


def report(ready=True):
    return {'version': 1, 'ready': ready, 'checked_at': time.time(), 'valid_for_seconds': 15,
            'checks': [{'id': 'runtime', 'label': 'Synthetic runtime',
                        'status': 'ready' if ready else 'blocked', 'detail': 'Synthetic check.'}]}


class PreflightServerTests(unittest.TestCase):
    def setUp(self):
        self.audio = Mock()
        self.factory = Mock(return_value=self.audio)
        self.state = server.MeetingState(macos_factory=self.factory)
        self.state.watchdog = lambda *args: None
        self.app = FastAPI()
        self.app.include_router(server.make_router(self.state))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.state.pause)

    def test_read_only_check_requires_consent_but_never_starts_or_reads_answer_key(self):
        with patch.object(capture_preflight, 'check', return_value=report()) as probe, \
             patch.object(capture_preflight, 'request_permissions') as permissions, \
             patch.object(byok, 'secret', side_effect=AssertionError('answer key read')):
            body = {'backend': 'macos', 'audio_source': 'system'}
            seen = self.state.pane_seen
            result = self.client.post('/preflight', json=body).json()
            self.assertFalse(result['ready'])
            self.assertEqual(result['checks'][0]['id'], 'consent')
            self.assertTrue(self.client.post('/preflight', json={**body, 'consent': True}).json()['ready'])
            probe.assert_called_with('macos', None, 'base.en', 'system', 'local', 'ENDPOINTING', adapter=None)
            self.factory.assert_not_called()
            permissions.assert_not_called()
            self.assertEqual(self.state.pane_seen, seen)

    def test_direct_start_rechecks_and_never_trusts_previous_success(self):
        with patch.object(capture_preflight, 'check', side_effect=[report(), report(False)]) as probe:
            self.assertTrue(self.client.post('/preflight', json={'backend': 'macos', 'consent': True}).json()['ready'])
            result = self.client.post('/start', json={'backend': 'macos', 'consent': True})
            self.assertEqual(result.status_code, 409)
            self.assertEqual(probe.call_count, 2)
            self.factory.assert_not_called()
            self.assertFalse(self.state.listening)
            self.assertEqual(self.state.generation, 0)

    def test_local_capture_can_start_without_answer_key(self):
        with patch.object(capture_preflight, 'check', return_value=report()), \
             patch.object(byok, 'secret', side_effect=AssertionError('answer key read')):
            response = self.client.post('/start', json={'backend': 'macos', 'consent': True})
            self.assertEqual(response.status_code, 200)
            self.audio.start.assert_called_once()

    def test_malformed_or_failed_checks_fail_closed_without_exception_text(self):
        for value in [None, {}, {'version': 1, 'checks': [], 'ready': True},
                      {'version': 1, 'checks': [None], 'ready': True}, report(False)]:
            with self.subTest(value=value), patch.object(capture_preflight, 'check', return_value=value):
                self.assertFalse(self.client.post('/preflight', json={'backend': 'macos', 'consent': True}).json()['ready'])
        with patch.object(capture_preflight, 'check', side_effect=RuntimeError('PRIVATE-KEY-PATH')):
            response = self.client.post('/preflight', json={'backend': 'macos', 'consent': True})
            self.assertFalse(response.json()['ready'])
            self.assertNotIn('PRIVATE', response.text)
        self.factory.assert_not_called()

    def test_strict_consent_and_windows_route_mapping(self):
        with patch.object(capture_preflight, 'check', return_value=report(False)) as probe:
            self.assertEqual(self.client.post('/preflight', json={'backend': 'native', 'consent': 'true'}).status_code, 422)
            response = self.client.post('/preflight', json={'backend': 'native', 'consent': True})
            self.assertFalse(response.json()['ready'])
            self.assertEqual(probe.call_args.args[0], 'native')

    def test_permissions_are_explicit_separate_and_capture_free(self):
        with patch.object(capture_preflight, 'request_permissions', return_value={
                'version': 1, 'status': 'completed', 'capture_started': False}) as request:
            self.assertEqual(self.client.post('/permissions/request', json={'audio_source': 'microphone'}).status_code, 400)
            self.assertEqual(self.client.post('/permissions/request', json={'audio_source': 'invalid', 'consent': True}).status_code, 422)
            request.assert_not_called()
            result = self.client.post('/permissions/request', json={'audio_source': 'system', 'consent': True})
            self.assertEqual(result.status_code, 200)
            request.assert_called_once_with('system')
            self.factory.assert_not_called()
            self.state.listening = True
            self.assertEqual(self.client.post('/permissions/request', json={'audio_source': 'system', 'consent': True}).status_code, 409)
            self.assertEqual(request.call_count, 1)
            self.state.listening = False

    def test_discovery_needs_explicit_consent_and_valid_provider(self):
        with patch.object(byok, 'discover_models', return_value={'models': []}) as discover:
            self.client.get('/models')
            self.assertEqual(self.client.post('/models/discover', json={'provider': 'openai'}).status_code, 400)
            self.assertEqual(self.client.post('/models/discover', json={'provider': 'unknown', 'consent': True}).status_code, 422)
            self.assertEqual(self.client.post('/models/discover', json={'provider': 'openai', 'consent': 'true'}).status_code, 422)
            discover.assert_not_called()
            self.assertEqual(self.client.post('/models/discover', json={'provider': 'openai', 'consent': True}).status_code, 200)
            discover.assert_called_once_with('openai')


class ModelDiscoveryTests(unittest.TestCase):
    def wire(self, payload, provider='openai', key='synthetic-key'):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(payload).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(byok, '_scope_available', return_value=True), \
             patch.object(byok, 'secret', return_value=key), \
             patch.object(byok.urllib.request, 'build_opener', return_value=opener) as build:
            result = byok.discover_models(provider)
        self.assertIsInstance(build.call_args.args[0], byok._NoRedirect)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(response.read.call_args.args, (byok._MODEL_BYTES + 1,))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, byok._MODEL_ENDPOINTS[provider])
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + key)
        return result

    def test_account_list_is_bounded_sanitized_and_never_recommends_automatically(self):
        result = self.wire({'data': [{'id': value} for value in [
            'gpt-synthetic', 'gpt-synthetic', 'o3', 'text-embedding-3-small', 'gpt-audio',
            'gpt-sk-secret', 'https://malicious.example', 'gpt-' + 'x' * 201]] + [None]})
        self.assertEqual(result['models'], ['gpt-synthetic', 'o3'])
        self.assertEqual(result['source'], 'account-list')
        self.assertIn('unverified', result['status'])

    def test_openrouter_options_are_labeled_catalog_not_entitlements(self):
        result = self.wire({'data': [{'id': 'vendor/text', 'architecture': {'output_modalities': ['text']}},
                                     {'id': 'vendor/image', 'architecture': {'output_modalities': ['image']}}]}, 'openrouter')
        self.assertEqual(result['models'], ['vendor/text'])
        self.assertEqual(result['source'], 'provider-catalog')
        self.assertIn('account access', result['status'])
        self.assertIn('unverified', result['status'])

    def test_absent_scope_or_key_never_contacts_provider(self):
        for scoped, key in [(False, 'synthetic-key'), (True, '')]:
            with patch.object(byok, '_scope_available', return_value=scoped), \
                 patch.object(byok, 'secret', return_value=key), \
                 patch.object(byok.urllib.request, 'build_opener') as network:
                with self.assertRaises(ValueError):
                    byok.discover_models('openai')
                network.assert_not_called()

    def test_network_errors_never_echo_provider_payload_or_use_fallback(self):
        with patch.object(byok, '_scope_available', return_value=True), \
             patch.object(byok, 'secret', return_value='synthetic-secret'), \
             patch.object(byok.urllib.request, 'build_opener') as network:
            network.return_value.open.side_effect = RuntimeError('synthetic-secret /private/path')
            with self.assertRaises(RuntimeError) as error:
                byok.discover_models('openai')
            self.assertNotIn('synthetic-secret', str(error.exception))
            self.assertNotIn('/private', str(error.exception))
            self.assertEqual(network.return_value.open.call_count, 1)
        for payload in [[], {}, {'data': 'bad'}, {'data': [{'id': 'x'}] * 10001}]:
            with self.assertRaises(RuntimeError):
                self.wire(payload)


class LiveModelCacheTests(unittest.TestCase):
    def test_live_model_loading_never_fetches_and_rejects_missing_tokenizer(self):
        import tempfile
        import types
        from pathlib import Path
        import audio_worker
        for vocabulary in ('vocabulary.txt', 'vocabulary.json'):
            with self.subTest(vocabulary=vocabulary), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for filename in ('model.bin', 'config.json', vocabulary):
                    (root / filename).write_text('synthetic')
                hub = types.SimpleNamespace(snapshot_download=Mock(return_value=str(root)))
                factory = Mock()
                with patch.dict('sys.modules', {'huggingface_hub': hub}):
                    with self.assertRaises(FileNotFoundError):
                        audio_worker.load_model('base.en', factory, offline=True)
                    factory.assert_not_called()
                    (root / 'tokenizer.json').write_text('synthetic')
                    audio_worker.load_model('base.en', factory, offline=True)
                    factory.assert_called_once_with(str(root), device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)
                    (root / vocabulary).unlink()
                    with self.assertRaises(ValueError):
                        audio_worker.load_model('base.en', factory, offline=True)
                    self.assertEqual(factory.call_count, 1)
                hub.snapshot_download.assert_called_with('Systran/faster-whisper-base.en', local_files_only=True, allow_patterns=capture_preflight.MODEL_FILES)

    def test_discovery_wall_clock_timeout_never_spawns_another_worker(self):
        import threading
        gate = threading.Event()
        released = threading.Event()
        opener = Mock()
        def slow(*args, **kwargs):
            try:
                gate.wait(2)
                raise RuntimeError('synthetic private failure')
            finally:
                released.set()
        opener.open.side_effect = slow
        with patch.object(byok, '_scope_available', return_value=True), \
             patch.object(byok, 'secret', return_value='synthetic-key'), \
             patch.object(byok, '_MODEL_TIMEOUT', .01), \
             patch.object(byok.urllib.request, 'build_opener', return_value=opener):
            try:
                with self.assertRaisesRegex(RuntimeError, 'timed out'):
                    byok.discover_models('openai')
                with self.assertRaisesRegex(RuntimeError, 'still in progress'):
                    byok.discover_models('openrouter')
                self.assertEqual(opener.open.call_count, 1)
            finally:
                gate.set()
                self.assertTrue(released.wait(1))
                # Wait for the worker's finally to release the concurrency cap.
                self.assertTrue(byok._DISCOVERY_LOCK.acquire(timeout=1))
                byok._DISCOVERY_LOCK.release()
