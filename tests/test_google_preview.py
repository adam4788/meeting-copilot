"""Google preview admission fixtures: never use actual keys, ledger or network."""
import base64
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import byok
import capture_preflight
import google_fixture
import google_preview
import macos_audio
import server


def wav():
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(16000)
        writer.writeframes(b'\0\0' * 16000)
    return base64.b64encode(out.getvalue()).decode()


class BudgetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'new' / 'ledger.json'
        for replacement in (patch.object(google_preview, '_path', return_value=self.path), patch.object(byok, '_scope_available', return_value=True)):
            replacement.start(); self.addCleanup(replacement.stop)

    def test_check_creates_nothing_and_durable_reservations_exhaust(self):
        self.assertTrue(google_preview.budget_status('both')['available'])
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(google_preview.reserve('both')['reserved_usd'], 2)
        self.assertEqual(google_preview.reserve('both')['reserved_usd'], 4)
        self.assertFalse(google_preview.budget_status()['available'])
        with self.assertRaises(RuntimeError): google_preview.reserve()
        self.assertEqual(json.loads(self.path.read_text())['reserved_streams'], 4)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_bad_or_empty_existing_ledger_never_resets(self):
        self.path.parent.mkdir()
        for payload in ('', '{}', '{"version":1,"reserved_streams":true}', '{"version":1,"reserved_streams":5}'):
            self.path.write_text(payload)
            self.assertFalse(google_preview.budget_status()['available'])
            with self.assertRaises(RuntimeError): google_preview.reserve()
            self.assertEqual(self.path.read_text(), payload)

    def test_scope_symlink_and_sync_failure_fail_closed(self):
        with patch.object(byok, '_scope_available', return_value=False):
            self.assertFalse(google_preview.budget_status()['available'])
            with self.assertRaises(RuntimeError): google_preview.reserve()
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir()
        target = self.path.parent / 'target'; target.write_text('unchanged')
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeError): google_preview.reserve()
        self.assertEqual(target.read_text(), 'unchanged')
        self.path.unlink()
        with patch.object(google_preview.os, 'fsync', side_effect=OSError):
            with self.assertRaises(RuntimeError): google_preview.reserve()
        self.assertEqual(json.loads(self.path.read_text())['reserved_streams'], 1)

    def test_concurrent_admission_never_exceeds_four(self):
        from concurrent.futures import ThreadPoolExecutor
        def attempt(_):
            try: google_preview.reserve(); return True
            except RuntimeError: return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            accepted = sum(pool.map(attempt, range(20)))
        self.assertLessEqual(accepted, 4)
        self.assertEqual(json.loads(self.path.read_text())['reserved_streams'], accepted)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.process = Mock(returncode=0)
        self.process.poll.return_value = None
        self.process.communicate.return_value = (b'{"type":"segment","speaker":"you","text":"Fictional Atlas pilot."}\n{"type":"google_usage","usage":{"audio_seconds":1,"input_tokens":32,"secret":"excluded"}}\n', None)
        self.patches = [patch.object(google_preview, '_require_scope'), patch.object(google_preview, 'scoped_key', return_value='synthetic-key'),
            patch.object(google_preview, 'reserve', return_value={'reservation_usd': 1}),
            patch.object(macos_audio, 'worker_python', return_value='/synthetic/python'),
            patch.object(macos_audio, 'terminate_group'), patch.object(google_fixture.subprocess, 'Popen', return_value=self.process)]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches: self.addCleanup(p.stop)

    def test_same_worker_private_pipe_no_capture_and_sanitized_result(self):
        result = google_fixture.run(wav())
        command = self.mocks[-1].call_args.args[0]
        self.assertIn('--google-fixture-stdin', command)
        self.assertNotIn('synthetic-key', str(command))
        self.assertNotIn('GEMINI_API_KEY', self.mocks[-1].call_args.kwargs['env'])
        frame = self.process.communicate.call_args.kwargs['input'].splitlines()[0]
        self.assertEqual(json.loads(frame)['type'], 'scoped_google_key')
        self.assertFalse(result['capture_started']); self.assertFalse(result['tail_confirmed'])
        self.assertEqual(result['usage'], {'audio_seconds': 1., 'input_tokens': 32})
        self.assertEqual(result['segments'][0]['text'], 'Fictional Atlas pilot.')
        self.mocks[2].assert_called_once_with('microphone')
        self.mocks[4].assert_called_once_with(self.process, timeout=3)

    def test_invalid_audio_scope_and_missing_key_do_not_reserve_or_spawn(self):
        for encoded in ('bad', base64.b64encode(b'not wav').decode()):
            with self.assertRaises(ValueError): google_fixture.run(encoded)
        self.mocks[0].side_effect = RuntimeError('no scope')
        with self.assertRaises(RuntimeError): google_fixture.run(wav())
        self.mocks[0].side_effect = None; self.mocks[1].return_value = None
        with self.assertRaises(RuntimeError): google_fixture.run(wav())
        self.mocks[2].assert_not_called(); self.mocks[-1].assert_not_called()

    def test_timeout_and_invalid_result_always_retire_without_refund(self):
        for output in (b'not-json', b'x' * (google_fixture.MAX_RESULT_BYTES + 1)):
            self.process.communicate.return_value = (output, None)
            with self.assertRaises(RuntimeError): google_fixture.run(wav())
        self.process.communicate.side_effect = subprocess.TimeoutExpired('synthetic', 85)
        with self.assertRaises(RuntimeError): google_fixture.run(wav())
        self.assertEqual(self.mocks[4].call_count, 3)
        self.assertEqual(self.mocks[2].call_count, 3)

    def test_completed_or_previously_stopped_fixture_is_not_signalled_again(self):
        operation = google_fixture.FixtureOperation()
        operation.process = self.process
        self.process.poll.return_value = 0
        operation.stop(); operation.stop()
        self.mocks[4].assert_not_called()
        self.process.poll.return_value = None
        self.mocks[4].side_effect = lambda *args, **kwargs: setattr(self.process.poll, 'return_value', 0)
        operation.stop(); operation.stop()
        self.mocks[4].assert_called_once()


class GoogleServerTests(unittest.TestCase):
    def setUp(self):
        self.state = server.MeetingState()
        self.state.watchdog = lambda *args: None
        app = FastAPI(); app.include_router(server.make_router(self.state))
        self.client = TestClient(app); self.addCleanup(self.client.close)

    def test_explicit_consent_lifecycle_and_idle_required(self):
        body = {'audio_base64': wav()}
        operation = Mock(is_running=False)
        operation.wait.return_value = {'capture_started': False}
        with patch.object(google_fixture, 'FixtureOperation', return_value=operation):
            run = operation.start
            self.assertEqual(self.client.post('/transcription/google/test', json=body).status_code, 400)
            self.assertEqual(self.client.post('/transcription/google/test', json={**body, 'consent': 'yes'}).status_code, 422)
            self.state.listening = True
            self.assertEqual(self.client.post('/transcription/google/test', json={**body, 'consent': True}).status_code, 409)
            self.state.listening = False; self.state.overlay_revision = 2
            self.assertEqual(self.client.post('/transcription/google/test', json={**body, 'consent': True}).status_code, 409)
            run.assert_not_called()
            result = self.client.post('/transcription/google/test', json={**body, 'consent': True, 'revision': 2, 'epoch': self.state.overlay_epoch})
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json()['capture_started'])
            self.assertFalse(self.state.listening); self.assertEqual(list(self.state.segments), [])

    def test_secret_capability_import_and_sanitized_errors(self):
        with patch.object(google_preview, 'scoped_key', return_value='synthetic-key'):
            self.assertTrue(server._google_key_present())
            self.assertTrue(self.state.status()['capabilities']['google_asr'])
        with patch.object(google_fixture.FixtureOperation, 'start', side_effect=RuntimeError('PRIVATE-KEY')):
            result = self.client.post('/transcription/google/test', json={'consent': True, 'audio_base64': wav()})
            self.assertEqual(result.status_code, 503); self.assertNotIn('PRIVATE', result.text)

    def test_google_usage_is_allowlisted_and_latched(self):
        self.state.audio_status({'state': 'stopped', 'google_usage': {'microphone': {'audio_seconds': 2, 'output_tokens': 3, 'secret': 'no'}}})
        self.state.audio_status({'state': 'stopped'})
        self.assertEqual(self.state.capture_info['google_usage'], {'microphone': {'audio_seconds': 2., 'output_tokens': 3}})

    def test_pause_retires_inflight_test_without_waiting_for_http_result(self):
        import threading
        entered, stopped = threading.Event(), threading.Event()
        operation = Mock(is_running=False)
        operation.status.return_value = {'running': False, 'stop_confirmed': True}
        operation.stop.side_effect = stopped.set
        def wait():
            entered.set()
            if not stopped.wait(2): raise AssertionError('Pause could not reach worker')
            return {'capture_started': False, 'error': 'cancelled'}
        operation.wait.side_effect = wait
        with patch.object(google_fixture, 'FixtureOperation', return_value=operation):
            results = []
            thread = threading.Thread(target=lambda: results.append(self.client.post('/transcription/google/test', json={'consent': True, 'audio_base64': wav()})))
            thread.start(); self.assertTrue(entered.wait(1))
            status = self.state.status()
            self.assertTrue(status['google_fixture_active']); self.assertTrue(status['stopping'])
            with self.assertRaises(Exception): self.state.start(True, 'macos')
            paused = self.state.pause()
            self.assertTrue(paused['stop_confirmed']); self.assertTrue(stopped.is_set())
            thread.join(2); self.assertFalse(thread.is_alive())
            self.assertEqual(results[0].status_code, 200)
            self.assertIsNone(self.state.fixture_test)

    def test_new_pane_claim_and_credential_change_retire_fixture(self):
        for action in ('claim', 'credentials'):
            with self.subTest(action=action):
                state = server.MeetingState()
                operation = Mock(is_running=False)
                operation.status.return_value = {'stop_confirmed': True}
                state.fixture_test = operation
                if action == 'claim':
                    state.overlay_claim(state.overlay_revision)
                else:
                    with state.credential_change_boundary(): pass
                operation.stop.assert_called_once()
                self.assertIsNone(state.fixture_test)

    def test_google_retirement_window_covers_six_second_final(self):
        for provider, expected in (('google', True), ('meta', False), ('local', False)):
            with self.subTest(provider=provider):
                audio = Mock(is_running=False)
                state = server.MeetingState(macos_factory=lambda **kwargs: audio)
                state.watchdog = lambda *args: None
                ready = {'version': 1, 'ready': True, 'checks': [{'status': 'ready'}]}
                with patch.object(state, 'capture_preflight', return_value=ready):
                    state.start(True, 'macos', transcription_provider=provider)
                callback = audio.start.call_args.kwargs['on_segment']
                now = [100.]
                def stop():
                    now[0] = 106.
                    callback({'text': 'Fictional final', 'speaker': 'you', 'timestamp': 1})
                audio.stop.side_effect = stop
                with patch.object(server.time, 'monotonic', side_effect=lambda: now[0]): state.pause()
                self.assertEqual(any(s['text'] == 'Fictional final' for s in state.segments), expected)


class GoogleReadinessTests(unittest.TestCase):
    def test_readiness_is_local_and_key_budget_specific(self):
        mac, native = Mock(), Mock()
        mac.worker_env.return_value = {}
        helper = {'version': 1, 'source': 'both', 'capture_started': False,
                  'microphone_available': True, 'display_available': True,
                  'microphone': 'authorized', 'system': 'authorized', 'microphone_usage_description': True}
        def module(name):
            return {'macos_audio': mac, 'native_runtime': native, 'google_preview': google_preview}[name]
        with patch.object(capture_preflight.sys, 'platform', 'darwin'), \
             patch.object(capture_preflight, '_module', side_effect=module), \
             patch.object(capture_preflight.os, 'access', return_value=True), \
             patch.object(capture_preflight, '_json_command', side_effect=lambda *args, **kwargs: helper if '--preflight' in args[0] else {'version': 1, 'dependencies': True}), \
             patch.object(google_preview, 'scoped_key', return_value='synthetic-key') as key, \
             patch.object(google_preview, 'budget_status', return_value={'available': True, 'detail': 'Synthetic allowance'}) as budget, \
             patch.object(google_preview, 'reserve', side_effect=AssertionError('preflight must not reserve')):
            def check(mode='ENDPOINTING'):
                return capture_preflight.check('macos', None, 'base.en', 'both', 'google', mode)
            self.assertTrue(check()['ready'])
            self.assertNotIn('model_cache', [r['id'] for r in check()['checks']])
            budget.assert_called_with('both')
            self.assertFalse(check('DIARIZATION')['ready'])
            key.return_value = ''
            self.assertFalse(check()['ready'])
            key.return_value = 'synthetic-key'; budget.return_value['available'] = False
            self.assertFalse(check()['ready'])

    def test_capture_admission_reserves_both_before_starting_worker(self):
        events = []
        capture = macos_audio.MacOSAudioCapture(source='both', provider='google')
        with patch.object(google_preview, 'scoped_key', return_value='synthetic-key'), \
             patch.object(google_preview, 'reserve', side_effect=lambda source: events.append(('reserved', source))), \
             patch.object(macos_audio, '_native_runtime', return_value=Mock()), \
             patch.object(macos_audio.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = lambda: events.append(('started',))
            capture.start(lambda value: None)
            self.assertEqual(events, [('reserved', 'both'), ('started',)])
            args = thread.call_args.kwargs['args']
            self.assertNotIn('MEETING_COPILOT_GEMINI_API_KEY', args[0])
            self.assertEqual(json.loads(args[1])['type'], 'scoped_google_key')

    def test_google_usage_rejects_nonfinite_and_untrusted_text(self):
        self.assertEqual(macos_audio.clean_google_usage({'microphone': {'audio_seconds': float('nan'), 'input_tokens': True, 'text': 'discard'}, 'other': {'audio_seconds': 1}}), {})
