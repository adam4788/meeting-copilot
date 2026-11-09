"""The optional native audio setup uses reviewed, hash-pinned PyPI wheels."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent


class MacOSDependencyLockTests(unittest.TestCase):
    def test_audio_lock_pins_every_dependency_with_sha256(self):
        path = ROOT / 'requirements-macos-audio.lock'
        self.assertTrue(path.is_file(), 'native setup needs a committed hash-pinned wheel lock')
        text = path.read_text(encoding='utf-8')
        logical = text.replace('\\\n', '')
        requirements = [line.strip() for line in logical.splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
        self.assertGreater(len(requirements), 4, 'lock must include transitive dependencies')
        names = set()
        for line in requirements:
            with self.subTest(requirement=line.split()[0]):
                self.assertRegex(line, r'^[A-Za-z0-9_.-]+==[^\s;]+(?:\s*;[^\\]+)?\s+--hash=sha256:[0-9a-f]{64}')
                self.assertNotIn('://', line, 'use official index resolution, not arbitrary URLs')
                hashes = re.findall(r'--hash=sha256:([^\s]+)', line)
                self.assertTrue(hashes)
                self.assertTrue(all(re.fullmatch('[0-9a-f]{64}', value) for value in hashes))
                names.add(line.split('==', 1)[0].lower().replace('_', '-'))
        self.assertTrue({'faster-whisper', 'av', 'numpy', 'websockets'} <= names)
