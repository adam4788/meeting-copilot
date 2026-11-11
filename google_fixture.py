"""Explicit host-scoped synthetic WAV test; no device, file path or meeting mutation.

This bounded developer endpoint is separate from pane capture. The authenticated
caller supplies consented WAV bytes and receives results in memory. Disconnecting
an HTTP client cannot recall already transmitted bytes; the worker has hard limits.
"""
import base64
import importlib
import json
from pathlib import Path
import subprocess
import threading

MAX_WAV_BYTES = 1_920_044  # 60 seconds PCM16 mono 16 kHz + canonical WAV header
MAX_ENCODED_BYTES = 2_600_000
MAX_RESULT_BYTES = 262144


def _module(name):
    return importlib.import_module('.' + name, __package__) if __package__ else importlib.import_module(name)


class FixtureOperation:
    is_google_fixture = True

    def __init__(self):
        self.process = None
        self.payload = None
        self.reservation = None
        self._stop_lock = threading.Lock()

    def start(self, encoded):
        preview, adapter, asr = (_module(name) for name in ('google_preview', 'macos_audio', 'google_asr'))
        preview._require_scope()
        if not isinstance(encoded, str) or not 0 < len(encoded) <= MAX_ENCODED_BYTES:
            raise ValueError('Supply a PCM16 mono 16 kHz WAV of at most 60 seconds.')
        try:
            audio = base64.b64decode(encoded, validate=True)
            if len(audio) > MAX_WAV_BYTES:
                raise ValueError()
            asr.pcm_from_wav(audio)
        except Exception:
            raise ValueError('Supply a valid PCM16 mono 16 kHz WAV of at most 60 seconds.') from None
        frame = adapter._meta_worker_frame(preview.scoped_key(), 'google')
        root = Path(__file__).resolve().parent
        python = adapter.worker_python(root)
        # No reservation is consumed for malformed input/missing auth/runtime. Every
        # admitted attempt is charged locally before starting the isolated worker.
        reservation = preview.reserve('microphone')
        self.process = subprocess.Popen([python, '-B', str(root / 'audio_worker.py'),
            '--provider', 'google', '--asr-mode', 'ENDPOINTING', '--google-fixture-stdin'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=adapter.worker_env('google'), start_new_session=True)
        self.payload = frame + json.dumps({'audio_base64': encoded}).encode('ascii') + b'\n'
        self.reservation = reservation

    @property
    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def status(self):
        return {'running': self.is_running, 'stop_confirmed': not self.is_running}

    def stop(self):
        with self._stop_lock:
            # This mode never creates native/helper children. An exited worker
            # needs no group signal; never target a recycled process-group ID.
            if self.process is not None and self.process.poll() is None:
                _module('macos_audio').terminate_group(self.process, timeout=3)

    def wait(self):
        adapter, asr = _module('macos_audio'), _module('google_asr')
        try:
            output, _ = self.process.communicate(input=self.payload, timeout=85)
            if len(output) > MAX_RESULT_BYTES:
                raise ValueError()
            segments, usage, error = [], {}, None
            characters = 0
            for line in output.splitlines():
                if not line:
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError()
                if item.get('type') == 'segment':
                    text = item.get('text')
                    if not isinstance(text, str) or not text.strip() or item.get('speaker') != 'you':
                        raise ValueError()
                    characters += len(text)
                    if characters > asr.MAX_OUTPUT_CHARACTERS or len(segments) >= asr.MAX_FINAL_EVENTS:
                        raise ValueError()
                    segments.append({'speaker': 'you', 'text': text})
                elif item.get('type') == 'google_usage':
                    usage = adapter.clean_google_usage({'microphone': item.get('usage')}).get('microphone', {})
                elif item.get('type') == 'error':
                    error = 'Google transcription was not completed. Partial results may be incomplete.'
            if self.process.returncode != 0:
                error = 'Google transcription was not completed. Partial results may be incomplete.'
            return {'provider': 'google', 'model': asr.DEFAULT_MODEL, 'segments': segments,
                    'usage': usage, 'reservation': self.reservation, 'error': error,
                    'tail_confirmed': False, 'capture_started': False,
                    'warning': 'Final events received during a bounded drain are preserved; Google does not provide a documented end-of-audio acknowledgement. The last words may be incomplete.'}
        except Exception:
            raise RuntimeError('Google fixture test did not finish safely. Its reservation remains used; review usage before retrying.') from None
        finally:
            self.payload = None
            self.stop()


def run(encoded):
    operation = FixtureOperation()
    operation.start(encoded)
    return operation.wait()
