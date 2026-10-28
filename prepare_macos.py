"""Prepare isolated macOS transcription dependencies without opening audio devices."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


def prepare(home, run=subprocess.run):
    if sys.platform != 'darwin':
        raise RuntimeError('Independent macOS capture requires macOS.')
    uv = shutil.which('uv')
    swiftc = shutil.which('swiftc')
    if not uv or not swiftc:
        raise RuntimeError('Install uv and Apple Command Line Tools before preparing audio.')
    core = Path(home).expanduser().resolve() / 'plugins/meeting-copilot-live/dashboard/core'
    for name in ('audio_worker.py', 'overlay_bridge.py', 'native/macos_capture.swift',
                 'native/macos_overlay.swift', 'requirements-macos-audio.txt'):
        if not (core / name).is_file():
            raise FileNotFoundError('Install this version with python install.py first.')
    runtime = core / 'audio-runtime'
    python = runtime / 'bin/python'
    if not python.is_file():
        run([uv, 'venv', '--python', '3.12', str(runtime)], check=True)
    run([uv, 'pip', 'install', '--python', str(python), '-r', str(core / 'requirements-macos-audio.txt')], check=True)
    info = core / 'native/CaptureInfo.plist'
    info.write_bytes(plistlib.dumps({
        'CFBundleIdentifier': 'com.adam.meeting-copilot.capture',
        'CFBundleName': 'Meeting Copilot Audio',
        'NSMicrophoneUsageDescription': 'Transcribe the microphone locally after you explicitly start Meeting Copilot.',
        'NSScreenCaptureUsageDescription': 'Transcribe system audio locally after you explicitly select system audio and start capture.',
    }))
    helper = core / 'macos_capture'
    run([swiftc, '-parse-as-library', str(core / 'native/macos_capture.swift'), '-O', '-o', str(helper),
         '-Xlinker', '-sectcreate', '-Xlinker', '__TEXT', '-Xlinker', '__info_plist', '-Xlinker', str(info)], check=True)
    run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', 'com.adam.meeting-copilot.capture', str(helper)], check=True)
    overlay = core / 'macos_overlay'
    run([swiftc, '-parse-as-library', str(core / 'native/macos_overlay.swift'), '-O', '-o', str(overlay)], check=True)
    return {'runtime': str(python), 'helper': str(helper), 'overlay': str(overlay), 'capture_started': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', default=os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    args = parser.parse_args()
    import json
    print(json.dumps(prepare(args.home), indent=2))
