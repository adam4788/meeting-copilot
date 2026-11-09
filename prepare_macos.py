"""Prepare isolated macOS transcription dependencies without opening audio devices."""
import argparse
import os
import platform
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

# Running setup from a catalog checkout must not create __pycache__ there.
_bytecode_setting = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    if __package__:
        from . import native_runtime
    else:
        import native_runtime
finally:
    sys.dont_write_bytecode = _bytecode_setting


def prepare(home, run=subprocess.run):
    if sys.platform != 'darwin' or platform.machine().lower() != 'arm64':
        raise RuntimeError('Independent macOS capture requires Apple Silicon macOS.')
    uv = shutil.which('uv')
    swiftc = shutil.which('swiftc')
    if not uv or not swiftc:
        raise RuntimeError('Install uv and Apple Command Line Tools before preparing audio.')
    plugin = Path(home).expanduser().resolve() / 'plugins/meeting-copilot-live'
    core = plugin / 'dashboard/core'
    # Catalog installs load the pinned tree directly. Legacy installs retain
    # their existing dashboard/core layout and in-place artifacts.
    if (plugin / 'audio_worker.py').is_file() and (plugin / 'plugin.yaml').is_file():
        core = plugin
    for name in ('audio_worker.py', 'overlay_bridge.py', 'native/macos_capture.swift',
                 'native/macos_overlay.swift', 'requirements-macos-audio.txt',
                 'requirements-macos-audio.lock'):
        if not (core / name).is_file():
            raise FileNotFoundError('Install this version with python install.py first.')
    artifacts = native_runtime.artifact_dir(core, home)
    if native_runtime.is_catalog(core):
        if core.resolve() in artifacts.resolve().parents:
            raise ValueError('Artifact storage must be outside the catalog checkout')
        artifacts.mkdir(parents=True, exist_ok=True)
    runtime = artifacts / 'audio-runtime'
    python = runtime / 'bin/python'
    # Do not let inherited uv/pip configuration replace the official index,
    # inject extra requirements, or shadow the isolated interpreter.
    uv_env = {k: v for k, v in os.environ.items()
              if not k.upper().startswith(('UV_', 'PIP_'))
              and k not in ('PYTHONPATH', 'PYTHONHOME')}
    if not python.is_file():
        run([uv, 'venv', '--no-config', '--python', '3.12', '--no-python-downloads', str(runtime)],
            check=True, env=uv_env)
    run([uv, 'pip', 'install', '--no-config', '--default-index', 'https://pypi.org/simple',
         '--require-hashes', '--only-binary', ':all:', '--python', str(python),
         '-r', str(core / 'requirements-macos-audio.lock')], check=True, env=uv_env)
    info = (artifacts / 'CaptureInfo.plist' if native_runtime.is_catalog(core)
            else core / 'native/CaptureInfo.plist')
    info.write_bytes(plistlib.dumps({
        'CFBundleIdentifier': 'com.adam.meeting-copilot.capture',
        'CFBundleName': 'Meeting Copilot Audio',
        'NSMicrophoneUsageDescription': 'Transcribe the microphone locally after you explicitly start Meeting Copilot.',
        'NSScreenCaptureUsageDescription': 'Transcribe system audio locally after you explicitly select system audio and start capture.',
    }))
    helper = artifacts / 'macos_capture'
    run([swiftc, '-parse-as-library', str(core / 'native/macos_capture.swift'), '-O', '-o', str(helper),
         '-Xlinker', '-sectcreate', '-Xlinker', '__TEXT', '-Xlinker', '__info_plist', '-Xlinker', str(info)], check=True)
    run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', 'com.adam.meeting-copilot.capture', str(helper)], check=True)
    overlay = artifacts / 'macos_overlay'
    run([swiftc, '-parse-as-library', str(core / 'native/macos_overlay.swift'), '-O', '-o', str(overlay)], check=True)
    return {'runtime': str(python), 'helper': str(helper), 'overlay': str(overlay), 'capture_started': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home')
    args = parser.parse_args()
    import json
    print(json.dumps(prepare(args.home or native_runtime.effective_home()), indent=2))
