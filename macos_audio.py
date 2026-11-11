"""Opt-in macOS capture gateway. Standard library only; no Meetily dependency."""
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
    'model': 'Prepared local model could not be loaded. Prepare the selected model separately, then recheck required settings. Live capture never downloads missing weights.',
    'runtime': 'Audio runtime unavailable. Prepare the isolated Python 3.12 environment.',
    'helper': 'Native macOS audio helper unavailable. Compile macos_capture first.',
    'capture': 'Native audio capture failed. Check the selected source and macOS permissions.',
    'protocol': 'Invalid or oversized audio worker frame.',
    'worker': 'Audio worker exited unexpectedly.',
    'google_auth': 'Google transcription requires its own key in Hermes plugin secrets. Account access remains unverified.',
    'google_runtime': 'Google transcription requires websockets >=15,<16 in the isolated audio runtime.',
    'google_limit': 'Google preview limit reached. Capture stopped; review transcription and local preview allowance before restarting.',
    'google_transcription': 'Google transcription failed or its final tail is unconfirmed. Check connection, account access and preview limits; no automatic retry occurs.',
    'asr_auth': 'Meta cloud transcription is unavailable. Set the Meta API key in Hermes plugin settings and reopen the pane.',
    'asr': 'Meta cloud transcription failed. Check the network connection and MODEL_API_KEY, then restart capture.',
}


def error_message(code):
    return ERRORS.get(code, ERRORS['worker'])


def _native_runtime():
    if __package__:
        from . import native_runtime
    else:
        import native_runtime
    return native_runtime


def capture_helper(root, artifacts=None):
    return (artifacts if artifacts is not None else _native_runtime().artifact_dir(root)) / 'macos_capture'


def worker_python(root, artifacts=None):
    root = Path(root)
    override = os.environ.get('MEETING_COPILOT_AUDIO_PYTHON')
    if override is not None:
        candidates = [Path(override)]
    elif _native_runtime().is_catalog(root):
        artifacts = artifacts if artifacts is not None else _native_runtime().artifact_dir(root)
        candidates = [artifacts / 'audio-runtime/bin/python']
    else:
        candidates = [root / 'audio-runtime/bin/python', root / '.venv-audio/bin/python']
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
PROVIDERS = ('local', 'meta', 'google')
ASR_MODES = ('ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK')


def validate(model, source, language, provider='local', asr_mode='ENDPOINTING'):
    if model not in MODELS:
        raise ValueError('model must be tiny.en, base.en, or small.en')
    if source not in SOURCES:
        raise ValueError('source must be microphone, system, or both')
    if language != 'en':
        raise ValueError('English-only models require language=en')
    if provider not in PROVIDERS:
        raise ValueError('provider must be local, meta, or google')
    if provider == 'google' and asr_mode != 'ENDPOINTING':
        raise ValueError('Google live transcription supports source labels only; select ENDPOINTING explicitly.')
    if asr_mode not in ASR_MODES:
        raise ValueError('asr_mode must be ENDPOINTING, DIARIZATION, or PUSH_TO_TALK')


def _model_api_key():
    try:
        if __package__:
            from . import meta_models
        else:
            import meta_models
        return meta_models.resolve_model_api_key()
    except Exception:
        return ''  # never bypass a failed profile-scope resolver


def clean_writer_timing(value):
    """Allowlisted, finite timing-only diagnostics; never reflect helper prose."""
    if not isinstance(value, dict):
        return {}
    return {k: float(value[k]) for k in ('max_queue_ms', 'max_encode_ms', 'max_write_ms')
            if isinstance(value.get(k), (int, float)) and not isinstance(value[k], bool)
            and 0 <= value[k] <= 60000 and math.isfinite(value[k])}


def clean_audio_diagnostics(value, source=None):
    """Strict content-free numeric schema; missing data is not silence."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in ('microphone', 'system'):
        if source in ('microphone', 'system') and name != source:
            continue
        row = value.get(name)
        if not isinstance(row, dict):
            continue
        clean = {}
        for key in ('received_samples', 'submitted', 'completed', 'empty'):
            number = row.get(key)
            if type(number) is int and 0 <= number <= 10**12:
                clean[key] = number
        for key in ('peak', 'rms'):
            number = row.get(key)
            if type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 1:
                clean[key] = float(number)
        if clean:
            result[name] = clean
    return result


def clean_google_usage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for source in ('microphone', 'system'):
        row = value.get(source)
        if not isinstance(row, dict):
            continue
        clean = {}
        for key in ('audio_bytes', 'final_segments', 'output_characters', 'input_tokens', 'output_tokens', 'total_tokens'):
            number = row.get(key)
            if type(number) is int and 0 <= number <= 2000000:
                clean[key] = number
        seconds = row.get('audio_seconds')
        if type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= 60:
            clean['audio_seconds'] = float(seconds)
        if clean:
            result[source] = clean
    return result


def worker_env(provider='local'):
    """Pass only named OS/locale and public-model cache settings to workers.

    Never inherit host credentials or Python/dynamic-loader configuration.
    Cloud credentials use the separate, request-scoped stdin handoff. Keep model
    settings explicit: HF_HUB_* can contain credentials and is not a safe prefix.
    """
    allowed = {
        'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG',
        'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'LC_NUMERIC', 'LC_TIME',
        'LC_COLLATE', 'LC_MONETARY', 'LC_PAPER', 'LC_NAME', 'LC_ADDRESS',
        'LC_TELEPHONE', 'LC_MEASUREMENT', 'LC_IDENTIFICATION',
        '__CF_USER_TEXT_ENCODING',
    }
    if provider == 'local':
        allowed.update({'HF_HOME', 'HF_HUB_CACHE', 'HF_HUB_OFFLINE'})
    env = {key: value for key, value in os.environ.items() if key in allowed}
    # Public Whisper weights need no account token, including cached logins.
    env['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    return env


def _meta_worker_frame(key, provider='meta'):
    """Bounded one-shot private handoff of a host-scoped key to the worker."""
    if provider not in ('meta', 'google'):
        raise RuntimeError('Unsupported cloud provider.')
    if not isinstance(key, str) or not key.strip() or '\n' in key or '\r' in key:
        raise RuntimeError(f'{provider.title()} transcription needs a profile-scoped Hermes key.')
    frame = json.dumps({'type': 'scoped_' + provider + '_key', 'key': key}, ensure_ascii=True).encode('ascii') + b'\n'
    if len(frame) > 4096:
        raise RuntimeError(f'{provider.title()} transcription needs a valid profile-scoped Hermes key.')
    return frame


class MacOSAudioCapture:
    def __init__(self, model_size='base.en', source='microphone', language='en',
                 provider='local', asr_mode='ENDPOINTING'):
        validate(model_size, source, language, provider, asr_mode)
        self.model_size, self.source, self.language = model_size, source, language
        self.provider, self.asr_mode = provider, asr_mode
        self._lock = threading.RLock()
        self._termination_lock = threading.Lock()
        self._cleanup_confirmed = True
        self._state, self._error = 'idle', None
        self._callback_error = None
        self._process = None
        self._thread = None
        self._cancel = threading.Event()
        self._stopping = threading.Event()

    def devices(self):
        """Static capabilities only. Does not probe TCC or instantiate AV/SC APIs."""
        root = Path(__file__).resolve().parent
        try:
            helper_available = capture_helper(root).is_file()
        except (OSError, ValueError, TypeError, ImportError):
            helper_available = False
        return {'backend': 'macos-native', 'sources': list(SOURCES),
                'default_source': 'microphone', 'models': list(MODELS),
                'providers': list(PROVIDERS), 'asr_modes': list(ASR_MODES),
                'meta_asr_available': bool(_model_api_key()),
                'permissions_checked': False,
                'helper_available': helper_available,
                'permission_requirements': {'microphone': 'Microphone',
                                            'system': 'Screen & System Audio Recording'}}

    def status(self):
        with self._lock:
            running = bool((self._thread and self._thread.is_alive())
                           or (self._process and self._process.poll() is None))
            return {'state': self._state, 'running': running,
                    'stop_confirmed': self._cleanup_confirmed and not running,
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
        """Explicit opt-in: live local capture requires prepared model weights.

        Callbacks execute on the reader thread; they should return promptly.
        """
        if not callable(on_segment) or (on_status is not None and not callable(on_status)):
            raise TypeError('on_segment and on_status must be callable')
        credential_frame = _meta_worker_frame(_model_api_key()) if self.provider == 'meta' else None
        if self.provider == 'google':
            import importlib
            preview = importlib.import_module('.google_preview', __package__) if __package__ else importlib.import_module('google_preview')
            credential_frame = _meta_worker_frame(preview.scoped_key(), 'google')
        # Resolve the active profile on the requesting thread. The worker thread
        # must not inherit unrelated request/credential context variables.
        root = Path(__file__).resolve().parent
        artifacts = _native_runtime().artifact_dir(root)
        with self._lock:
            if not self.status()['stop_confirmed']:
                raise RuntimeError('Audio capture is active or its cleanup is unconfirmed')
            if self.provider == 'google':
                preview.reserve(self.source)  # Before worker/socket; uncertain attempts remain reserved.
            self._cleanup_confirmed = False
            self._cancel = threading.Event()
            self._stopping = threading.Event()
            self._on_segment = on_segment
            self._on_status = on_status or (lambda value: None)
            self._state, self._error, self._callback_error = 'starting', None, None
            self._last_loss = {}
            self._process = None
            environment = worker_env(self.provider)
            if self.provider == 'local':
                environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
            self._thread = threading.Thread(target=self._run, args=(environment, credential_frame, artifacts),
                                            daemon=True, name='macos-audio-worker')
            self._thread.start()

    def _diagnostics_update(self, event):
        diagnostics = clean_audio_diagnostics(event.get('audio_diagnostics'), self.source)
        if not diagnostics:
            return
        with self._lock:
            self._last_loss = {**getattr(self, '_last_loss', {}), 'audio_diagnostics': diagnostics}
        # No lifecycle snapshot: stop may change state before callback delivery.
        self._callback(self._on_status, {'type': 'diagnostics', 'audio_diagnostics': diagnostics})

    def _run(self, environment, credential_frame=None, artifacts=None):
        process = None
        try:
            root = Path(__file__).resolve().parent
            command = [worker_python(root, artifacts), '-u', str(root / 'audio_worker.py'),
                       '--helper', str(capture_helper(root, artifacts)),
                       '--model', self.model_size, '--source', self.source, '--language', self.language,
                       '--provider', self.provider, '--asr-mode', self.asr_mode]
            if self._cancel.is_set():
                return
            process = subprocess.Popen(command, stdin=subprocess.PIPE if credential_frame else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       env=environment)
            with self._lock:
                self._process = process
            if credential_frame:
                try:
                    process.stdin.write(credential_frame)
                    process.stdin.flush()
                finally:
                    process.stdin.close()
                    credential_frame = None
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
                if kind == 'diagnostics':
                    self._diagnostics_update(event)
                    continue
                if kind == 'status' and event.get('state') in ('loading', 'starting', 'running', 'stopped'):
                    extra = {k: event.get(k) for k in ('warning', 'dropped_chunks', 'native_dropped_chunks',
                                                       'worker_dropped_chunks', 'sub_second_dropped_chunks',
                                                       'reader_incomplete', 'partial_tail', 'google_usage')
                             if event.get(k) is not None}
                    if 'google_usage' in extra:
                        extra['google_usage'] = clean_google_usage(extra['google_usage'])
                    diagnostics = clean_audio_diagnostics(event.get('audio_diagnostics'), self.source)
                    if diagnostics:
                        extra['audio_diagnostics'] = diagnostics
                    timing = clean_writer_timing(event.get('native_writer_timing_ms'))
                    if timing:
                        extra['native_writer_timing_ms'] = timing
                    self._notify(event['state'], None)
                    if extra:
                        with self._lock:
                            self._last_loss = {**getattr(self, '_last_loss', {}), **extra}
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
                    self._terminate(process, .2)
                except Exception:
                    self._notify('error', 'worker')
                process.stdout.close()
            else:
                with self._lock:
                    self._cleanup_confirmed = True
            with self._lock:
                if self._cancel.is_set() and self._state != 'error':
                    self._state = 'stopped'

    def _terminate(self, process, timeout):
        # Reader cleanup and explicit stop may overlap. Serialize signals,
        # never callbacks or joins; a failed cleanup retains the exact handle.
        with self._termination_lock:
            with self._lock:
                if self._cleanup_confirmed and process.poll() is not None:
                    return  # do not signal a reaped/reusable process-group ID
                self._cleanup_confirmed = False
            terminate_group(process, timeout)
            with self._lock:
                self._cleanup_confirmed = True

    def stop(self, timeout=None):
        """Bounded graceful shutdown: SIGTERM first so the worker flushes its
        tail and delivers a final summary, then force-terminate leftovers.

        Already executing user callbacks cannot be forcibly interrupted.
        """
        if timeout is None:
            timeout = 9.5 if self.provider == 'google' else 3.0
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
            self._terminate(process, min(.5, timeout))
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
