"""The runtime export retains its documentation without development code."""
from html import unescape
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from install import FILES
from scripts.export_catalog_runtime import ENTRY_FILES, export_runtime


ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r'!?\[[^]]*\]\(([^)]+)\)')


def markdown_lines(path):
    """Ignore fenced examples; their links and headings are not rendered."""
    fenced = False
    for line in path.read_text(encoding='utf-8').splitlines():
        if re.match(r'^\s*(```|~~~)', line):
            fenced = not fenced
        elif not fenced:
            yield line


def headings(path):
    counts = {}
    result = set()
    for line in markdown_lines(path):
        match = re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line)
        if not match:
            continue
        title = re.sub(r'<[^>]+>', '', unescape(match.group(1))).lower()
        slug = re.sub(r'[^\w\- ]', '', title).replace(' ', '-')
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        result.add(f'{slug}-{number}' if number else slug)
    return result


def broken_links(root):
    errors = []
    for document in root.rglob('*.md'):
        for line in markdown_lines(document):
            for raw in LINK.findall(line):
                target = raw.strip().split(' ', 1)[0].strip('<>')
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue
                path = (document.parent / unquote(parsed.path)).resolve() if parsed.path else document
                if not path.is_file():
                    errors.append(f'{document.relative_to(root)}: {target} is missing')
                elif parsed.fragment and path.suffix.lower() == '.md':
                    if unquote(parsed.fragment).lower() not in headings(path):
                        errors.append(f'{document.relative_to(root)}: {target} has no heading')
    return errors


class ExportTests(unittest.TestCase):
    def fixture(self, root):
        root.mkdir()
        names = set(FILES) | set(ENTRY_FILES) | {
            'tests/verify_overlay_eof.py', 'tests/test_server.py',
            'docs/byok.md', 'docs/assets/unrelated.png',
            'docs/assets/overlay-controls.png',
        }
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic ' + name, encoding='utf-8')
        (root / 'README.md').write_text('# Synthetic – overview\n', encoding='utf-8')
        (root / 'docs/assets/meeting-copilot-hero.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        (root / '.env').write_text('synthetic-secret', encoding='utf-8')
        return names

    def test_release_excludes_developer_sources_without_changing_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, dest = root / 'source', root / 'release'
            tracked = self.fixture(source)
            export_runtime(source, dest, 'a' * 40, tracked)
            for name in (set(FILES) | set(ENTRY_FILES)) - {'README.md'}:
                self.assertEqual((source / name).read_bytes(), (dest / name).read_bytes(), name)
            self.assertTrue({'native_runtime.py', 'requirements-macos-audio.lock'} <= set(FILES))
            for name in ('native_runtime.py', 'requirements-macos-audio.lock'):
                self.assertEqual((dest / name).read_bytes(), (source / name).read_bytes())
            self.assertTrue((dest / 'README.md').read_bytes().endswith((source / 'README.md').read_bytes()))
            for name in ('.env', 'tests/verify_overlay_eof.py', 'tests/test_server.py',
                         'docs/assets/unrelated.png'):
                self.assertFalse((dest / name).exists(), name)
            self.assertTrue((source / 'tests/verify_overlay_eof.py').exists())
            self.assertEqual((dest / 'SOURCE_REVISION').read_text(encoding='utf-8'), 'a' * 40 + '\n')

    def test_reviewed_screenshot_is_in_the_runtime_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, dest = root / 'source', root / 'release'
            tracked = self.fixture(source)
            export_runtime(source, dest, 'a' * 40, tracked)
            for screenshot in ('docs/assets/overlay-controls.png', 'docs/assets/workflow.svg'):
                self.assertTrue((dest / screenshot).is_file())
                self.assertEqual((dest / screenshot).read_bytes(), (source / screenshot).read_bytes())
            self.assertFalse((dest / 'docs/assets/unrelated.png').exists())

    def test_missing_source_or_existing_destination_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            tracked = self.fixture(source)
            for missing in ('server.py', 'docs/assets/meeting-copilot-hero.png', 'LICENSE',
                            'requirements-audio.txt'):
                with self.subTest(missing=missing), self.assertRaises(ValueError):
                    export_runtime(source, root / 'bad', 'a' * 40, tracked - {missing})
                self.assertFalse((root / 'bad').exists())
            existing = root / 'existing'
            existing.mkdir()
            (existing / 'sentinel').write_text('keep', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                export_runtime(source, existing, 'a' * 40, tracked)
            self.assertEqual((existing / 'sentinel').read_text(encoding='utf-8'), 'keep')

    def test_worktree_export_has_closed_local_documentation_links(self):
        # Exercise packaging before new required files are staged. The export
        # CLI separately requires a clean tree and uses tracked files only.
        tracked = set(subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT, text=True).split('\0'))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'release'
            export_runtime(ROOT, dest, 'a' * 40, tracked)
            self.assertEqual(broken_links(dest), [])
            self.assertEqual((dest / 'docs/assets/meeting-copilot-hero.png').read_bytes(),
                             (ROOT / 'docs/assets/meeting-copilot-hero.png').read_bytes())
            self.assertFalse((dest / 'tests').exists())
            self.assertFalse((dest / 'scripts').exists())


if __name__ == '__main__':
    unittest.main()
