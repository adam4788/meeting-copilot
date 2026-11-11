"""Synthetic preflight tests: no hardware, provider, permissions or downloads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import capture_preflight as preflight


class PreflightTests(unittest.TestCase):
    def check(self, **kwargs):
        args = dict(backend='macos', recording_id=None, transcription_model='base.en',
                    audio_source='microphone', transcription_provider='local', asr_mode='ENDPOINTING')
        args.update(kwargs)
        return preflight.check(**args)

    def test_invalid_settings_do_not_probe(self):
        with patch.object(preflight, '_json_command') as run:
            self.assertFalse(self.check(audio_source='bogus')['ready'])
            run.assert_not_called()

    def test_wrong_platform_does_not_probe(self):
        with patch.object(preflight.sys, 'platform', 'linux'), patch.object(preflight, '_json_command') as run:
            self.assertFalse(self.check()['ready'])
            run.assert_not_called()

    def test_meetily_active_only(self):
        adapter = Mock()
        adapter.recordings.return_value = [{'id': 'safe', 'status': 'recording'}, {'id': 'old', 'status': 'completed'}]
        self.assertTrue(self.check(backend='meetily', recording_id='safe', adapter=adapter)['ready'])
        self.assertFalse(self.check(backend='meetily', recording_id='old', adapter=adapter)['ready'])
        adapter.attach.assert_not_called()

    def mac_check(self, permission='authorized', source='microphone', runtime=None, microphone=True, display=True):
        mac, native = Mock(), Mock()
        mac.worker_env.return_value = {}
        mac.worker_python.return_value = '/prepared/python'
        mac.capture_helper.return_value.is_file.return_value = True
        mac.capture_helper.return_value.__str__ = lambda _: '/prepared/helper'
        helper = dict(version=1, source=source, capture_started=False, microphone=permission,
                      microphone_usage_description=True, system=permission, microphone_available=microphone, display_available=display)
        with patch.object(preflight.sys, 'platform', 'darwin'), \
             patch.object(preflight, '_module', side_effect=lambda name: mac if name == 'macos_audio' else native), \
             patch.object(preflight.os, 'access', return_value=True), \
             patch.object(preflight, '_json_command', side_effect=[helper, runtime or {'version': 1, 'dependencies': True, 'model': True}]) as run:
            result = self.check(audio_source=source)
            self.assertEqual(run.call_args_list[0].args[0][1:], ['--preflight', '--source', source])
            self.assertEqual(run.call_args_list[1].args[1]['HF_HUB_OFFLINE'], '1')
            return result

    def test_macos_ready_for_selected_source(self):
        result = self.mac_check()
        self.assertTrue(result['ready'])
        self.assertEqual(result['valid_for_seconds'], 15)
        self.assertNotIn('system_permission', [r['id'] for r in result['checks']])
        self.assertTrue(self.mac_check(source='system')['ready'])

    def test_missing_or_unknown_devices_block_only_selected_sources(self):
        for value in (False, None, 'true', 1):
            mic = self.mac_check(microphone=value)
            self.assertFalse(mic['ready'])
            row = next(r for r in mic['checks'] if r['id'] == 'microphone_device')
            self.assertEqual(row['status'], 'blocked' if value is False else 'unknown')
            self.assertFalse(self.mac_check(source='system', display=value)['ready'])
        self.assertTrue(self.mac_check(source='microphone', display=False)['ready'])
        self.assertTrue(self.mac_check(source='system', microphone=False)['ready'])
        self.assertFalse(self.mac_check(source='both', display=False)['ready'])
        self.assertFalse(self.mac_check(source='both', microphone=False)['ready'])

    def test_denied_and_unknown_permission_block(self):
        for state in ('denied', 'not_determined', 'unknown', None):
            self.assertFalse(self.mac_check(permission=state)['ready'])

    def test_model_and_dependency_failure_block(self):
        for dep, model in ((False, True), (True, False), (None, None)):
            self.assertFalse(self.mac_check(runtime={'version': 1, 'dependencies': dep, 'model': model})['ready'])

    def test_windows_unknown_blocks(self):
        mac = Mock()
        mac.worker_env.return_value = {}
        with patch.object(preflight.sys, 'platform', 'win32'), patch.object(preflight, '_module', return_value=mac), \
             patch.object(preflight, '_json_command', return_value={'version': 1, 'dependencies': True, 'model': True}):
            result = self.check(backend='native')
        self.assertFalse(result['ready'])
        self.assertIn('unknown', [r['status'] for r in result['checks']])

    def test_probe_rejects_protocol_and_timeout(self):
        mac = Mock()
        mac.worker_env.return_value = {}
        with patch.object(preflight.sys, 'platform', 'darwin'), patch.object(preflight, '_module', return_value=mac), \
             patch.object(preflight.os, 'access', return_value=True), patch.object(preflight, '_json_command', side_effect=TimeoutError):
            self.assertFalse(self.check()['ready'])
        with patch.object(preflight.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'{}')):
            with self.assertRaises(ValueError):
                preflight._json_command(['synthetic'], {})

    def test_offline_model_requires_tokenizer_before_constructor(self):
        with tempfile.TemporaryDirectory() as tmp:
            whisper, hub = Mock(), Mock()
            hub.snapshot_download.return_value = tmp
            with patch.object(preflight.importlib, 'import_module', side_effect=lambda name: hub if name == 'huggingface_hub' else whisper):
                result = preflight._runtime_probe('base.en', 'local')
            self.assertFalse(result['model'])
            whisper.WhisperModel.assert_not_called()
            hub.snapshot_download.assert_called_once_with('Systran/faster-whisper-base.en', local_files_only=True, allow_patterns=preflight.MODEL_FILES)

    def test_offline_probe_accepts_text_or_json_vocabulary_and_validates_model(self):
        for vocabulary in ('vocabulary.txt', 'vocabulary.json'):
            with self.subTest(vocabulary=vocabulary), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                whisper, hub = Mock(), Mock()
                hub.snapshot_download.return_value = tmp
                for name in ('model.bin', 'config.json', 'tokenizer.json', vocabulary):
                    (root / name).write_text('synthetic')
                with patch.object(preflight.importlib, 'import_module', side_effect=lambda name: hub if name == 'huggingface_hub' else whisper):
                    self.assertTrue(preflight._runtime_probe('base.en', 'local')['model'])
                    whisper.WhisperModel.assert_called_once_with(tmp, device='cpu', compute_type='int8', cpu_threads=1, local_files_only=True)
                    whisper.WhisperModel.side_effect = ValueError('corrupt synthetic model')
                    self.assertFalse(preflight._runtime_probe('base.en', 'local')['model'])
                    whisper.WhisperModel.reset_mock()
                    for name in ('model.bin', 'config.json', 'tokenizer.json', vocabulary):
                        (root / name).write_text('')
                        self.assertFalse(preflight._runtime_probe('base.en', 'local')['model'])
                        whisper.WhisperModel.assert_not_called()
                        (root / name).unlink()
                        self.assertFalse(preflight._runtime_probe('base.en', 'local')['model'])
                        whisper.WhisperModel.assert_not_called()
                        (root / name).write_text('synthetic')
                hub.snapshot_download.assert_called_with('Systran/faster-whisper-base.en', local_files_only=True, allow_patterns=preflight.MODEL_FILES)

    def test_model_preparation_uses_prepared_interpreter_and_sanitized_env(self):
        mac, native = Mock(), Mock()
        mac.worker_python.return_value = '/prepared/python'
        mac.worker_env.return_value = {'HOME': '/synthetic/home'}
        with patch.object(preflight.sys, 'platform', 'darwin'), \
             patch.object(preflight, '_module', side_effect=lambda name: mac if name == 'macos_audio' else native), \
             patch.object(preflight, '_json_command', return_value={'version': 1, 'prepared': True}) as run:
            self.assertTrue(preflight.prepare_model('base.en')['prepared'])
            command, env = run.call_args.args
            self.assertEqual(command[0], '/prepared/python')
            self.assertEqual(command[-2:], ['--prepare-model-worker', 'base.en'])
            self.assertEqual(env['HF_HUB_DISABLE_IMPLICIT_TOKEN'], '1')
            self.assertNotIn('HF_HUB_OFFLINE', env)
            mac.worker_env.assert_called_once_with('local')
            mac.worker_python.assert_called_once()
            run.side_effect = RuntimeError('secret must never be reflected')
            failed = preflight.prepare_model('base.en')
            self.assertFalse(failed['prepared'])
            self.assertNotIn('secret', json.dumps(failed))

    def test_invalid_model_preparation_does_not_run(self):
        with patch.object(preflight, '_json_command') as run:
            self.assertFalse(preflight.prepare_model('../untrusted')['prepared'])
            with patch.object(preflight.sys, 'platform', 'win32'):
                self.assertFalse(preflight.prepare_model('base.en')['prepared'])
            run.assert_not_called()

    def test_model_preparation_worker_only_constructs_model(self):
        whisper = Mock()
        with patch.object(preflight.importlib, 'import_module', return_value=whisper) as imports, \
             patch.object(preflight, '_runtime_probe', return_value={'model': True}) as offline, \
             patch.dict(preflight.os.environ, {}, clear=True):
            self.assertTrue(preflight._prepare_model_worker('tiny.en')['prepared'])
            self.assertEqual(preflight.os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'], '1')
            imports.assert_called_once_with('faster_whisper')
            whisper.WhisperModel.assert_called_once_with('tiny.en', device='cpu', compute_type='int8', cpu_threads=1)
            whisper.WhisperModel.return_value.transcribe.assert_not_called()
            offline.assert_called_once_with('tiny.en', 'local')
            offline.return_value = {'model': False}
            self.assertFalse(preflight._prepare_model_worker('tiny.en')['prepared'])

    def test_missing_model_has_exact_recovery_command(self):
        result = self.mac_check(runtime={'version': 1, 'dependencies': True, 'model': False})
        detail = next(r['detail'] for r in result['checks'] if r['id'] == 'model_cache')
        self.assertIn('python -B capture_preflight.py --prepare-model base.en', detail)

    def test_permission_request_is_explicit_and_source_scoped(self):
        mac = Mock()
        mac.worker_env.return_value = {}
        mac.capture_helper.return_value.is_file.return_value = True
        mac.capture_helper.return_value.__str__ = lambda _: '/prepared/helper'
        result = {'version': 1, 'source': 'microphone', 'capture_started': False,
                  'permission_request_completed': True}
        with patch.object(preflight.sys, 'platform', 'darwin'), patch.object(preflight, '_module', return_value=mac), \
             patch.object(preflight.os, 'access', return_value=True), patch.object(preflight, '_json_command', return_value=result) as run:
            report = preflight.request_permissions('microphone')
            self.assertEqual(report['status'], 'completed')
            self.assertFalse(report['capture_started'])
            self.assertEqual(run.call_args.args[0], ['/prepared/helper', '--request-permissions', '--source', 'microphone'])
            self.assertEqual(run.call_args.kwargs['timeout'], 60)
            run.side_effect = TimeoutError
            self.assertEqual(preflight.request_permissions('both')['status'], 'unknown')

    def test_invalid_permission_request_never_invokes_helper(self):
        with patch.object(preflight, '_json_command') as run:
            self.assertEqual(preflight.request_permissions('bogus')['status'], 'unavailable')
            with patch.object(preflight.sys, 'platform', 'linux'):
                self.assertEqual(preflight.request_permissions('microphone')['status'], 'unavailable')
            run.assert_not_called()

    def test_swift_permission_setup_exits_before_capture(self):
        source = (Path(__file__).resolve().parents[1] / 'native/macos_capture.swift').read_text()
        branch = source.split('if args.count == 3, args[0] == "--request-permissions"', 1)[1].split('// Nonprompting readiness only:', 1)[0]
        self.assertIn('AVCaptureDevice.requestAccess', branch)
        self.assertIn('CGRequestScreenCaptureAccess()', branch)
        self.assertIn('return', branch)
        for unsafe in ('Capture()', 'startCapture', 'SCShareableContent', 'AVAudioEngine'):
            self.assertNotIn(unsafe, branch)

    def test_swift_preflight_branch_never_requests_or_captures(self):
        source = (Path(__file__).resolve().parents[1] / 'native/macos_capture.swift').read_text()
        branch = source.split('if args.count == 3, args[0] == "--preflight"', 1)[1].split('guard args.count == 3', 1)[0]
        self.assertIn('authorizationStatus', branch)
        self.assertIn('CGPreflightScreenCaptureAccess()', branch)
        self.assertIn('AVCaptureDevice.default(for: .audio)', branch)
        self.assertIn('CGGetActiveDisplayList(0, nil, &displayCount)', branch)
        for unsafe in ('requestAccess', 'Capture()', 'startCapture', 'SCShareableContent'):
            self.assertNotIn(unsafe, branch)


if __name__ == '__main__':
    unittest.main()
