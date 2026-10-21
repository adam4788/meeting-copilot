import json
from pathlib import Path
import tempfile
import unittest

import install


class InstallerTests(unittest.TestCase):
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
                (source / name).write_text('# Synthetic fixture\n', encoding='utf-8')
            (source / 'desktop/plugin.js').write_text('export default {};\n', encoding='utf-8')
            original = (home / 'config.yaml').read_bytes()
            first = install.install(source, home)
            self.assertIsNone(first['backup'])
            backend = home / 'plugins/meeting-copilot-live/dashboard'
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
