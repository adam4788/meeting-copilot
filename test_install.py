import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallerTests(unittest.TestCase):
    def test_default_home_honors_active_profile_and_native_windows_location(self):
        with patch.dict('os.environ', {'HERMES_HOME': '  C:/profiles/extra  '}, clear=True):
            self.assertEqual(install.default_home(), Path('C:/profiles/extra'))
        with patch.dict('os.environ', {'LOCALAPPDATA': 'C:/Local', 'HERMES_HOME': ' '}, clear=True), patch.object(install.sys, 'platform', 'win32'):
            self.assertEqual(install.default_home(), Path('C:/Local/hermes'))
        with patch.dict('os.environ', {}, clear=True), patch.object(install.sys, 'platform', 'win32'), patch.object(install.Path, 'home', return_value=Path('C:/Account')):
            self.assertEqual(install.default_home(), Path('C:/Account/AppData/Local/hermes'))
        with patch.dict('os.environ', {}, clear=True), patch.object(install.sys, 'platform', 'linux'), patch.object(install.Path, 'home', return_value=Path('/account')):
            self.assertEqual(install.default_home(), Path('/account/.hermes'))

    def test_independent_runtime_files_are_in_install_manifest(self):
        self.assertTrue({'macos_audio.py', 'audio_worker.py', 'model_selection.py', 'meta_models.py', 'overlay_bridge.py', 'native/macos_capture.swift', 'native/macos_overlay.swift', 'requirements-macos-audio.txt'} <= set(install.FILES))

    def test_missing_source_fails_before_mutating_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, home = Path(tmp) / 'source', Path(tmp) / 'profile'
            source.mkdir()
            with self.assertRaises(FileNotFoundError):
                install.install(source, home)
            self.assertFalse(home.exists())

    def test_scoped_copy_backup_and_no_config_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, home = Path(tmp) / 'source', Path(tmp) / 'profile'
            (source / 'desktop').mkdir(parents=True)
            home.mkdir()
            (home / 'config.yaml').write_text('plugins: {enabled: [existing]}\n', encoding='utf-8')
            for name in install.FILES:
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                (source / name).write_text('# Synthetic fixture\n', encoding='utf-8')
            (source / 'desktop/plugin.js').write_text('export default {};\n', encoding='utf-8')
            (source / 'plugin.yaml').write_text((Path(__file__).parent / 'plugin.yaml').read_text())
            original = (home / 'config.yaml').read_bytes()
            first = install.install(source, home)
            self.assertIsNone(first['backup'])
            backend = home / 'plugins/meeting-copilot-live/dashboard'
            self.assertEqual((backend.parent / 'plugin.yaml').read_bytes(),
                             (source / 'plugin.yaml').read_bytes())
            api_file = json.loads((backend / 'manifest.json').read_text())['api']
            self.assertRegex(api_file, r'^plugin_api_[0-9a-f]{16}\.py$')
            self.assertTrue((backend / api_file).is_file())
            for name in install.FILES:
                self.assertEqual((backend / 'core' / name).read_bytes(), (source / name).read_bytes())
            self.assertTrue((home / 'desktop-plugins/meeting-copilot-live/plugin.js').is_file())
            second = install.install(source, home)
            self.assertTrue(Path(second['backup']).is_dir())
            self.assertEqual((home / 'config.yaml').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
