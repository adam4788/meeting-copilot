"""Read-only developer diagnostics. Never opens a stream or changes readiness.

Run with the gateway's Python. --devices imports SoundCard and lists endpoints;
without that flag only package metadata is inspected. No models are loaded.
"""
import argparse
import importlib
import importlib.util
import json
import sys


def diagnose(*, devices=False, platform=None, find_spec=None, load_backend=None):
    platform = sys.platform if platform is None else platform
    find_spec = importlib.util.find_spec if find_spec is None else find_spec
    result = {'platform': platform, 'capture_verified': False,
              'stream_opened': False, 'dependencies': {}, 'errors': []}
    for name in ('numpy', 'soundcard', 'faster_whisper'):
        try:
            result['dependencies'][name] = find_spec(name) is not None
        except (ImportError, ValueError):
            result['dependencies'][name] = False
    if not devices:
        return result
    if platform != 'win32':
        result['errors'].append('Endpoint inspection requires Windows.')
        return result
    if not result['dependencies']['soundcard']:
        result['errors'].append('SoundCard is unavailable in this interpreter. Use the managed dependency resolver.')
        return result
    try:
        backend = load_backend() if load_backend else importlib.import_module('soundcard')
        # Enumerate only. Do not call recorder(), record(), or change defaults.
        inputs = backend.all_microphones(include_loopback=False)
        outputs = backend.all_speakers()
        mic, speaker = backend.default_microphone(), backend.default_speaker()
        result['endpoints'] = {
            'microphones': [{'name': d.name, 'default': mic is not None and d.id == mic.id} for d in inputs],
            'outputs': [{'name': d.name, 'default': speaker is not None and d.id == speaker.id} for d in outputs],
        }
        if mic is None or speaker is None:
            result['errors'].append('A default microphone or output is missing.')
    except Exception as error:
        # Native errors may include endpoint IDs/paths. Keep shareable output minimal.
        result['errors'].append('Endpoint inspection failed: ' + type(error).__name__)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--devices', action='store_true', help='List device names locally, without recording.')
    args = parser.parse_args()
    print(json.dumps(diagnose(devices=args.devices), indent=2))
