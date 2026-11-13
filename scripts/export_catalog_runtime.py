"""Build a source-only runtime release, with an explicit allowlist, never publish.

Development tests/mutators remain in the source commit. This does not alter the
scanner, add ignores to it, or claim a full development checkout is warning-free.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess

from install import FILES

ENTRY_FILES = ('__init__.py', 'plugin.yaml', 'desktop/plugin.js',
               'dashboard/manifest.json', 'dashboard/plugin_api.py',
               'prepare_macos.py', 'install.py', 'README.md', 'SECURITY.md',
               'CONTRIBUTING.md', '.gitignore', 'LICENSE', 'requirements-audio.txt',
               'docs/assets/meeting-copilot-hero.png',
               'docs/assets/overlay-controls.png',
               'docs/assets/workflow.svg')


def export_runtime(source, destination, revision, tracked):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Full source commit required')
    if destination == source or source in destination.parents:
        raise ValueError('Runtime export must be outside the development tree')
    if destination.exists():
        raise FileExistsError('Use a new export directory; existing files are never overwritten')
    selected = set(FILES) | set(ENTRY_FILES)
    selected |= {p for p in tracked if p.startswith(('docs/', 'sample-docs/')) and
                 Path(p).suffix in ('.md', '.txt', '.rst', '.sample')}
    for name in selected:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or name not in tracked:
            raise ValueError('Required runtime source is not tracked: '+name)
        if (source / name).is_symlink() or not (source / name).is_file():
            raise ValueError('Expected a regular runtime source: '+name)
    destination.mkdir(parents=True)
    for name in sorted(selected):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    (destination / 'SOURCE_REVISION').write_text(revision+'\n', encoding='utf-8', newline='\n')
    readme = destination / 'README.md'
    notice = ('> Runtime-only source release. Development tests and mutation probes '
              'are retained in source commit `'+revision+'`. Their omission is an '
              'explicit packaging boundary, not a scanner suppression.\n\n')
    readme.write_bytes(notice.encode('utf-8') + readme.read_bytes())
    return sorted(selected | {'SOURCE_REVISION'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    if subprocess.check_output(['git','status','--porcelain'],cwd=source):
        raise SystemExit('Commit development changes before exporting a release')
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    tracked = set(subprocess.check_output(['git','ls-files','-z'],cwd=source,text=True).split('\0'))
    files = export_runtime(source,args.destination,revision,tracked)
    print(f'Exported {len(files)} source files from {revision}; no publication performed.')


if __name__ == '__main__':
    main()
