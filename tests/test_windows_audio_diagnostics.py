import unittest
from types import SimpleNamespace
from scripts.windows_audio_diagnostics import diagnose


class WindowsDiagnosticsTests(unittest.TestCase):
    def test_default_never_loads_backend(self):
        def forbidden():
            raise AssertionError('loaded native library')
        result = diagnose(platform='win32', find_spec=lambda _: object(), load_backend=forbidden)
        self.assertFalse(result['capture_verified'])
        self.assertFalse(result['stream_opened'])
        self.assertNotIn('endpoints', result)

    def test_missing_dependency_does_not_load_backend(self):
        result = diagnose(devices=True, platform='win32', find_spec=lambda _: None,
                          load_backend=lambda: self.fail('loaded missing dependency'))
        self.assertIn('SoundCard', result['errors'][0])

    def test_endpoint_listing_is_not_capture(self):
        mic = SimpleNamespace(id='private-mic-id', name='Test microphone')
        out = SimpleNamespace(id='private-output-id', name='Test output')
        backend = SimpleNamespace(all_microphones=lambda **_: [mic], all_speakers=lambda: [out],
                                  default_microphone=lambda: mic, default_speaker=lambda: out)
        result = diagnose(devices=True, platform='win32', find_spec=lambda _: object(), load_backend=lambda: backend)
        self.assertFalse(result['capture_verified'])
        self.assertEqual(result['endpoints']['microphones'][0]['default'], True)
        self.assertNotIn('private-mic-id', str(result))
        self.assertEqual(result['errors'], [])

    def test_non_windows_does_not_load_backend(self):
        result = diagnose(devices=True, platform='linux', find_spec=lambda _: object(),
                          load_backend=lambda: self.fail('loaded native backend'))
        self.assertIn('Windows', result['errors'][0])
