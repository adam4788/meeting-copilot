"""Opt-in macOS capture gateway. Standard library only; no Meetily dependency.

Install audio_worker.py and macos_capture alongside this module, and the isolated
Python 3.12 environment at audio-runtime (or project .venv-audio).
"""
import os
import json
import math
import signal
import subprocess
import time

MAX_EVENT = 65536
ERRORS = {
    'microphone_usage': 'Native helper lacks NSMicrophoneUsageDescription. Rebuild with an embedded usage-description Info.plist before starting microphone capture.',
    'microphone_permission': 'Microphone permission denied. Allow Microphone access in System Settings > Privacy & Security, then restart capture.',
    'system_permission': 'System audio permission denied or unavailable. Allow Screen & System Audio Recording in System Settings > Privacy & Security, then restart the app.',
    'model': 'Local model could not be loaded. Check the audio runtime and model cache/network availability; starting may download model weights.',
    'runtime': 'Audio runtime unavailable. Prepare the isolated Python 3.12 environment.',
    'helper': 'Native macOS audio helper unavailable. Compile macos_capture first.',
    'capture': 'Native audio capture failed. Check the selected source and macOS permissions.',
    'protocol': 'Invalid or oversized audio worker frame.',
    'worker': 'Audio worker exited unexpectedly.',
}


def error_message(code):
    return ERRORS.get(code, ERRORS['worker'])


def worker_python(root):
    override = os.environ.get('MEETING_COPILOT_AUDIO_PYTHON')
    candidates = [Path(override)] if override is not None else [
        root / 'audio-runtime/bin/python', root / '.venv-audio/bin/python']
    for path in candidates:
        if path.is_absolute() and path.is_file() and os.access(str(path), os.X_OK):
            return str(path)
    raise RuntimeError(ERRORS['runtime'])


def terminate_group(process, timeout=1.0):
    """Always signal the group, even when the worker has exited leaving a helper."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=max(0, timeout))
    except subprocess.TimeoutExpired:
        pass
    # A child may ignore TERM even if its parent already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=.2)
    except subprocess.TimeoutExpired:
        pass
from pathlib import Path
import threading

MODELS = ('tiny.en', 'base.en', 'small.en')
SOURCES = ('microphone', 'system', 'both')


def validate(model, source, language):
    if model not in MODELS:
        raise ValueError('model must be tiny.en, base.en, or small.en')
    if source not in SOURCES:
        raise ValueError('source must be microphone, system, or both')
    if language != 'en':
        raise ValueError('English-only models require language=en')


class MacOSAudioCapture:
    def __init__(self, model_size='base.en', source='microphone', language='en'):
        validate(model_size, source, language)
        self.model_size, self.source, self.language = model_size, source, language
        self._lock = threading.RLock()
        self._state, self._error = 'idle', None
        self._callback_error = None
        self._process = None
        self._thread = None
        self._cancel = threading.Event()

    def devices(self):
        """Static capabilities only. Does not probe TCC or instantiate AV/SC APIs."""
        root = Path(__file__).resolve().parent
        return {'backend': 'macos-native', 'sources': list(SOURCES),
                'default_source': 'microphone', 'models': list(MODELS),
                'permissions_checked': False,
                'helper_available': (root / 'macos_capture').is_file(),
                'permission_requirements': {'microphone': 'Microphone',
                                            'system': 'Screen & System Audio Recording'}}

    def status(self):
        with self._lock:
            return {'state': self._state, 'running': bool(self._thread and self._thread.is_alive()),
                    'error': self._error, 'callback_error': self._callback_error,
                    'source': self.source, 'model': self.model_size}

    def _notify(self, state, code=None):
        if self._cancel.is_set():
            return
        with self._lock:
            self._state, self._error = state, error_message(code) if code else None
        self._callback(self._on_status, self.status())

    def _callback(self, callback, value):
        if self._cancel.is_set():
            return
        try:
            callback(value)
        except Exception:
            self._callback_error = 'Audio callback failed.'

    def start(self, on_segment, on_status=None):
        """Explicit opt-in: worker may download allowlisted model weights.

        Callbacks execute on the reader thread; they should return promptly.
        """
        if not callable(on_segment) or (on_status is not None and not callable(on_status)):
            raise TypeError('on_segment and on_status must be callable')
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError('Audio capture is already active or stopping')
            self._cancel = threading.Event()
            self._on_segment = on_segment
            self._on_status = on_status or (lambda value: None)
            self._state, self._error, self._callback_error = 'starting', None, None
            self._process = None
            self._thread = threading.Thread(target=self._run, daemon=True, name='macos-audio-worker')
            self._thread.start()

    def _run(self):
        process = None
        try:
            root = Path(__file__).resolve().parent
            command = [worker_python(root), '-u', str(root / 'audio_worker.py'),
                       '--model', self.model_size, '--source', self.source, '--language', self.language]
            if self._cancel.is_set():
                return
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            with self._lock:
                self._process = process
            assert process.stdout is not None
            while not self._cancel.is_set():
                line = process.stdout.readline(MAX_EVENT + 1)
                if not line:
                    if self._state != 'error':
                        self._notify('error', 'worker')
                    break
                if len(line) > MAX_EVENT or not line.endswith(b'\n'):
                    self._notify('error', 'protocol')
                    break
                event = json.loads(line)
                kind = event.get('type')
                if kind == 'error':
                    self._notify('error', event.get('code'))
                    break
                if kind == 'status' and event.get('state') in ('loading', 'starting', 'running'):
                    self._notify(event['state'])
                elif kind == 'segment':
                    text, stamp = event.get('text'), event.get('timestamp')
                    if (not isinstance(text, str) or len(text) > 16000 or
                            event.get('speaker') not in ('you', 'remote') or
                            not isinstance(stamp, (float, int)) or not math.isfinite(stamp)):
                        raise ValueError('protocol')
                    self._callback(self._on_segment, {'speaker': event['speaker'],
                                                     'text': text, 'timestamp': float(stamp)})
        except RuntimeError:
            self._notify('error', 'runtime')
        except (ValueError, UnicodeError, AttributeError):
            self._notify('error', 'protocol')
        except Exception:
            self._notify('error', 'worker')
        finally:
            if process:
                terminate_group(process, .2)
                process.stdout.close()
            with self._lock:
                if self._cancel.is_set() and self._state != 'error':
                    self._state = 'stopped'

    def stop(self, timeout=3.0):
        """Cancel callbacks immediately and terminate worker plus native descendants.

        Already executing user callbacks cannot be forcibly interrupted.
        """
        timeout = max(0.0, min(float(timeout), 10.0))
        deadline = time.monotonic() + timeout
        self._cancel.set()
        with self._lock:
            process, thread = self._process, self._thread
        if process:
            terminate_group(process, min(.5, timeout))
        if thread and thread is not threading.current_thread():
            thread.join(max(0, deadline - time.monotonic()))
        with self._lock:
            if self._state != 'error':
                self._state = 'stopping' if thread and thread.is_alive() else 'stopped'
