"""Install only this project's files into the active desktop profile. Never enables capture."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

FILES = ('host_default.py', 'runtime_loader.py', 'capture_preflight.py', 'transcript_snapshot.py', 'server.py', 'credentials.py', 'byok.py', 'knowledge.py', 'web_policy.py', 'audio.py', 'meetily.py',
         'macos_audio.py', 'audio_worker.py', 'native_runtime.py', 'model_selection.py', 'meta_models.py', 'meta_asr.py', 'google_asr.py', 'google_preview.py', 'google_fixture.py',
         'overlay_bridge.py', 'answer_core.py', 'hermes_adapters.py', 'ask.py', 'eval_retrieval.py',
         'demo_standalone.py',
         'native/macos_capture.swift', 'native/macos_overlay.swift', 'requirements-macos-audio.txt',
         'requirements-macos-audio.lock')


def install(source, home):
    source, home = Path(source).resolve(), Path(home).expanduser().resolve()
    for name in (*FILES, '__init__.py', 'dashboard/plugin_api.py', 'desktop/plugin.js', 'plugin.yaml'):
        if not (source / name).is_file():
            raise FileNotFoundError(f'Required source missing: {name}')
    build_inputs = (*FILES, '__init__.py', 'dashboard/plugin_api.py')
    build = hashlib.sha256(b''.join((source / name).read_bytes() for name in build_inputs)).hexdigest()[:16]
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
    shutil.copy2(source / '__init__.py', target / '__init__.py')
    shutil.copy2(source / 'plugin.yaml', target / 'plugin.yaml')
    dashboard = target / 'dashboard'
    (dashboard / 'manifest.json').write_text(json.dumps({
        'name': 'meeting-copilot-live', 'label': 'Meeting Copilot', 'version': '0.1.0',
        'tab': {'hidden': True}, 'entry': 'dist/index.js', 'api': api_file
    }, indent=2), encoding='utf-8')
    (dashboard / 'dist').mkdir(exist_ok=True)
    (dashboard / 'dist' / 'index.js').write_text('// Desktop-only UI; API manifest intentionally has no dashboard tab.\n', encoding='utf-8')
    shutil.copy2(source / 'dashboard/plugin_api.py', dashboard / api_file)
    shutil.copy2(source / 'desktop' / 'plugin.js', desktop / 'plugin.js')
    return {'backend': str(target), 'desktop': str(desktop), 'backup': str(backup) if backup.exists() else None}


def default_home():
    """Standalone fallback matching native Hermes; --home selects a named profile."""
    override = os.environ.get('HERMES_HOME', '').strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    suffix = os.environ.get('HERMES_DATA_DIR_SUFFIX', '')
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA', '').strip()
        return (Path(local) if local else Path.home() / 'AppData' / 'Local') / ('hermes' + suffix)
    return Path.home() / ('.hermes' + suffix)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', default=default_home(),
                        help='Active profile home. Defaults to HERMES_HOME, otherwise the native Hermes data directory (LOCALAPPDATA/hermes on Windows).')
    args = parser.parse_args()
    print(json.dumps(install(Path(__file__).parent, args.home), indent=2))
    print('Next: hermes plugins enable meeting-copilot-live --no-allow-tool-override')
    print('The desktop pane should hot-load. Capture remains paused until explicit consent + Start.')


if __name__ == '__main__':
    main()
