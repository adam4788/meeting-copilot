"""Install only this project's files into the active desktop profile. Never enables capture."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil

FILES = ('server.py', 'knowledge.py', 'web_policy.py', 'audio.py', 'meetily.py',
         'macos_audio.py', 'audio_worker.py', 'model_selection.py', 'meta_models.py',
         'overlay_bridge.py',
         'native/macos_capture.swift', 'native/macos_overlay.swift', 'requirements-macos-audio.txt')


def install(source, home):
    source, home = Path(source).resolve(), Path(home).expanduser().resolve()
    for name in (*FILES, 'desktop/plugin.js'):
        if not (source / name).is_file():
            raise FileNotFoundError(f'Required source missing: {name}')
    build = hashlib.sha256(b''.join((source / name).read_bytes() for name in FILES)).hexdigest()[:16]
    api_file = f'plugin_api_{build}.py'
    target = home / 'plugins' / 'meeting-copilot-live'
    desktop = home / 'desktop-plugins' / 'meeting-copilot-live'
    backup = home / 'backups' / ('meeting-copilot-live-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    for path in (target, desktop):
        if path.exists():
            shutil.copytree(path, backup / path.parent.name / path.name,
                            ignore=shutil.ignore_patterns('audio-runtime', '__pycache__'))
    core = target / 'dashboard' / 'core'
    core.mkdir(parents=True, exist_ok=True)
    desktop.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        (core / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, core / name)
    (core / '__init__.py').write_text('', encoding='utf-8')
    (target / '__init__.py').write_text('def register(ctx):\n    pass\n', encoding='utf-8')
    (target / 'plugin.yaml').write_text('''name: meeting-copilot-live
label: Meeting Copilot
version: 0.1.0
author: Adam Nugroho
description: Read-only live meeting answers grounded in project files and past sessions.
requires_hermes: ">=0.22.0"
provides_tools: []
provides_hooks: []
''', encoding='utf-8')
    dashboard = target / 'dashboard'
    (dashboard / 'manifest.json').write_text(json.dumps({
        'name': 'meeting-copilot-live', 'label': 'Meeting Copilot', 'version': '0.1.0',
        'tab': {'hidden': True}, 'entry': 'dist/index.js', 'api': api_file
    }, indent=2), encoding='utf-8')
    (dashboard / 'dist').mkdir(exist_ok=True)
    (dashboard / 'dist' / 'index.js').write_text('// Desktop-only UI; API manifest intentionally has no dashboard tab.\n', encoding='utf-8')
    (dashboard / api_file).write_text('''"""Load a profile-specific package without mutating sys.path."""
import hashlib
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parent / 'core'
name = '_meeting_copilot_' + hashlib.sha256((str(root) + Path(__file__).stem).encode()).hexdigest()[:20]
if name not in sys.modules:
    spec = importlib.util.spec_from_file_location(name, root / '__init__.py', submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
from importlib import import_module
router = import_module(name + '.server').router
''', encoding='utf-8')
    shutil.copy2(source / 'desktop' / 'plugin.js', desktop / 'plugin.js')
    return {'backend': str(target), 'desktop': str(desktop), 'backup': str(backup) if backup.exists() else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', default=os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')),
                        help='Active profile home. Defaults to HERMES_HOME or ~/.hermes.')
    args = parser.parse_args()
    print(json.dumps(install(Path(__file__).parent, args.home), indent=2))
    print('Next: hermes plugins enable meeting-copilot-live --no-allow-tool-override')
    print('The desktop pane should hot-load. Capture remains paused until explicit consent + Start.')


if __name__ == '__main__':
    main()
