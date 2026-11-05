"""Runtime packaging keeps code identical and development mutation checks separate."""
from pathlib import Path
import tempfile
import unittest
from export_catalog_runtime import export_runtime, ENTRY_FILES
from install import FILES


class ExportTests(unittest.TestCase):
    def fixture(self, root):
        root.mkdir()
        names=set(FILES)|set(ENTRY_FILES)|{'verify_overlay_eof.py','real_effect_test_backend.py',
                                         'test_real_effects.cjs','test_server.py','docs/byok.md'}
        for name in names:
            p=root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_text('# synthetic '+name)
        (root/'.env').write_text('synthetic-secret')
        return names

    def test_release_excludes_developer_sources_without_changing_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source'; tracked=self.fixture(source); dest=root/'release'
            files=export_runtime(source,dest,'a'*40,tracked)
            for name in set(FILES)|set(ENTRY_FILES)-{'README.md'}:
                self.assertEqual((source/name).read_bytes(),(dest/name).read_bytes())
            self.assertTrue((dest/'README.md').read_text().endswith((source/'README.md').read_text()))
            for name in ('.env','verify_overlay_eof.py','real_effect_test_backend.py','test_real_effects.cjs','test_server.py'):
                self.assertFalse((dest/name).exists())
            self.assertTrue((source/'verify_overlay_eof.py').exists())
            self.assertTrue((dest/'prepare_macos.py').exists())
            self.assertEqual((dest/'SOURCE_REVISION').read_text(),'a'*40+'\n')

    def test_missing_source_or_existing_destination_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source'; tracked=self.fixture(source)
            with self.assertRaises(ValueError):export_runtime(source,root/'bad','a'*40,tracked-{'server.py'})
            self.assertFalse((root/'bad').exists())
            existing=root/'existing'; existing.mkdir(); (existing/'sentinel').write_text('keep')
            with self.assertRaises(FileExistsError):export_runtime(source,existing,'a'*40,tracked)
            self.assertEqual((existing/'sentinel').read_text(),'keep')
