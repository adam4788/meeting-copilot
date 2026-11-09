"""Synthetic catalog preparation and runtime discovery; no devices or installs."""
import contextvars
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import audio_worker
import macos_audio
import native_runtime
import prepare_macos
import server


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'profile'
        self.plugin = self.home / 'plugins/meeting-copilot-live'
        (self.plugin / 'native').mkdir(parents=True)
        for name in ('plugin.yaml', 'audio_worker.py', 'overlay_bridge.py',
                     'native/macos_capture.swift', 'native/macos_overlay.swift',
                     'requirements-macos-audio.txt', 'requirements-macos-audio.lock'):
            (self.plugin / name).write_text('fixture-' + name)
        (self.plugin / '.git').mkdir()
        (self.plugin / '.git/HEAD').write_text('a' * 40 + '\n')
        self.constants = types.SimpleNamespace(get_hermes_home=lambda: self.home)

    def prepare(self):
        def run(argv, **kwargs):
            if 'venv' in argv:
                python = Path(argv[-1]) / 'bin/python'
                python.parent.mkdir(parents=True, exist_ok=True)
                python.symlink_to(sys.executable)
            if '-o' in argv:
                output = Path(argv[argv.index('-o') + 1])
                output.touch()
                output.chmod(0o755)
        with patch('prepare_macos.platform.machine', return_value='arm64'), patch('prepare_macos.sys.platform', 'darwin'), patch('prepare_macos.shutil.which', return_value='/tools/tool'):
            return prepare_macos.prepare(self.home, run=run)

    def test_prepared_paths_are_used_by_capture_and_overlay(self):
        result = self.prepare()
        with patch.dict(sys.modules, {'hermes_constants': self.constants}), \
             patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': '', 'MEETING_COPILOT_OVERLAY_HELPER': ''}), \
             patch('macos_audio._model_api_key', return_value=''), \
             patch.object(macos_audio, '__file__', str(self.plugin / 'macos_audio.py')), \
             patch.object(server, '__file__', str(self.plugin / 'server.py')):
            # An empty override is not configured.
            os.environ.pop('MEETING_COPILOT_AUDIO_PYTHON')
            self.assertEqual(macos_audio.worker_python(self.plugin), result['runtime'])
            self.assertEqual(str(macos_audio.capture_helper(self.plugin)), result['helper'])
            self.assertTrue(macos_audio.MacOSAudioCapture().devices()['helper_available'])
            state = server.MeetingState()
            self.assertTrue(state._macos_runtime_ready())
            self.assertEqual(state._overlay_helper_path(), result['overlay'])
            old_overlay = self.home.parent / 'old-overlay'
            old_overlay.touch()
            with patch.dict(os.environ, {'MEETING_COPILOT_OVERLAY_HELPER': str(old_overlay)}):
                self.assertEqual(state._overlay_helper_path(), result['overlay'])
            other = self.home.parent / 'another-profile'
            self.constants.get_hermes_home = lambda: other
            self.assertFalse(state._macos_runtime_ready())
            self.assertIsNone(state._overlay_helper_path())

    def test_worker_thread_uses_calling_profile_and_prepared_command(self):
        result = self.prepare()
        profile = contextvars.ContextVar('synthetic_profile', default=self.home.parent / 'wrong')
        token = profile.set(self.home)
        calls = []
        class FakeProcess:
            stdout = io.BytesIO(b'')
            def poll(self): return 0
        try:
            self.constants.get_hermes_home = profile.get
            with patch.dict(sys.modules, {'hermes_constants': self.constants}), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(macos_audio, '__file__', str(self.plugin / 'macos_audio.py')), \
                 patch('macos_audio.subprocess.Popen', side_effect=lambda command, **kw: calls.append(command) or FakeProcess()), \
                 patch('macos_audio.terminate_group'):
                capture = macos_audio.MacOSAudioCapture()
                capture.start(lambda segment: None)
                capture._thread.join(3)
                self.assertFalse(capture._thread.is_alive())
            self.assertEqual(calls[0][0], result['runtime'])
            self.assertEqual(calls[0][calls[0].index('--helper') + 1], result['helper'])
        finally:
            profile.reset(token)

    def test_missing_public_resolver_uses_profile_environment_only(self):
        with patch.dict(sys.modules, {'hermes_constants': None}), \
             patch.dict(os.environ, {'HERMES_HOME': str(self.home)}, clear=True):
            self.assertEqual(native_runtime.effective_home(), self.home)
        self.constants.get_hermes_home = lambda: (_ for _ in ()).throw(RuntimeError('resolver failed'))
        with patch.dict(sys.modules, {'hermes_constants': self.constants}), \
             patch.dict(os.environ, {'HERMES_HOME': str(self.home)}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'resolver failed'):
                native_runtime.effective_home()

    def test_checkout_without_release_pin_fails_closed(self):
        self.prepare()
        (self.plugin / 'SOURCE_REVISION').write_text('a' * 40 + '\n')
        shutil.rmtree(self.plugin / '.git')
        with self.assertRaisesRegex(ValueError, 'no Git revision'):
            native_runtime.artifact_dir(self.plugin, self.home)
        with patch.dict(sys.modules, {'hermes_constants': self.constants}), \
             patch.object(server, '__file__', str(self.plugin / 'server.py')):
            self.assertFalse(server.MeetingState()._macos_runtime_ready())
            self.assertIsNone(server.MeetingState()._overlay_helper_path())

    def test_repin_and_each_build_input_invalidate_old_artifacts(self):
        with patch.dict(sys.modules, {'hermes_constants': self.constants}), \
             patch.object(server, '__file__', str(self.plugin / 'server.py')):
            original = self.prepare()
            # Old unversioned checkout artifacts may exist from a prior setup;
            # none may be used after a repin or input change.
            (self.plugin / 'macos_capture').touch()
            (self.plugin / 'macos_overlay').touch()
            stale_python = self.plugin / 'audio-runtime/bin/python'
            stale_python.parent.mkdir(parents=True)
            stale_python.touch()
            stale_python.chmod(0o755)
            for name in ('native/macos_capture.swift', 'native/macos_overlay.swift',
                         'requirements-macos-audio.txt', 'requirements-macos-audio.lock', '.git/HEAD'):
                path = self.plugin / name
                old = path.read_bytes()
                path.write_bytes((b'b' * 40 + b'\n') if name == '.git/HEAD' else old + b'changed')
                try:
                    with self.subTest(name=name):
                        self.assertNotEqual(str(macos_audio.capture_helper(self.plugin)), original['helper'])
                        self.assertIsNone(server.MeetingState()._overlay_helper_path())
                        with self.assertRaises(RuntimeError):
                            macos_audio.worker_python(self.plugin)
                finally:
                    path.write_bytes(old)

    def test_worker_main_passes_selected_helper_to_both_providers(self):
        helper = self.plugin / 'helper'
        helper.touch()
        with patch('audio_worker.load_model', return_value=object()), \
             patch('audio_worker.run_capture') as local, patch('audio_worker.emit'):
            self.assertEqual(audio_worker.main(['--helper', str(helper)]), 0)
            self.assertEqual(local.call_args.kwargs['helper'], helper)
        meta = types.SimpleNamespace(DEFAULT_MODEL='synthetic')
        with patch.dict(sys.modules, {'meta_asr': meta}), patch('audio_worker._read_meta_worker_key', return_value='synthetic'), \
             patch('audio_worker.run_capture_meta') as remote, patch('audio_worker.emit'):
            self.assertEqual(audio_worker.main(['--helper', str(helper), '--provider', 'meta']), 0)
            self.assertEqual(remote.call_args.kwargs['helper'], helper)

    def test_explicit_override_and_legacy_paths_remain_usable(self):
        legacy = self.home / 'plugins/meeting-copilot-live/dashboard/core'
        legacy.mkdir(parents=True)
        (legacy / 'macos_capture').touch()
        (legacy / 'audio_worker.py').touch()
        (legacy / 'macos_overlay').touch()
        python = legacy / 'audio-runtime/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        python.chmod(0o755)
        with patch.dict(os.environ, {}, clear=True), patch.object(server, '__file__', str(legacy / 'server.py')):
            self.assertEqual(macos_audio.worker_python(legacy), str(python))
            self.assertEqual(macos_audio.capture_helper(legacy), legacy / 'macos_capture')
            self.assertEqual(server.MeetingState()._overlay_helper_path(), str(legacy / 'macos_overlay'))
            self.assertTrue(server.MeetingState()._macos_runtime_ready())
            custom_overlay = self.home / 'custom-overlay'
            custom_overlay.touch()
            with patch.dict(os.environ, {'MEETING_COPILOT_OVERLAY_HELPER': str(custom_overlay)}):
                self.assertEqual(server.MeetingState()._overlay_helper_path(), str(custom_overlay))
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}):
            self.assertEqual(macos_audio.worker_python(self.plugin), sys.executable)
        with patch.dict(os.environ, {'MEETING_COPILOT_AUDIO_PYTHON': '/invalid'}):
            with self.assertRaises(RuntimeError):
                macos_audio.worker_python(self.plugin)


if __name__ == '__main__':
    unittest.main()
