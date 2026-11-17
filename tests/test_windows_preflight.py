import types
import unittest
from unittest.mock import Mock, patch
import windows_preflight as w


class WindowsPreflightTests(unittest.TestCase):
    def backend(self):
        mic = types.SimpleNamespace(id='synthetic-mic', name='Synthetic mic', isloopback=False)
        out = types.SimpleNamespace(id='synthetic-output', name='Synthetic output', isloopback=True)
        for dev in (mic, out):
            dev.recorder = Mock(return_value=types.SimpleNamespace(_ptr=object()))
        backend = types.SimpleNamespace(default_microphone=Mock(return_value=mic),
            default_speaker=Mock(return_value=out),
            get_microphone=Mock(side_effect=lambda id, **kw: mic if id == mic.id else out))
        foundation = types.SimpleNamespace(_com=types.SimpleNamespace(release=Mock()))
        backend.mediafoundation = foundation
        return backend, foundation, mic, out

    def test_both_initialize_exact_format_without_entering_or_reading(self):
        backend, foundation, mic, out = self.backend()
        with patch.object(w.sys, 'platform', 'win32'), patch.object(w.importlib.metadata, 'version', return_value='0.4.6'), patch.dict('sys.modules', {'soundcard': backend, 'soundcard.mediafoundation': foundation}):
            result = w.probe_devices('both')
        self.assertTrue(result['ready'])
        self.assertFalse(result['capture_started'])
        self.assertEqual(set(result['endpoints']), {'microphone', 'system'})
        self.assertEqual(foundation._com.release.call_count, 2)
        mic.recorder.assert_called_once_with(samplerate=16000, blocksize=1600)
        out.recorder.assert_called_once_with(samplerate=16000, blocksize=1600)

    def test_unknown_backend_version_fails_before_backend_import(self):
        with patch.object(w.sys, 'platform', 'win32'), patch.object(w.importlib.metadata, 'version', return_value='0.4.7'):
            result = w.probe_devices('microphone')
        self.assertFalse(result['ready'])
        self.assertEqual(result['error'], 'audited_soundcard_required')

    def test_init_denied_or_bad_format_never_reports_ready_or_exception_text(self):
        backend, foundation, mic, out = self.backend()
        mic.recorder.side_effect = OSError('private error detail')
        with patch.object(w.sys, 'platform', 'win32'), patch.object(w.importlib.metadata, 'version', return_value='0.4.6'), patch.dict('sys.modules', {'soundcard': backend, 'soundcard.mediafoundation': foundation}):
            result = w.probe_devices('microphone')
        self.assertFalse(result['ready'])
        self.assertNotIn('private', str(result))
        self.assertFalse(result['capture_started'])

    def test_system_only_never_initializes_microphone(self):
        backend, foundation, mic, out = self.backend()
        with patch.object(w.sys, 'platform', 'win32'), patch.object(w.importlib.metadata, 'version', return_value='0.4.6'), patch.dict('sys.modules', {'soundcard': backend, 'soundcard.mediafoundation': foundation}):
            self.assertTrue(w.probe_devices('system')['ready'])
        backend.default_microphone.assert_not_called()
        mic.recorder.assert_not_called()

    def test_loopback_identity_mismatch_fails_closed(self):
        backend, foundation, mic, out = self.backend()
        out.isloopback = False
        with patch.object(w.sys, 'platform', 'win32'), patch.object(w.importlib.metadata, 'version', return_value='0.4.6'), patch.dict('sys.modules', {'soundcard': backend, 'soundcard.mediafoundation': foundation}):
            self.assertFalse(w.probe_devices('system')['ready'])
        out.recorder.assert_not_called()
