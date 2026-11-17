"""Windows WASAPI initialization probe: never starts streams or reads samples.

SoundCard 0.4.6's recorder constructor calls IAudioClient.Initialize, whereas
its context manager __enter__ calls Start. Keep the audited version exact:
unknown versions are not permission/device evidence. Run in a disposable,
credential-sanitized subprocess; never import the backend in the host gateway.
"""
import argparse
import importlib.metadata
import json
import sys

AUDITED_SOUNDCARD = '0.4.6'
SOURCES = ('microphone', 'system', 'both')


def probe_devices(source):
    result = {'version': 1, 'source': source, 'capture_started': False,
              'ready': False, 'endpoints': {}, 'error': None}
    if source not in SOURCES or sys.platform != 'win32':
        result['error'] = 'unsupported_platform_or_source'
        return result
    try:
        if importlib.metadata.version('SoundCard') != AUDITED_SOUNDCARD:
            result['error'] = 'audited_soundcard_required'
            return result
        import soundcard
        # This is a narrowly audited cleanup boundary, not a capture call.
        # No recorder context manager or read()/record()/Start() is invoked.
        from soundcard import mediafoundation
        for name, default, loopback in (
                ('microphone', 'default_microphone', False),
                ('system', 'default_speaker', True)):
            if source not in (name, 'both'):
                continue
            endpoint = getattr(soundcard, default)()
            if endpoint is None or not isinstance(endpoint.id, str) or not 1 <= len(endpoint.id) <= 1024:
                raise ValueError('default endpoint missing')
            selected = soundcard.get_microphone(id=endpoint.id, include_loopback=loopback)
            if (selected is None or selected.id != endpoint.id
                    or bool(selected.isloopback) != loopback):
                raise ValueError('exact endpoint unavailable')
            recorder = None
            try:
                # Same shared-mode, all-channel 16 kHz format as AudioCapture.
                # Initialize can reject invalid formats/devices or access denied.
                recorder = selected.recorder(samplerate=16000, blocksize=1600)
            finally:
                if recorder is not None:
                    mediafoundation._com.release(recorder._ptr)
            result['endpoints'][name] = {'id': endpoint.id, 'name': str(endpoint.name)[:200],
                                         'ready': True}
        result['ready'] = bool(result['endpoints'])
    except Exception:
        # Never leak OS exception text, arbitrary paths, or environment values.
        result['error'] = 'device_access_or_format_unavailable'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=SOURCES, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(probe_devices(args.source), ensure_ascii=True))


if __name__ == '__main__':
    main()
