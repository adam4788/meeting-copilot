"""Preparation is installation/build only, never a device start."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import prepare_macos


class PreparationTests(unittest.TestCase):
    def test_preparation_compiles_usage_metadata_and_never_starts_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = Path(tmp) / 'plugins/meeting-copilot-live/dashboard/core'
            (core / 'native').mkdir(parents=True)
            for name in ('audio_worker.py', 'native/macos_capture.swift', 'requirements-macos-audio.txt'):
                (core / name).write_text('synthetic')
            calls = []
            def run(argv, **kwargs):
                self.assertTrue(kwargs['check'])
                calls.append(argv)
            with patch('prepare_macos.sys.platform', 'darwin'), patch('prepare_macos.shutil.which', side_effect=lambda x: '/tools/' + x):
                result = prepare_macos.prepare(tmp, run=run)
            self.assertFalse(result['capture_started'])
            compiler = next(argv for argv in calls if argv[0].endswith('swiftc'))
            self.assertIn('-parse-as-library', compiler)
            self.assertIn('__info_plist', compiler)
            self.assertTrue((core / 'native/CaptureInfo.plist').is_file())
            self.assertFalse(any('--start' in argv for argv in calls))
            self.assertFalse(any('audio_worker.py' in argv for argv in calls))

    def test_missing_install_fails_before_commands(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp, patch('prepare_macos.sys.platform', 'darwin'), patch('prepare_macos.shutil.which', return_value='/tools/tool'):
            with self.assertRaises(FileNotFoundError):
                prepare_macos.prepare(tmp, run=lambda *args, **kwargs: calls.append(args))
        self.assertEqual(calls, [])
