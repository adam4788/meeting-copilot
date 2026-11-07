"""Device-free Windows setup and backend contracts; never opens an audio stream."""
import unittest
from unittest.mock import patch

import audio
import server


class WindowsSetupTests(unittest.TestCase):
    def test_windows_audio_dependencies_are_declared_with_platform_markers(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        manifest = (root / 'plugin.yaml').read_text(encoding='utf-8')
        audio_requirements = (root / 'requirements-audio.txt').read_text().splitlines()
        for requirement in audio_requirements:
            if requirement.strip() and not requirement.startswith('#'):
                self.assertIn('  - "' + requirement + "; sys_platform == 'win32'\"", manifest)

    def test_readiness_checks_metadata_without_importing_audio_packages(self):
        seen = []
        def find(name):
            seen.append(name)
            return None if name == 'soundcard' else object()
        with patch.object(audio.sys, 'platform', 'win32'), patch.object(audio.importlib.util, 'find_spec', side_effect=find), patch('builtins.__import__', side_effect=AssertionError('no runtime imports')):
            result = audio.windows_readiness()
        self.assertFalse(result['ready'])
        self.assertIn('SoundCard', result['detail'])
        self.assertEqual(seen, ['numpy', 'soundcard', 'faster_whisper'])

    def test_readiness_is_not_a_hardware_claim(self):
        with patch.object(audio.sys, 'platform', 'win32'), patch.object(audio.importlib.util, 'find_spec', return_value=object()):
            result = audio.windows_readiness()
        self.assertTrue(result['ready'])
        self.assertIn('hardware unverified', result['detail'])
        with patch.object(audio.sys, 'platform', 'darwin'):
            self.assertFalse(audio.windows_readiness()['ready'])

    def test_status_exposes_windows_readiness_without_creating_capture(self):
        with patch.object(audio, 'windows_readiness', return_value={'ready': False, 'detail': 'Install optional audio dependencies.'}), patch.object(audio, 'AudioCapture', side_effect=AssertionError('capture constructed')):
            state = server.MeetingState()
            result = state.status()
        self.assertFalse(result['capabilities']['windows_transcription'])
        self.assertIn('optional', result['capabilities']['windows_detail'])
        self.assertIsNone(state.audio)

    def test_native_backend_passes_selected_model_and_source(self):
        with patch.object(audio, 'AudioCapture') as factory:
            server.MeetingState().get_audio('native', 'tiny.en', 'system')
        factory.assert_called_once_with(model_size='tiny.en', source='system')


if __name__ == '__main__':
    unittest.main()
