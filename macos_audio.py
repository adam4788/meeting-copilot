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
    'asr_auth': 'Meta cloud transcription is not configured: set MODEL_API_KEY, then prepare macOS capture again.',
    'asr': 'Meta cloud transcription failed. Check the network connection and MODEL_API_KEY, then restart capture.',
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


# SIGKILL does not exist on Windows; fall back to a second terminate there.
_SIGKILL = getattr(signal, 'SIGKILL', signal.SIGTERM)


def _signal_group(process, sig):
    """Signal the whole process group on POSIX; the process itself on Windows."""
    if os.name == 'posix':
        os.killpg(process.pid, sig)
    elif sig is _SIGKILL and _SIGKILL is not signal.SIGTERM:
        process.kill()
    else:
        process.terminate()


def terminate_group(process, timeout=1.0):
    """Always signal the group, even when the worker has exited leaving a helper."""
    try:
        _signal_group(process, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    try:
        process.wait(timeout=max(0, timeout))
    except subprocess.TimeoutExpired:
        pass
    # A child may ignore TERM even if its parent already exited.
    try:
        _signal_group(process, _SIGKILL)
    except ProcessLookupError:
        pass  # the group has already exited; other signal failures are unsafe
    try:
        process.wait(timeout=.2)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Audio worker termination is not confirmed') from exc
from pathlib import Path
import threading

import re as _re

_SPEAKER_LABEL = _re.compile(r'^(you|remote)( \u00b7 Speaker [A-Za-z0-9_-]{1,12})?$|^Speaker [A-Za-z0-9_-]{1,12}$')


def _valid_speaker(value):
    """Worker speaker identities: a source label ('you'/'remote'),
    optionally carrying a bounded diarization suffix, or a bare bounded
    'Speaker X' label. Anything else is a protocol violation."""
    return isinstance(value, str) and bool(_SPEAKER_LABEL.match(value))


MODELS = ('tiny.en', 'base.en', 'small.en')
SOURCES = ('microphone', 'system', 'both')
PROVIDERS = ('local', 'meta')
ASR_MODES = ('ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK')


def validate(model, source, language, provider='local', asr_mode='ENDPOINTING'):
    if model not in MODELS:
        raise ValueError('model must be tiny.en, base.en, or small.en')
    if source not in SOURCES:
        raise ValueError('source must be microphone, system, or both')
    if language != 'en':
        raise ValueError('English-only models require language=en')
    if provider not in PROVIDERS:
        raise ValueError('provider must be local or meta')
    if asr_mode not in ASR_MODES:
        raise ValueError('asr_mode must be ENDPOINTING, DIARIZATION, or PUSH_TO_TALK')


def _model_api_key():
    try:
        import meta_models
        return meta_models.resolve_model_api_key()
    except Exception:
        return os.environ.get('MODEL_API_KEY', '')


def worker_env():
    """Hermetic env for the isolated worker: never inherit the launcher's
    PYTHONPATH/PYTHONHOME (e.g. Hermes's own 3.14 site-packages), which would
    shadow the audio-runtime's 3.12 packages and break model loading."""
    return {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}


class MacOSAudioCapture:
    def __init__(self, model_size='base.en', source='microphone', language='en',
                 provider='local', asr_mode='ENDPOINTING'):
        validate(model_size, source, language, provider, asr_mode)
        self.model_size, self.source, self.language = model_size, source, language
        self.provider, self.asr_mode = provider, asr_mode
        self._lock = threading.RLock()
        self._state, self._error = 'idle', None
        self._callback_error = None
        self._process = None
        self._thread = None
        self._cancel = threading.Event()
        self._stopping = threading.Event()

    def devices(self):
        """Static capabilities only. Does not probe TCC or instantiate AV/SC APIs."""
        root = Path(__file__).resolve().parent
        return {'backend': 'macos-native', 'sources': list(SOURCES),
                'default_source': 'microphone', 'models': list(MODELS),
                'providers': list(PROVIDERS), 'asr_modes': list(ASR_MODES),
                'meta_asr_available': bool(_model_api_key()),
                'permissions_checked': False,
                'helper_available': (root / 'macos_capture').is_file(),
                'permission_requirements': {'microphone': 'Microphone',
                                            'system': 'Screen & System Audio Recording'}}

    def status(self):
        with self._lock:
            return {'state': self._state, 'running': bool((self._thread and self._thread.is_alive())
                                                        or (self._process and self._process.poll() is None)),
                    'error': self._error, 'callback_error': self._callback_error,
                    'source': self.source, 'model': self.model_size,
                    'provider': getattr(self, 'provider', 'local'),
                    'asr_mode': getattr(self, 'asr_mode', 'ENDPOINTING')}

    def _notify(self, state, code=None):
        if self._cancel.is_set():
            return
        # During the graceful stop window the worker's expected EOF must not
        # surface as a capture error: teardown already owns the final state.
        # A real failure reported mid-teardown is still news, never hidden.
        if self._stopping.is_set():
            if state != 'error' or code in ('worker', None):
                return
        with self._lock:
            self._state, self._error = state, error_message(code) if code else None
        self._callback(self._on_status, self.status())

    def _callback(self, callback, value):
        if self._cancel.is_set():
            # Terminal capture states are always delivered: final loss
            # counts and errors must reach the pane even during teardown.
            state = value.get('state') if isinstance(value, dict) else None
            if state not in ('stopped', 'stopping', 'error'):
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
            self._stopping = threading.Event()
            self._on_segment = on_segment
            self._on_status = on_status or (lambda value: None)
            self._state, self._error, self._callback_error = 'starting', None, None
            self._last_loss = {}
            self._process = None
            self._thread = threading.Thread(target=self._run, daemon=True, name='macos-audio-worker')
            self._thread.start()

    def _run(self):
        process = None
        try:
            root = Path(__file__).resolve().parent
            command = [worker_python(root), '-u', str(root / 'audio_worker.py'),
                       '--model', self.model_size, '--source', self.source, '--language', self.language,
                       '--provider', self.provider, '--asr-mode', self.asr_mode]
            if self._cancel.is_set():
                return
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       env=worker_env())
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
                if kind == 'status' and event.get('state') in ('loading', 'starting', 'running', 'stopped'):
                    extra = {k: event.get(k) for k in ('warning', 'dropped_chunks', 'native_dropped_chunks',
                                                       'worker_dropped_chunks', 'sub_second_dropped_chunks',
                                                       'reader_incomplete', 'partial_tail')
                             if event.get(k) is not None}
                    self._notify(event['state'], None)
                    if extra:
                        with self._lock:
                            self._last_loss = dict(extra)
                        try:
                            merged = self.status()
                            merged.update(extra)
                            self._callback(self._on_status, merged)
                        except Exception:
                            pass
                elif kind == 'segment':
                    text, stamp = event.get('text'), event.get('timestamp')
                    if (not isinstance(text, str) or len(text) > 16000 or
                            not _valid_speaker(event.get('speaker')) or
                            not isinstance(stamp, (float, int)) or not math.isfinite(stamp)):
                        raise ValueError('protocol')
                    segment = {'speaker': event['speaker'],
                               'text': text, 'timestamp': float(stamp)}
                    if event.get('partial_tail') is True:
                        segment['partial_tail'] = True
                    self._callback(self._on_segment, segment)
        except RuntimeError:
            self._notify('error', 'runtime')
        except (ValueError, UnicodeError, AttributeError):
            self._notify('error', 'protocol')
        except Exception:
            self._notify('error', 'worker')
        finally:
            if process:
                try:
                    terminate_group(process, .2)
                except Exception:
                    self._notify('error', 'worker')
                process.stdout.close()
            with self._lock:
                if self._cancel.is_set() and self._state != 'error':
                    self._state = 'stopped'

    def stop(self, timeout=3.0):
        """Bounded graceful shutdown: SIGTERM first so the worker flushes its
        tail and delivers a final summary, then force-terminate leftovers.

        Already executing user callbacks cannot be forcibly interrupted.
        """
        timeout = max(0.0, min(float(timeout), 10.0))
        deadline = time.monotonic() + timeout
        self._stopping.set()
        force_needed = False
        with self._lock:
            if self._state != 'error':
                self._state = 'stopping'
            process, thread = self._process, self._thread
        if process:
            # Graceful window: the worker stops its helper, flushes trailing
            # audio, and exits on its own. Callbacks stay live so the final
            # summary and tail segments reach the pane.
            try:
                process.terminate()
            except Exception:
                pass
            # Honor the caller's stop budget (reserving 0.5s for forced
            # teardown): slow inference or queued chunks can need more than
            # a fixed one-second window to flush the trailing audio.
            graceful = max(0.0, min(deadline - time.monotonic() - 0.5, timeout - 0.5))
            try:
                process.wait(timeout=graceful or 0.0)
            except Exception:
                pass
            # Whether force termination was actually needed — not reader
            # liveness, which can misreport a killed worker as clean.
            force_needed = process.poll() is None
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
            final_loss = dict(getattr(self, '_last_loss', None) or {})
            forced = force_needed or bool(thread and thread.is_alive())
        # Deliver the terminal state with the last known loss evidence: the
        # worker may have been terminated before its own summary arrived.
        # A worker still alive after the bounded join remains visibly stopping.
        # A forced shutdown is marked explicitly: trailing audio may be lost.
        if forced:
            prior = final_loss.get('warning') or ''
            final_loss['warning'] = (prior + ' Forced shutdown before the worker exited; trailing audio may be incomplete.').strip()
        try:
            final = self.status()
            final.update(final_loss)
            self._callback(self._on_status, final)
        except Exception:
            pass
