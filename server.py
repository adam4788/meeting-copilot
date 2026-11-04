"""Read-only meeting copilot backend. Importing never starts audio capture."""
import threading
import time
from contextlib import contextmanager, nullcontext
from collections import deque
from typing import Literal
from fastapi import APIRouter
from pydantic import BaseModel, Field


def _is_engine_abort(exc):
    """True when the engine raised its cooperative-cancellation signal."""
    try:
        from knowledge import _Aborted
    except ImportError:
        _Aborted = None
    if _Aborted is not None and isinstance(exc, _Aborted):
        return True
    # The deployed dashboard loads the engine under a namespaced package, so
    # a packaged _Aborted is a different class object: match by signal name.
    # _Aborted is engine-private (never raised by providers or transports).
    return type(exc).__name__ == '_Aborted'


class AssistRequest(BaseModel):
    question: str = Field(default='', max_length=4000)
    request_id: str | None = Field(default=None, max_length=100)
    row_id: str | None = Field(default=None, max_length=200)
    project: str | None = Field(default=None, max_length=1000)
    all_projects: bool = False
    web_fallback: bool = False
    web_query: str | None = Field(default=None, max_length=300)
    answer_provider: str | None = Field(default=None, max_length=100)
    answer_model: str | None = Field(default=None, max_length=200)
    # Exact-replay retry: a stored transcript snapshot instead of current segments.
    transcript: str | None = Field(default=None, max_length=16000)


class AssistCancelRequest(BaseModel):
    request_id: str = Field(max_length=100)


class OverlayScopeRequest(BaseModel):
    scope: dict = Field(default_factory=dict)


class OverlayClaimRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    revision: int = Field(ge=0, strict=True)
    client: str | None = Field(default=None, min_length=1, max_length=100)
    creation: int | None = Field(default=None, ge=1, le=9007199254740991, strict=True)
    epoch: str | None = Field(default=None, min_length=1, max_length=100)


class OverlayActionRequest(OverlayScopeRequest):
    pane: str = Field(min_length=1, max_length=100)
    action: int = Field(ge=1, strict=True)


class LeaseRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    pane: str | None = Field(default=None, min_length=1, max_length=100)
    revision: int | None = Field(default=None, ge=0, strict=True)
    epoch: str | None = Field(default=None, min_length=1, max_length=100)


class LifecycleControlRequest(BaseModel):
    # No ignored/typoed authorization may fall back to legacy admission.
    model_config = {'extra': 'forbid'}
    pane: str | None = Field(default=None, min_length=1, max_length=100)
    action: int | None = Field(default=None, ge=1, strict=True)
    revision: int | None = Field(default=None, ge=0, strict=True)
    epoch: str | None = Field(default=None, min_length=1, max_length=100)


class CaptureRequest(LifecycleControlRequest):
    consent: bool = False
    backend: Literal['meetily', 'native', 'macos'] = 'meetily'
    recording_id: str | None = Field(default=None, max_length=500)
    transcription_model: Literal['tiny.en', 'base.en', 'small.en'] = 'base.en'
    audio_source: Literal['microphone', 'system', 'both'] = 'microphone'
    transcription_provider: Literal['local', 'meta'] = 'local'
    asr_mode: Literal['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK'] = 'ENDPOINTING'
    new_meeting: bool = False


def _meta_key_present():
    try:
        import importlib
        module = importlib.import_module(f'{__package__}.meta_models' if __package__ else 'meta_models')
        return module.api_key_present()
    except Exception:
        import os
        return bool(os.environ.get('MODEL_API_KEY'))


class MeetingState:
    def __init__(self, engine_factory=None, audio_factory=None, bridge_factory=None, macos_factory=None):
        self.macos_factory = macos_factory
        self.audio_options = None
        self.bridge_factory = bridge_factory
        self.bridge = None
        self.backend = None
        self.session_key = None
        self.audio_factory = audio_factory
        self.audio = None
        self.capture_info = {}
        self.last_seen = time.monotonic()
        self.pane_seen = self.last_seen
        self.engine_factory = engine_factory
        self.engine = None
        self.busy = False
        self.result = None
        self.requests = {}
        self.request_seq = 0
        self.live_workers = 0
        self.generation = 0
        self.capture_generation = 0
        self._status_generation = 0
        self.status_seq = 0
        self.retiring_generations = {}
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.overlay_lock = threading.RLock()
        self.overlay_lease_seq = 0
        self.capture_error = None
        # Logical retirement is not proof of physical shutdown. Keep the exact
        # adapter until stop succeeds; starts cannot replace it meanwhile.
        self.pending_stop = None
        self.stop_sequence = 0
        self.stop_attempts = 0
        self.stop_error = None
        self.overlay_cleanup_error = None
        self.pending_overlay = None
        self._overlay_cleanup_job = None
        self._overlay_startup_job = None
        self._overlay_probe_lock = threading.Lock()
        self._overlay_probe_jobs = {}
        self._overlay_invalidation_error = None
        self.segments = deque(maxlen=120)
        self.listening = False
        self.error = None
        self.error_request = None
        # A failed Meetily forget-boundary must not look like a clean Clear:
        # the adapter keeps serving its old snapshot until the boundary is
        # installed, so the failure stays visible until a later Clear lands it.
        self.meetily_clear_error = None
        self.overlay = None
        self.overlay_scope_value = {}
        self.overlay_owner = None
        self.overlay_pane = None
        self.overlay_action = 0
        self.overlay_revision = 0
        import secrets
        self.overlay_epoch = secrets.token_hex(16)
        self.overlay_client = None
        self.overlay_creation = 0

    def add_segment(self, segment):
        text = str(segment.get('text', '')).strip()[:4000]
        if text:
            with self.lock:
                item = {'speaker': str(segment.get('speaker', 'remote'))[:30], 'text': text,
                        'timestamp': segment.get('timestamp', time.time())}
                if segment.get('partial_tail') is True:
                    item['partial_tail'] = True
                segment_id = segment.get('id')
                if segment_id is not None:
                    item['id'] = str(segment_id)[:600]
                    for i, existing in enumerate(self.segments):
                        if existing.get('id') == item['id']:
                            self.segments[i] = item
                            return
                self.segments.append(item)

    def get_bridge(self):
        with self.lock:
            if self.bridge is None:
                if self.bridge_factory:
                    self.bridge = self.bridge_factory()
                else:
                    import importlib
                    module = importlib.import_module(f'{__package__}.meetily' if __package__ else 'meetily')
                    self.bridge = module.MeetilyBridge()
            return self.bridge

    def get_audio(self, backend='native', transcription_model='base.en', audio_source='microphone',
                  transcription_provider='local', asr_mode='ENDPOINTING'):
        with self.lock:
            options = (backend, transcription_model, audio_source, transcription_provider, asr_mode)
            if self.audio is None or self.audio_options != options:
                if backend == 'macos':
                    if self.macos_factory:
                        factory = self.macos_factory
                    else:
                        import importlib
                        module = importlib.import_module(f'{__package__}.macos_audio' if __package__ else 'macos_audio')
                        factory = module.MacOSAudioCapture
                    self.audio = factory(model_size=transcription_model, source=audio_source,
                                         provider=transcription_provider, asr_mode=asr_mode)
                elif self.audio_factory:
                    self.audio = self.audio_factory()
                else:
                    import importlib
                    module = importlib.import_module(f'{__package__}.audio' if __package__ else 'audio')
                    self.audio = module.AudioCapture()
                self.audio_options = options
            return self.audio

    def start(self, consent, backend='meetily', recording_id=None, transcription_model='base.en', audio_source='microphone',
              new_meeting=False, transcription_provider='local', asr_mode='ENDPOINTING', *, pane=None, action=None):
        # Serialize the actual backend operation, not merely the UI state transition.
        # Status/segment callbacks take only self.lock, so stop/join cannot deadlock them.
        with self.lifecycle_lock:
            # Pane starts and cleanup share one capability and total order.
            # A delayed start arriving after Release must never resume capture.
            if pane is not None or action is not None:
                with self.overlay_lock:
                    if not self._accept_overlay_action_locked(pane, action):
                        from fastapi import HTTPException
                        raise HTTPException(409, 'Capture pane was closed or superseded.')
            return self._start(consent, backend, recording_id, transcription_model, audio_source, new_meeting,
                               transcription_provider, asr_mode)

    def _start(self, consent, backend='meetily', recording_id=None, transcription_model='base.en', audio_source='microphone',
               new_meeting=False, transcription_provider='local', asr_mode='ENDPOINTING', *, renew_lease=True):
        from fastapi import HTTPException
        if self.pending_stop is not None:
            raise HTTPException(409, self.stop_error or 'Capture stop is not confirmed. Retry Pause before starting.')
        ticket = self._reconcile_overlay_admission()
        if self.pending_overlay is not None or self.overlay_cleanup_error:
            raise HTTPException(409, self.overlay_cleanup_error or 'Overlay shutdown is not confirmed. Retry Hide / Clear.')
        if not consent:
            raise HTTPException(400, 'Confirm participant consent and audio capture before starting.')
        if backend == 'meetily' and not recording_id:
            raise HTTPException(400, 'Start a recording in Meetily, then explicitly select it here.')
        with self._overlay_admission_guard(ticket), self.lock:
            # Check under the same lock Clear uses to record the failure, and
            # before any state mutation: a concurrent plain start must block
            # until a paused Clear publishes its outcome instead of reading a
            # stale clean flag, and a rejected start must not flip listening
            # on or advance the generation (a retry would then early-return
            # as "already listening" without ever attaching).
            if self.pending_stop is not None:
                raise HTTPException(409, self.stop_error)
            if backend == 'meetily' and not new_meeting and self.meetily_clear_error is not None:
                raise HTTPException(409, self.meetily_clear_error)
            if self.listening:
                return {'listening': True}
            if backend in ('native', 'macos'):
                self.get_audio(backend, transcription_model, audio_source, transcription_provider, asr_mode)
            self.last_start_args = (backend, recording_id, transcription_model, audio_source,
                                    transcription_provider, asr_mode)
            session_key = (backend, recording_id if backend == 'meetily'
                           else (transcription_model, audio_source, transcription_provider, asr_mode))
            switching = (self.session_key is not None and self.session_key != session_key) or new_meeting
        # Retire before publishing the new session, with lock order
        # lifecycle -> overlay -> bridge -> state, never state -> bridge.
        boundary = self.overlay_clear_boundary() if switching else nullcontext()
        with boundary, self._overlay_admission_guard(ticket) if not switching else nullcontext(), self.lock:
            if self.pending_stop is not None:
                raise HTTPException(409, self.stop_error)
            # A new meeting never inherits the prior one: same-settings start
            # defaults to resume (keeps transcript, results, retry data), while
            # an explicit new_meeting — or a different session — clears them.
            if self.session_key != session_key or (new_meeting and not self.listening):
                self.segments.clear()
                self.result = None
                self.capture_info = {}
                # New meeting: no tail window from the previous session, and no
                # failed-request retry data from it either. Same-session resume
                # keeps its bounded retiring window.
                self.retiring_generations.clear()
                self.error_request = None
                # New meeting/session: retire every old slot immediately so no
                # old question, partial answer, or source stays readable via
                # /assist/<id>. Workers hold their own entry references and
                # still observe cancelled + generation to exit promptly.
                for e in self.requests.values():
                    e['cancelled'] = True
                self.requests.clear()
                self.generation += 1
                self.session_key = session_key
            self.backend = backend
            self.listening = True
            self.error = None
            self.error_request = None  # retry data belongs only to an active answer error
            # Reset capture-owned status before starting the new adapter, not
            # after its callback: old errors/messages/loss are not evidence
            # about this generation, even when resuming identical options.
            # New startup failures and pending-stop evidence must survive.
            # Meeting-level history stays in segments and recent answers.
            self.capture_info = {}
            self.capture_error = None
            self.capture_generation += 1
            # The newest capture owns the terminal state from birth: a late
            # terminal from an older capture can never present itself as
            # current, even before this capture emits its own terminal.
            self._status_generation = self.capture_generation
            capture_generation = self.capture_generation
            status_session_key = self.session_key
        # Lease refresh outside self.lock: _touch_pane_seen takes overlay_lock,
        # and overlay_lock -> self.lock ordering elsewhere forbids the reverse.
        if switching and self.overlay_cleanup_error is not None:
            self.pause(retain_tail=False)
            raise HTTPException(503, self.overlay_cleanup_error)
        if renew_lease:
            self._touch_pane_seen()
        elif self._expire_capture():
            return {'listening': False}
        def segment_callback(segment):
            with self.lock:
                if self.listening and self.capture_generation == capture_generation:
                    self.add_segment(segment)
                elif (capture_generation in self.retiring_generations
                        and time.monotonic() < self.retiring_generations[capture_generation]):
                    # Bounded tail of a retiring capture (pause flush): keep
                    # trailing speech instead of dropping the last seconds.
                    self.add_segment(segment)
        def status_callback(info):
            retry_sequence = None
            with self.lock:
                # Backends disagree on the terminal key: native/macOS audio
                # emits 'state', Meetily emits 'status'. A status-shaped
                # terminal error must be recognized, not mistaken for a
                # clean pause.
                state = (info.get('state') or info.get('status')) if isinstance(info, dict) else None
                same_session = self.capture_generation == capture_generation
                same_meeting = status_session_key == self.session_key
                if state in ('stopped', 'error') and (same_session or (not self.listening and same_meeting)):
                    # Terminal loss metadata must survive teardown: pause
                    # bumps the generation before the adapter delivers its
                    # final latched summary. A stale terminal never touches
                    # an actively listening newer session — or a newer
                    # meeting after a session switch. It also never rewinds
                    # a newer generation's terminal state: each capture keeps
                    # its own final loss evidence; only the newest stopped
                    # or errored capture may overwrite it.
                    if capture_generation >= self._status_generation:
                        self.audio_status(info)
                        self._status_generation = capture_generation
                elif self.listening and self.capture_generation == capture_generation:
                    self.audio_status(info)
                if (same_session and not self.listening and self.pending_stop is None
                        and isinstance(info, dict)
                        and (info.get('running') is True or info.get('stop_confirmed') is False)):
                    # Terminal status retires delivery, not the physical adapter.
                    # Publish the fence before another start can replace it. The
                    # reader must never join itself or wait on lifecycle_lock.
                    self.pending_stop = self.audio if backend in ('native', 'macos') else self.bridge
                    self.stop_sequence += 1
                    self.stop_attempts = 0
                    self.stop_error = 'Capture stop is not confirmed; stopping audio. Do not assume the microphone is off.'
                    self.retiring_generations.clear()
                    self.capture_generation += 1
                    retry_sequence = self.stop_sequence
            if retry_sequence is not None:
                self._start_stop_retry_locked(retry_sequence)
        try:
            with self._overlay_admission_guard(None if switching else ticket):
                if backend == 'meetily':
                    bridge = self.get_bridge()
                    if new_meeting:
                        forget = getattr(bridge, 'note_cleared', None)
                        if callable(forget):
                            forget(recording_id)
                        with self.lock:
                            self.meetily_clear_error = None
                    bridge.attach(recording_id, on_segment=segment_callback, on_status=status_callback)
                else:
                    # Native start must not clear a failed Meetily boundary.
                    self.audio.start(on_segment=segment_callback, on_status=status_callback)
        except HTTPException:
            self.pause(retain_tail=False)
            raise
        except Exception:
            self.pause()
            raise HTTPException(503, 'Audio could not start. Check audio permissions, the selected recording/provider and backend connection. Verify capture has stopped before retrying Start.') from None
        threading.Thread(target=self.watchdog, args=(capture_generation,), name='meeting-capture-lease', daemon=True).start()
        return {'listening': self.listening}

    def audio_status(self, info):
        with self.lock:
            incoming = info if isinstance(info, dict) else {'message': str(info)}
            if isinstance(incoming, dict) and 'state' not in incoming and isinstance(incoming.get('status'), str):
                incoming = dict(incoming, state=incoming['status'])
            previous = self.capture_info if isinstance(self.capture_info, dict) else {}
            # Audio-loss evidence latches: a later plain status must not clear
            # an earlier drop warning until pause, clear, or a new capture.
            # Drop counts are monotonic within a capture: a lower later report
            # (e.g. a worker-only update after a combined one) never rewinds.
            for key in ('warning', 'dropped_chunks', 'native_dropped_chunks', 'worker_dropped_chunks',
                        'sub_second_dropped_chunks', 'reader_incomplete', 'partial_tail'):
                if incoming.get(key) is None and previous.get(key) is not None:
                    incoming = dict(incoming)
                    incoming[key] = previous[key]
            for key in ('dropped_chunks', 'native_dropped_chunks', 'worker_dropped_chunks',
                        'sub_second_dropped_chunks'):
                if (isinstance(incoming.get(key), int)
                        and isinstance(previous.get(key), int)
                        and incoming[key] < previous[key]):
                    incoming = dict(incoming)
                    incoming[key] = previous[key]
                    # A clamped count keeps its own warning: an incoming
                    # lower-count warning would otherwise disagree with the
                    # displayed total.
                    if previous.get('warning'):
                        incoming['warning'] = previous['warning']
            self.capture_info = incoming
            lifecycle = info.get('state', info.get('status')) if isinstance(info, dict) else None
            if isinstance(info, dict) and (info.get('listening') is False or lifecycle in ('error', 'stopped', 'completed', 'idle')):
                self.listening = False
            self.capture_error = str(info['error'])[:500] if isinstance(info, dict) and info.get('error') else None

    def _touch_pane_seen(self):
        """Refresh the pane lease under the lease lock.

        Heartbeat renewal (touch) and lease-expiry decisions (watchdog,
        overlay lease) must serialize on the same lock, or a renewal can
        slip between an expiry check and its teardown.
        """
        with self.overlay_lock:
            self.pane_seen = time.monotonic()
            self.last_seen = self.pane_seen

    def heartbeat(self, body=None):
        """Renew only the current owner; reads cannot confer capture authority.

        Revision/epoch is the explicit trusted API-client path, never a
        renderer fallback. An expired lease cannot be revived by a late ping.
        """
        body = body or LeaseRequest()
        with self.overlay_lock:
            if body.pane is not None:
                accepted = (body.pane == self.overlay_pane
                            and body.revision is None and body.epoch is None)
            elif body.revision is not None or body.epoch is not None:
                accepted = (body.revision == self.overlay_revision and body.epoch == self.overlay_epoch)
            else:
                accepted = self.overlay_revision == 0
            accepted = accepted and self.pending_stop is None and time.monotonic() - self.pane_seen <= 30
            if accepted:
                self._touch_pane_seen()
            with self.lock:
                return {'accepted': accepted, 'listening': self.listening}

    def _expire_capture(self):
        """Called with lifecycle_lock; no stop/join while holding overlay_lock."""
        with self.overlay_lock:
            expired = self.listening and time.monotonic() - self.pane_seen > 30
        if expired:
            self.pause(retain_tail=False)
            self.overlay_hide()
        return expired

    def watchdog(self, capture_generation=None):
        if capture_generation is None:
            capture_generation = self.capture_generation
        while self.listening and capture_generation == self.capture_generation:
            time.sleep(2)
            with self.lifecycle_lock:
                if capture_generation != self.capture_generation:
                    return
                if self._expire_capture():
                    return

    def _start_stop_retry_locked(self, sequence=None):
        threading.Thread(target=self._retry_stop, args=(self.stop_sequence if sequence is None else sequence,),
                         name='meeting-stop-retry', daemon=True).start()

    def _retry_stop(self, sequence):
        # Separate from capture-generation watchdog: logical retirement must
        # not kill the only worker capable of retrying a failed physical stop.
        for _ in range(3):
            time.sleep(2)
            with self.lifecycle_lock:
                if sequence != self.stop_sequence or self.pending_stop is None:
                    return
                if self.stop_attempts >= 3:
                    return
                if self._attempt_stop_locked():
                    return

    def _attempt_stop_locked(self):
        """One adapter-bounded attempt, lifecycle lock only across stop/join."""
        audio = self.pending_stop
        if audio is None:
            return True
        self.stop_attempts += 1
        try:
            audio.stop()
            # Concrete adapters report workers still running after a bounded
            # join. A return from stop alone must not erase that uncertainty.
            status = getattr(type(audio), 'status', None)
            info = status(audio) if callable(status) else None
            if getattr(audio, 'is_running', False) is True or (
                    isinstance(info, dict) and (info.get('running') is True
                                              or info.get('stop_confirmed') is False
                                              or info.get('state', info.get('status')) == 'stopping')):
                raise RuntimeError('Adapter is still stopping')
        except Exception:
            with self.lock:
                self.stop_error = ('Capture stop is not confirmed; audio may still be active. '
                                   'Retry Pause / Retry stop. If it persists, quit the capture process or '
                                   'revoke microphone/system-audio permissions and verify the OS indicator. '
                                   f'Attempt {self.stop_attempts}/3.')
                self.retiring_generations.clear()
            return False
        with self.lock:
            self.pending_stop = None
            self.stop_error = None
            self.capture_info = {**self.capture_info, 'state': 'stopped', 'running': False,
                                 'stop_confirmed': True, 'message': 'Paused / detached'}
        return True

    def restart_after_clear(self, last_args, clear_generation, clear_session):
        # See the /clear route: restart the same session on a new generation
        # only if nothing newer took over while Clear waited for the lifecycle
        # lock. A newer start (different session or generation) or an explicit
        # pause wins: Clear never stops a newer capture or resumes a paused one.
        with self.lifecycle_lock:
            if (self.listening and last_args
                    and self.capture_generation == clear_generation + 1
                    and self.session_key == clear_session):
                stopped = self.pause(retain_tail=False)
                if not stopped['stop_confirmed']:
                    return
                self.retiring_generations.clear()
                with self.overlay_lock:
                    if time.monotonic() - self.pane_seen > 30:
                        return
                backend, recording_id, tm, src, *rest = last_args
                # Sessions saved before provider selection existed carry
                # only four fields; they restart as local/ENDPOINTING.
                provider = rest[0] if len(rest) > 0 else 'local'
                asr_mode = rest[1] if len(rest) > 1 else 'ENDPOINTING'
                self._start(True, backend, recording_id, tm, src,
                            transcription_provider=provider, asr_mode=asr_mode, renew_lease=False)

    def pause(self, *, retain_tail=True):
        with self.lifecycle_lock:
            with self.lock:
                self.listening = False
                # The retiring generation gets a bounded tail window: the
                # adapter's graceful shutdown flushes trailing speech after
                # pause, and those segments belong to this meeting. Five
                # seconds covers the adapter's default teardown budget.
                # Clear passes retain_tail=False: a forget-everything boundary
                # must not accept stop-time audio into the cleared transcript.
                if retain_tail:
                    self.retiring_generations[self.capture_generation] = time.monotonic() + 5.0
                # Prune expired windows so the map stays tiny.
                now = time.monotonic()
                for gen in [g for g, deadline in self.retiring_generations.items() if deadline < now]:
                    del self.retiring_generations[gen]
                self.capture_generation += 1
                # Cancel answer work before stopping capture: provider workers
                # observe cancellation during teardown instead of publishing
                # while stop is in progress. Pause is still only cooperative
                # for already-blocked provider calls.
                for entry in self.requests.values():
                    if entry.get('status') in ('queued', 'streaming', 'running'):
                        entry['cancelled'] = True
                        entry['status'] = 'cancelled'
                audio = self.audio if self.backend in ('native', 'macos') else self.bridge
                self.busy = False
                self.stop_sequence += 1
                self.stop_attempts = 0
                self.pending_stop = self.pending_stop or audio
                if self.pending_stop is not None:
                    self.stop_error = 'Capture stop is not confirmed; stopping audio. Do not assume the microphone is off.'
            if self.pending_stop is not None and not self._attempt_stop_locked():
                self._start_stop_retry_locked()
                return {'listening': False, 'stopping': True, 'stop_confirmed': False}
            with self.lock:
                self.busy = False
                # A genuine capture failure survives pause — including one the
                # teardown terminal just delivered: the pane must still show
                # what failed instead of an orderly-looking paused state.
                # A clean pause keeps error state clear; resume clears it via
                # the next running status.
                error_before = self.capture_error
                # Latch audio-loss evidence across pause: stopping must not
                # silently clear a transcript-incomplete warning — including
                # the per-unit counters, so the pane keeps reporting worker
                # chunks, native frames, and sub-second tails separately.
                previous = self.capture_info if isinstance(self.capture_info, dict) else {}
                latched = {'state': 'stopped', 'backend': self.backend, 'message': 'Paused / detached',
                           'error': previous.get('error') if error_before else None}
                for key in ('warning', 'dropped_chunks', 'native_dropped_chunks', 'worker_dropped_chunks',
                            'sub_second_dropped_chunks', 'reader_incomplete', 'partial_tail'):
                    if previous.get(key) is not None:
                        latched[key] = previous[key]
                self.capture_info = latched
            return {'listening': False, 'stopping': False, 'stop_confirmed': True}

    def get_engine(self):
        with self.lock:
            if self.engine is None:
                if self.engine_factory:
                    self.engine = self.engine_factory()
                else:
                    import importlib
                    module = importlib.import_module(f'{__package__}.knowledge' if __package__ else 'knowledge')
                    self.engine = module.KnowledgeEngine()
            return self.engine

    def assist(self, question, project=None, web_query=None, all_projects=False, web_fallback=False, answer_provider=None, answer_model=None, request_id=None, row_id=None, transcript=None, expected_generation=None, expected_capture_generation=None):
        from fastapi import HTTPException
        import secrets
        if all_projects and project:
            raise HTTPException(400, 'Choose all projects or one project, not both.')
        with self.lock:
            # Overlay questions belong to the snapshot the user clicked, not
            # whichever meeting is current when its worker finally arrives.
            # Compare under the same lock as clear/start and slot allocation,
            # before retaining question/transcript data or reserving capacity.
            # Pane requests omit this optional in-process admission precondition.
            if expected_generation is not None and expected_generation != self.generation:
                raise HTTPException(409, 'Meeting context changed; refresh and retry the question.')
            if expected_capture_generation is not None and (
                    not self.listening or expected_capture_generation != self.capture_generation):
                raise HTTPException(409, 'Capture context changed; refresh and retry the question.')
            if expected_capture_generation is not None:
                import importlib
                module = importlib.import_module(f'{__package__}.overlay_bridge' if __package__ else 'overlay_bridge')
                current_questions = {r['id']: r['question'] for r in module.suggest_questions(self.segments)}
                if current_questions.get(row_id) != question:
                    raise HTTPException(409, 'Question changed; refresh and tap the current row.')
            # Cancelled-but-not-yet-exited workers are not owners: a session
            # switch or clear must not block new requests behind them.
            live = [entry for entry in self.requests.values()
                    if entry.get('status') in ('queued', 'streaming', 'running') and not entry.get('cancelled')]
            if live:
                # Explicit row requests never hijack another row: 409 carries the
                # owner so the caller keeps attribution instead of polling /status.
                raise HTTPException(409, f"Request {live[0]['request_id']} is already being prepared.")
            transcript = ('\n'.join(f"{s['speaker']}: {s['text']}" for s in list(self.segments)[-40:])[-16000:]
                          if transcript is None else str(transcript)[-16000:])
            if not question.strip():
                if not transcript:
                    raise HTTPException(400, 'Enter a question or start listening first.')
                question = 'Suggest a concise answer to the most recent question in this transcript.'
            self.request_seq += 1
            request_id = (str(request_id or '').strip() or f'r-{self.request_seq}-{secrets.token_hex(4)}')[:100]
            if request_id in self.requests:
                raise HTTPException(409, f'Request {request_id} is already being prepared.')
            if len(self.requests) >= 8:
                # Bound the slot table; drop terminal entries and cancelled
                # orphans first, never genuinely live ones.
                droppable = [key for key, entry in self.requests.items()
                             if entry.get('status') in ('done', 'error', 'cancelled') or entry.get('cancelled')]
                for key in droppable[:len(self.requests) - 7]:
                    del self.requests[key]
                if len(self.requests) >= 8:
                    raise HTTPException(409, 'Too many pending answer requests.')
            # Reserve worker capacity only after every rejecting check above:
            # a 400/409 must never consume a slot that only a thread exit
            # would release. Thread-start failure below still rolls back.
            if self.live_workers >= 8:
                raise HTTPException(503, 'Too many answer workers in flight; wait for one to finish and retry.')
            self.live_workers += 1
            entry = {'request_id': request_id, 'row_id': row_id, 'question': question[:4000],
                     'status': 'queued', 'answer': '', 'sources': [], 'warnings': [],
                     'web_search': {'used': False}, 'model_route': '', 'streaming': False,
                     'completed_only': True, 'error': None, 'cancelled': False, 'generation': self.generation,
                     # Full original request: retry replays exactly these
                     # parameters (including the resolved question and the
                     # transcript snapshot) instead of current pane state.
                     'request': {'question': question[:4000], 'transcript': transcript,
                                 'project': project, 'web_query': web_query, 'all_projects': all_projects,
                                 'web_fallback': web_fallback, 'answer_provider': answer_provider,
                                 'answer_model': answer_model, 'row_id': row_id}}
            self.requests[request_id] = entry
            self.busy, self.error, self.result = True, None, None
            self.error_request = None
            generation = self.generation

        def work(entry_ref):
            streamed_any = False
            def aborted():
                with self.lock:
                    return bool(entry_ref.get('cancelled') or generation != self.generation)
            def mark_cancelled():
                with self.lock:
                    entry_ref['status'] = 'cancelled'
            try:
                if aborted():
                    mark_cancelled()
                    return
                options = {}
                if answer_provider is not None or answer_model is not None:
                    options = {'answer_provider': answer_provider, 'answer_model': answer_model}
                engine = self.get_engine()
                # Prefer a retrieval-free streaming probe so completed-only routes
                # never pay for double retrieval + ranking on fallback.
                # Legacy/fake engines without the probe keep the old behavior:
                # attempt the stream, fall back on NotImplementedError.
                use_stream = False
                try:
                    probe_fn = getattr(engine, 'supports_streaming', None)
                    probe = probe_fn(**options) if callable(probe_fn) else True
                except Exception:
                    probe = True
                stream_header, chunks = None, None
                if probe:
                    if aborted():
                        mark_cancelled()
                        return
                    try:
                        stream_header, chunks = engine.answer_stream(
                            question, transcript=transcript, project=project, web_query=web_query,
                            all_projects=all_projects, web_fallback=web_fallback,
                            should_abort=aborted, **options)
                        use_stream = chunks is not None
                    except (NotImplementedError, AttributeError) as exc:
                        # Sync stream-setup failures happen after retrieval:
                        # reuse the prepared sources instead of repeating work.
                        stream_header, chunks = getattr(exc, 'prepared', None), None
                        use_stream = False
                if not use_stream:
                    if aborted():
                        mark_cancelled()
                        return
                    # Completed-only work is genuinely running, not queued:
                    # the pane label follows the live slot, not a stale default.
                    with self.lock:
                        if entry_ref.get('status') == 'queued':
                            entry_ref['status'] = 'running'
                    # A returned stream header is post-web (fully prepared);
                    # an exception-carried payload declares its own stage.
                    # Either way the fallback must not repeat completed work.
                    if isinstance(stream_header, dict) and 'stage' not in stream_header:
                        stream_header = dict(stream_header, stage='full')
                    # The default Meta route already failed this request's
                    # stream: fall back straight to the configured model
                    # instead of retrying the failed endpoint.
                    skip_meta = (isinstance(stream_header, dict)
                                 and str(stream_header.get('model_route') or '').startswith('meta/'))
                    result = engine.answer(question, transcript=transcript, project=project, web_query=web_query, all_projects=all_projects, web_fallback=web_fallback, should_abort=aborted, prepared=stream_header, skip_meta_default=skip_meta, **options)
                    with self.lock:
                        if entry_ref.get('cancelled'):
                            entry_ref['status'] = 'cancelled'
                        elif generation == self.generation:
                            entry_ref.update({'status': 'done', 'answer': result.get('answer', ''),
                                              'sources': result.get('sources', []), 'warnings': result.get('warnings', []),
                                              'web_search': result.get('web_search', {'used': False}),
                                              'model_route': result.get('model_route', ''),
                                              'streaming': bool(result.get('streaming', False)),
                                              'completed_only': bool(result.get('completed_only', True))})
                            result['request'] = entry_ref.get('request')
                            if result.get('provider_error'):
                                # A completed-mode provider failure is an
                                # explicit error with retry — never a normal
                                # answer that hides the failure.
                                entry_ref['status'] = 'error'
                                entry_ref['error'] = str(result['provider_error'])[:500]
                                self.error = str(result['provider_error'])[:500]
                                self.error_request = entry_ref.get('request')
                                self.result = None
                            else:
                                self.result = result
                    return
                with self.lock:
                    if entry_ref.get('cancelled') or generation != self.generation:
                        entry_ref['status'] = 'cancelled'
                        return
                    entry_ref['status'] = 'streaming'
                    entry_ref['streaming'] = True
                    entry_ref['completed_only'] = False
                    header = stream_header or {}
                    entry_ref['model_route'] = header.get('model_route', '')
                    entry_ref['sources'] = header.get('sources', [])
                    entry_ref['warnings'] = header.get('warnings', [])
                    entry_ref['web_search'] = header.get('web_search', {'used': False})
                try:
                    for delta in (chunks or []):
                        with self.lock:
                            if entry_ref.get('cancelled') or generation != self.generation:
                                entry_ref['status'] = 'cancelled'
                                try:
                                    close = getattr(chunks, 'close', None)
                                    if callable(close):
                                        close()
                                except Exception:
                                    pass
                                return
                            if isinstance(delta, str) and delta:
                                streamed_any = True
                                entry_ref['answer'] = (entry_ref['answer'] + delta)[:12000]
                except (NotImplementedError, AttributeError):
                    # Lazy provider stream failed mid-iteration: fall back to a
                    # completed answer when nothing streamed yet, else keep partial.
                    if not streamed_any:
                        if aborted():
                            mark_cancelled()
                            return
                        # The slot truthfully becomes a running completed-mode
                        # job before the fallback call: the pane must read
                        # 'Preparing', not a stale 'Streaming'.
                        with self.lock:
                            entry_ref['status'] = 'running'
                            entry_ref['streaming'] = False
                        header_prepared = stream_header
                        if isinstance(header_prepared, dict) and 'stage' not in header_prepared:
                            header_prepared = dict(header_prepared, stage='full')
                        header_skip_meta = (isinstance(header_prepared, dict)
                                            and str(header_prepared.get('model_route') or '').startswith('meta/'))
                        result = engine.answer(question, transcript=transcript, project=project, web_query=web_query, all_projects=all_projects, web_fallback=web_fallback, should_abort=aborted, prepared=header_prepared, skip_meta_default=header_skip_meta, **options)
                        with self.lock:
                            if entry_ref.get('cancelled'):
                                entry_ref['status'] = 'cancelled'
                            elif generation == self.generation:
                                entry_ref.update({'status': 'done', 'answer': result.get('answer', ''),
                                                  'sources': result.get('sources', []), 'warnings': result.get('warnings', []),
                                                  'web_search': result.get('web_search', {'used': False}),
                                                  'model_route': result.get('model_route', ''),
                                                  'streaming': bool(result.get('streaming', False)),
                                                  'completed_only': bool(result.get('completed_only', True))})
                                result['request'] = entry_ref.get('request')
                                if result.get('provider_error'):
                                    entry_ref['status'] = 'error'
                                    entry_ref['error'] = str(result['provider_error'])[:500]
                                    self.error = str(result['provider_error'])[:500]
                                    self.error_request = entry_ref.get('request')
                                    self.result = None
                                else:
                                    self.result = result
                        return
                    with self.lock:
                        warnings = list(entry_ref.get('warnings') or [])
                        msg = 'Stream interrupted; showing a partial answer. Retry for the complete response.'
                        if msg not in warnings:
                            warnings.append(msg)
                        entry_ref['warnings'] = warnings
                with self.lock:
                    if generation == self.generation and not entry_ref.get('cancelled'):
                        warnings = list(entry_ref.get('warnings') or [])
                        sources = entry_ref.get('sources') or []
                        import re as _re
                        citations = [int(n) for n in _re.findall(r'\[(\d+)\]', entry_ref.get('answer') or '')]
                        if sources and (not citations or any(n < 1 or n > len(sources) for n in citations)):
                            msg = 'Answer citations are missing or invalid; verify against the listed evidence.'
                            if msg not in warnings:
                                warnings.append(msg)
                        if len(entry_ref.get('answer') or '') >= 12000:
                            trunc = 'Answer reached the 12,000-character overlay cap and was shortened; ask a follow-up for the rest.'
                            if trunc not in warnings:
                                warnings.append(trunc)
                        entry_ref['warnings'] = warnings
                        entry_ref['status'] = 'done'
                        self.result = {'answer': entry_ref['answer'], 'sources': entry_ref['sources'],
                                       'warnings': warnings, 'web_search': entry_ref['web_search'],
                                       'model_route': entry_ref['model_route'], 'streaming': True,
                                       'completed_only': False, 'request': entry_ref.get('request')}
                    elif entry_ref.get('cancelled'):
                        entry_ref['status'] = 'cancelled'
            except Exception as exc:
                if _is_engine_abort(exc):
                    # Cooperative cancellation inside the engine: no error,
                    # no partial publication beyond what already streamed.
                    mark_cancelled()
                    return
                with self.lock:
                    if entry_ref.get('cancelled'):
                        entry_ref['status'] = 'cancelled'
                    elif generation == self.generation:
                        if streamed_any and (entry_ref.get('answer') or '').strip():
                            # Ordinary stream failures after partial output keep
                            # the visible partial text, marked — not presented
                            # as a complete answer, and never as a bare error
                            # that hides what already arrived.
                            warnings = list(entry_ref.get('warnings') or [])
                            msg = f'Stream interrupted ({type(exc).__name__}); showing a partial answer. Retry for the complete response.'
                            if msg not in warnings:
                                warnings.append(msg)
                            entry_ref['warnings'] = warnings
                            entry_ref['status'] = 'done'
                            self.result = {'answer': entry_ref['answer'], 'sources': entry_ref.get('sources', []),
                                           'warnings': warnings, 'web_search': entry_ref.get('web_search', {'used': False}),
                                           'model_route': entry_ref.get('model_route', ''), 'streaming': True,
                                           'completed_only': False, 'request': entry_ref.get('request')}
                        else:
                            entry_ref.update({'status': 'error', 'error': f'{type(exc).__name__}: {str(exc)[:500]}'})
                            self.error = f'{type(exc).__name__}: {str(exc)[:500]}'
                            # The failed request stays retryable with its exact
                            # original parameters, not current pane state.
                            self.error_request = entry_ref.get('request')
            finally:
                with self.lock:
                    # Cancelled orphans do not own the busy flag: a blocked
                    # old-generation worker must never wedge the pane busy
                    # after a newer request has already completed.
                    self._recompute_busy_locked()
                    self.live_workers = max(0, self.live_workers - 1)
        # Provider credentials are request/profile scoped; propagate that scope into the worker.
        # Native click handlers run on overlay threads, so the bridge snapshot
        # also carries the originating context with it (see BackendTransport).
        import contextvars
        context = contextvars.copy_context()
        with self.lock:
            current = self.requests.get(request_id)
        if current is not entry:
            # The slot was cleared — or the caller-supplied ID was reused by
            # a newer request — before this worker started. Never run the old
            # request against a stranger's slot: that would attribute the old
            # answer to the wrong row and corrupt the replacement's
            # lifecycle (including cancelling it on a generation change).
            # The replacement's own worker owns its entry.
            with self.lock:
                self.live_workers = max(0, self.live_workers - 1)
                self._recompute_busy_locked()
            raise RuntimeError('Answer request was cleared before it could start.')
        try:
            threading.Thread(target=lambda: context.run(work, entry), name=f'meeting-answer-{request_id}', daemon=True).start()
        except Exception:
            with self.lock:
                self.live_workers = max(0, self.live_workers - 1)
                if entry.get('status') == 'queued':
                    entry['status'] = 'error'
                    entry['error'] = 'Answer worker could not start.'
                self._recompute_busy_locked()
            raise
        return {'accepted': True, 'request_id': request_id, 'streaming': None}

    def _recompute_busy_locked(self):
        """Busy follows genuinely live, uncancelled work — never orphans."""
        if not any(e.get('status') in ('queued', 'streaming', 'running') and not e.get('cancelled')
                   for e in self.requests.values()):
            self.busy = False

    def assist_request(self, request_id):
        with self.lock:
            entry = self.requests.get(request_id)
            if entry is None:
                return None
            return dict(entry)

    def cancel_assist(self, request_id):
        from fastapi import HTTPException
        with self.lock:
            entry = self.requests.get(request_id)
            if entry is None:
                raise HTTPException(404, 'Unknown answer request.')
            if entry.get('status') in ('done', 'error', 'cancelled'):
                return {'request_id': request_id, 'status': entry['status']}
            entry['cancelled'] = True
            entry['status'] = 'cancelled'
            if not any(e.get('status') in ('queued', 'streaming', 'running') for e in self.requests.values()):
                self.busy = False
            return {'request_id': request_id, 'status': 'cancelled'}

    @staticmethod
    def _clean_overlay_scope(scope):
        """Keep only pane-owned keys; drop anything the pane never sets."""
        scope = scope if isinstance(scope, dict) else {}
        cleaned = {}
        seq = scope.get('seq')
        if isinstance(seq, int):
            cleaned['seq'] = seq
        project = scope.get('project')
        if isinstance(project, str) and project:
            cleaned['project'] = project[:1000]
        if scope.get('all_projects') is True:
            cleaned['all_projects'] = True
        if scope.get('web_fallback') is True:
            cleaned['web_fallback'] = True
        elif scope.get('web_fallback') is False:
            cleaned['web_fallback'] = False
        web_query = scope.get('web_query')
        if isinstance(web_query, str) and web_query.strip():
            cleaned['web_query'] = web_query.strip()[:300]
        provider = scope.get('answer_provider')
        model = scope.get('answer_model')
        if isinstance(provider, str) and provider and isinstance(model, str) and model:
            cleaned['answer_provider'] = provider[:100]
            cleaned['answer_model'] = model[:200]
        return cleaned

    def _overlay_lease(self, overlay, seq):
        """Hide the overlay when its pane lease expires, listening or not.

        The capture watchdog only runs while listening; a manually paused
        pane that then disconnects would otherwise orphan the window.
        """
        while True:
            time.sleep(5)
            if self._overlay_hide_if_expired(overlay, seq):
                return

    def _overlay_hide_if_expired(self, overlay, seq):
        """Hide only if the same overlay/lease is still current and expired.

        Identity, sequence, and heartbeat are revalidated atomically under
        overlay_lock so a renewed show or heartbeat cannot be torn down by a
        stale expiry check.
        """
        with self.lifecycle_lock:
            with self.overlay_lock:
                if self.overlay is not overlay or seq != self.overlay_lease_seq:
                    return True
                if time.monotonic() - self.pane_seen <= 30:
                    return False
                self._retire_overlay_locked()
            self._finish_overlay_cleanup()
        return True

    def _start_overlay_lease_locked(self):
        self.overlay_lease_seq += 1
        overlay, seq = self.overlay, self.overlay_lease_seq
        if overlay is not None:
            threading.Thread(target=self._overlay_lease, args=(overlay, seq),
                             name='meeting-overlay-lease', daemon=True).start()

    def _refresh_overlay_token_locked(self, force=False):
        # Re-bind the transport to this explicit pane action. Must run under
        # overlay_lock on the pane's request thread so the captured context
        # (and token) belong to the authenticated action, not a worker.
        import secrets
        bridge = self.overlay
        transport = getattr(bridge, 'transport', None)
        refresh = getattr(transport, 'refresh_pane_token', None)
        module_action = None
        try:
            import importlib
            module = importlib.import_module(f'{__package__}.overlay_bridge' if __package__ else 'overlay_bridge')
            module_action = getattr(module, '_PANE_ACTION', None)
        except Exception:
            module_action = None
        if not callable(refresh):
            return
        matches = getattr(transport, 'pane_context_matches', None)
        if not force and callable(matches) and matches() is True:
            return
        retire = getattr(bridge, 'retire_authorization', None)
        if callable(retire):
            retire()
        try:
            token = secrets.token_hex(16)
            if module_action is not None:
                module_action.set(token)
            refresh(token)
        except Exception:
            raise _unavailable('Overlay credential scope unavailable. Reopen the pane and retry.') from None

    def overlay_claim(self, revision=None, *, client=None, creation=None, epoch=None):
        """Claim once per mounted pane, never once per Show retry.

        A later pane revokes the old capability. Only the current capability
        can send ordered Show/Hide/Release actions; counters never rebind it.
        """
        import secrets
        with self.lifecycle_lock:
            with self.overlay_lock:
                if client is not None:
                    # Renderer-local creation order is assigned BEFORE any lookup.
                    # Different renderers have no comparable clock: never guess an
                    # order from network arrival, wall time or random identifiers.
                    if epoch != self.overlay_epoch:
                        return {'accepted': False, 'reason': 'backend_restarted'}
                    if self.overlay_client not in (None, client):
                        return {'accepted': False, 'reason': 'different_renderer'}
                    if type(creation) is not int or creation <= self.overlay_creation:
                        return {'accepted': False, 'reason': 'superseded'}
                    self.overlay_client = client
                    self.overlay_creation = creation
                elif revision is not None and revision != self.overlay_revision:
                    # Only an immediately preceding release can be recovered. Do
                    # not issue fresh tickets on conflicts with another claimant:
                    # an old pane must never retry its way over a newer owner.
                    if self.overlay_pane is None and revision == self.overlay_revision - 1:
                        return {'accepted': False, 'retry_revision': self.overlay_revision}
                    return {'accepted': False}
            # A claim authorizes a pane, never continuation of someone else's
            # capture. Stop before minting authority; hold lifecycle, not overlay,
            # across adapter teardown (callbacks may need the state lock).
            if self.listening:
                self.pause(retain_tail=False)
            with self.overlay_lock:
                self.overlay_revision += 1
                self._retire_overlay_locked()
                self.overlay_pane = secrets.token_hex(16)
                self.overlay_action = 0
                # Claim is explicit authorization, not a passive status read.
                self._touch_pane_seen()
                pane = self.overlay_pane
            # Grant recovery authority even if the retired helper is stuck,
            # but never lose its handle or let the new pane reuse it.
            self._finish_overlay_cleanup()
        return {'pane': pane}

    @contextmanager
    def control_boundary(self, body):
        """Fence HTTP controls for their entire operation, including Clear restart.

        Internal watchdog/teardown calls remain trusted and use pause/start
        directly. Legacy HTTP clients work until the first pane claims; after
        that they must explicitly echo a current lifecycle revision.
        """
        from fastapi import HTTPException
        body = body or LifecycleControlRequest()
        with self.lifecycle_lock:
            with self.overlay_lock:
                if body.pane is not None or body.action is not None:
                    accepted = (body.revision is None and body.epoch is None
                                and self._accept_overlay_action_locked(body.pane, body.action))
                elif body.revision is not None or body.epoch is not None:
                    accepted = (body.revision == self.overlay_revision and body.epoch == self.overlay_epoch)
                else:
                    accepted = self.overlay_revision == 0
                if not accepted:
                    raise HTTPException(409, 'Lifecycle control is stale or unscoped. Reopen the pane; API clients must read /overlay/lifecycle and send its revision and epoch explicitly.')
            self._expire_capture()
            yield

    def _accept_overlay_action_locked(self, pane, action):
        if (not pane or pane != self.overlay_pane or type(action) is not int
                or action <= self.overlay_action):
            return False
        self.overlay_action = action
        return True

    def overlay_release(self, pane, action):
        # Cleanup must not pause capture or hide the helper of a newer pane.
        with self.lifecycle_lock:
            with self.overlay_lock:
                if not self._accept_overlay_action_locked(pane, action):
                    return {'accepted': False}
                self.overlay_pane = None
                self.overlay_revision += 1
                self._retire_overlay_locked()
            stopped = self.pause()
            display = self._finish_overlay_cleanup()
        return {'accepted': True, **display, **stopped}

    def overlay_show(self, scope, *, pane=None, action=None):
        """Explicit pane action: backend-managed bridge owns one helper process."""
        import secrets
        with self.lifecycle_lock:
            with self.overlay_lock:
                if pane is not None and not self._accept_overlay_action_locked(pane, action):
                    return {'shown': self.overlay is not None, 'accepted': False}
            self._expire_capture()
            result = self._overlay_show(scope)
        if not isinstance(result, dict) or 'startup' not in result:
            return result
        job = result['startup']
        # No shutdown/publication lock is held while spawning, probing or
        # waiting for startup. A reserved pending bridge fences new admission.
        try:
            job['worker'].start()
        except RuntimeError as exc:
            job['error'] = f'Overlay startup worker could not start: {exc}'
            job['done'].set()
        job['done'].wait(3)
        # Observe startup liveness off the lifecycle/publication locks. A hung
        # poll remains bounded and cannot turn uncertainty into Show success.
        process = getattr(job['bridge'], 'process', None)
        alive = self._overlay_process_alive(process) if process is not None else True
        with self.lifecycle_lock:
            self._expire_capture()
            with self.overlay_lock:
                bridge = job['bridge']
                current = (self._overlay_startup_job is job and self.pending_overlay is bridge
                           and self.overlay_lease_seq == job['epoch'])
                with getattr(bridge, 'lock', nullcontext()):
                    shown = (job['done'].is_set() and not job['error']
                             and bool(job['result'].get('shown'))
                             and process not in getattr(bridge, '_reader_failed', ())
                             and alive is True
                             and getattr(bridge, 'process', None) is process
                             and not isinstance(getattr(process, 'returncode', None), int)
                             and getattr(bridge, 'stopping', False) is not True)
                    if current and shown:
                        self.pending_overlay = None
                        self.overlay_cleanup_error = None
                        self._overlay_startup_job = None
                        self.overlay = bridge
                        self.overlay_owner = secrets.token_hex(16)
                        if not job['capture_was_listening'] and not self.listening and self.pending_stop is None:
                            self._touch_pane_seen()
                        self._start_overlay_lease_locked()
                        return {**job['result'], 'available': True, 'owner': self.overlay_owner}
                if current:
                    self._retire_overlay_locked()
                error = job['error'] or job['result'].get('error') or 'Overlay startup was retired or timed out.'
            if self.pending_overlay is bridge:
                self._finish_overlay_cleanup()
            raise _unavailable(self.overlay_cleanup_error or error)

    def _overlay_process_alive(self, process):
        """Tri-state inspection, with no process method on the caller thread.

        A poll wrapper can hang just like a signal wrapper. One controller-wide
        slot stays occupied until actual worker exit, including after done is
        signaled. Same-process callers may join only before completion is
        published; callers arriving afterward get unknown until worker exit.
        Publication is not a fresh OS sample: every waiter vetoes healthy
        evidence contradicted by that handle's known returncode at consumption.
        Poll runs without control/publication locks; each caller waits at most
        50ms (subject to scheduling). This is an observation, not a guarantee
        that the child cannot exit after the last check.
        """
        if process is None:
            return False
        with self._overlay_probe_lock:
            self._overlay_probe_jobs = {target: job for target, job in self._overlay_probe_jobs.items()
                                        if job['worker'].is_alive()}
            if self._overlay_probe_jobs:
                target, job = next(iter(self._overlay_probe_jobs.items()))
                if target is not process or job['done'].is_set():
                    return None
            else:
                job = {'done': threading.Event(), 'alive': None}
                self._overlay_probe_jobs[process] = job
                def inspect_process():
                    alive = None
                    try:
                        alive = process.poll() is None
                    except Exception:
                        pass  # unknown is not evidence of exit
                    # Admission and completion share this lock: a caller either
                    # joins before publication or cannot consume this result.
                    with self._overlay_probe_lock:
                        job['alive'] = alive
                        job['done'].set()
                job['worker'] = threading.Thread(target=inspect_process, name='meeting-overlay-probe', daemon=True)
                job['worker'].start()
        if not job['done'].wait(.05):
            return None
        # Publication can lag the OS sample (including inside a poll wrapper).
        # Each waiter checks the exact handle again, without invoking poll on
        # a control thread. A known exit contradicts this healthy sample; it
        # does not itself prove group/reader cleanup or authorize admission.
        if job['alive'] is True and isinstance(getattr(process, 'returncode', None), int):
            return None
        return job['alive']

    def _reconcile_overlay_admission(self, *, for_show=False):
        """Discover autonomous bridge retirement before Start or Show.

        Caller holds lifecycle_lock. Preserve overlay -> bridge -> state order;
        cleanup waits happen outside all publication locks. A confirmed child
        exit can finish cleanup immediately, but an unknown/live retired child
        remains reachable and denies admission until an explicit cleanup retry.
        """
        # lifecycle_lock stabilizes controller ownership. Healthy admission
        # needs no publication lock (a tap may be waiting for state.lock while
        # holding it); only retirement takes overlay -> bridge -> state locks.
        bridge = self.overlay or self.pending_overlay
        if bridge is None:
            return
        process = getattr(bridge, 'process', None)
        alive = self._overlay_process_alive(process)
        retire = self.overlay is bridge and (getattr(bridge, 'stopping', False) is True
                 or process in getattr(bridge, '_reader_failed', ())
                 or alive is None or (alive is False and (process is not None or for_show)))
        if retire:
            with self.overlay_lock:
                self._retire_overlay_locked()
        if self.pending_overlay is bridge and alive is False:
            self._finish_overlay_cleanup()
        bridge = self.overlay
        return (bridge, getattr(bridge, '_lifecycle_epoch', 0))

    @contextmanager
    def _overlay_admission_guard(self, ticket, code=409):
        # lifecycle_lock stabilizes controller ownership. The small retirement
        # gate does NOT require bridge/publication locks: a tap may hold those
        # while waiting for state.lock. Never acquire them with this gate held.
        # Retirement takes bridge -> admission (marker only), releases admission,
        # then cancels slots. Capture admission takes admission -> state only.
        from fastapi import HTTPException
        bridge, epoch = ticket or (None, 0)
        publication = self.overlay_lock if code == 503 else nullcontext()
        display = getattr(bridge, 'lock', nullcontext()) if code == 503 else nullcontext()
        with publication, display, getattr(bridge, '_admission_lock', nullcontext()):
            process = getattr(bridge, 'process', None)
            # Plain cached exit evidence can veto a previously healthy ticket.
            # Never poll here: freshness/cleanup is established off these locks.
            retired = bridge is not None and (
                getattr(bridge, 'stopping', False) is True
                or isinstance(getattr(process, 'returncode', None), int)
                or process in getattr(bridge, '_reader_failed', ())
                or getattr(bridge, '_lifecycle_epoch', 0) != epoch)
            if (not retired and self.overlay is bridge and self.pending_overlay is None
                    and not self.overlay_cleanup_error):
                yield
                return
        if retired and self.overlay is bridge:
            with self.overlay_lock:
                self._retire_overlay_locked()
        # A clean exit in the reconciliation/commit gap is not an unconfirmed
        # helper. Reap outside all admission/publication locks, then continue
        # only if lifecycle ownership is still empty and cleanup is confirmed.
        if retired and self._overlay_process_alive(process) is False:
            if self.pending_overlay is bridge:
                self._finish_overlay_cleanup()
            if self.overlay is None and self.pending_overlay is None and not self.overlay_cleanup_error:
                with self.overlay_lock if code == 503 else nullcontext():
                    yield
                    return
        raise HTTPException(code, self.overlay_cleanup_error or
                            'Overlay lifecycle changed; retry the action.')

    def _overlay_show(self, scope):
        import importlib
        import secrets
        ticket = self._reconcile_overlay_admission(for_show=True)
        with self._overlay_admission_guard(ticket, 503):
            if self.pending_overlay is not None or self.overlay_cleanup_error:
                raise _unavailable(self.overlay_cleanup_error or 'Overlay shutdown is not confirmed. Retry Hide / Clear.')
            cleaned = self._clean_overlay_scope(scope)
            if self.overlay is not None:
                # Reconciliation confirmed a live, non-retired helper.
                # Explicit Show establishes a fresh owner, even on reuse.
                self.overlay_owner = secrets.token_hex(16)
                self.overlay.scope_seq = 0
                result = self.overlay.set_scope(cleaned)
                if not (isinstance(result, dict) and result.get('ignored_stale')):
                    self.overlay_scope_value = cleaned
                    self._refresh_overlay_token_locked(force=True)
                try:
                    self.overlay.update(self.status())
                except Exception:
                    pass
                # Scope publication/status may observe an exit after the
                # reconciliation probe. Do not commit a known-dead reuse.
                process = getattr(self.overlay, 'process', None)
                if (isinstance(getattr(process, 'returncode', None), int)
                        or process in getattr(self.overlay, '_reader_failed', ())):
                    self._retire_overlay_locked()
                    raise _unavailable('Overlay helper exited during Show; retry Hide / Show.')
                if not self.listening and self.pending_stop is None:
                    self._touch_pane_seen()  # idle display lease only
                self._start_overlay_lease_locked()
                return {'shown': True, 'reused': True, 'available': True, 'owner': self.overlay_owner}
            try:
                module = importlib.import_module(f'{__package__}.overlay_bridge' if __package__ else 'overlay_bridge')
            except ImportError:
                from fastapi import HTTPException
                raise HTTPException(503, 'Overlay helper is not installed in this backend.')
            try:
                pane_token = secrets.token_hex(16)
                module_pane_action = getattr(module, '_PANE_ACTION', None)
                if module_pane_action is not None:
                    module_pane_action.set(pane_token)
            except Exception:
                pane_token = None
            bridge = module.OverlayBridge(helper=self._overlay_helper_path(), transport=module.BackendTransport(self, pane_token=pane_token))
            set_result = bridge.set_scope(cleaned)
            if not (isinstance(set_result, dict) and set_result.get('ignored_stale')):
                self.overlay_scope_value = cleaned
            try:
                bridge.update(self.status())
            except Exception:
                pass
            job = {'bridge': bridge, 'epoch': self.overlay_lease_seq,
                   'done': threading.Event(), 'result': {}, 'error': None,
                   'capture_was_listening': self.listening}
            # The exact bridge is reachable even before Popen returns. Cleanup
            # cannot prove absence while this job could still create a child.
            self.pending_overlay = bridge
            self.overlay_cleanup_error = 'Overlay startup is pending; shutdown is not confirmed.'
            self._overlay_startup_job = job
            import contextvars
            context = contextvars.copy_context()
            def launch():
                try:
                    result = context.run(bridge.show_overlay)
                    job['result'] = result if isinstance(result, dict) else {'shown': bool(result)}
                except Exception as exc:
                    job['error'] = f'Overlay startup failed: {type(exc).__name__}: {str(exc)[:200]}'
                finally:
                    job['done'].set()
            job['worker'] = threading.Thread(target=launch, name='meeting-overlay-startup', daemon=True)
            return {'startup': job}

    @staticmethod
    def _invalidate_overlay_locked(bridge):
        # overlay_lock -> bridge.lock -> state.lock; no teardown/pipe IO.
        # Legacy injected display-only bridges may not expose invalidate.
        invalidate = getattr(bridge, 'invalidate', None)
        if callable(invalidate):
            invalidate()

    def _retire_overlay_locked(self):
        # Keep the exact retired bridge reachable until cleanup is confirmed.
        # Publication stops immediately; a pending bridge is never reused.
        bridge = self.overlay or self.pending_overlay
        startup = self._overlay_startup_job
        if startup is not None and startup['bridge'] is bridge:
            bridge._startup_cancelled = True
        self._overlay_invalidation_error = None
        try:
            self._invalidate_overlay_locked(bridge)
        except Exception:
            self._overlay_invalidation_error = 'Overlay invalidation failed; shutdown is not confirmed. Retry hide overlay and verify the window is gone before sharing.'
        self.overlay = None
        self.pending_overlay = bridge
        self.overlay_owner = None
        self.overlay_lease_seq += 1
        if bridge is not None:
            self.overlay_cleanup_error = (self._overlay_invalidation_error or
                'Overlay shutdown is not confirmed; the old window may still display meeting content. Retry Hide / Clear.')

    def _finish_overlay_cleanup(self):
        # Caller may hold lifecycle_lock, never overlay/bridge/state locks.
        # Even an unexpectedly blocking adapter cannot hold that lock forever.
        # The worker returns evidence only; only a current caller may publish it.
        with self.overlay_lock:
            bridge = self.pending_overlay
            startup = self._overlay_startup_job
            if startup is not None and startup['bridge'] is bridge and not startup['done'].is_set():
                return {'shown': None, 'overlay_stop_confirmed': False,
                        'overlay_cleanup_error': self.overlay_cleanup_error}
            job = self._overlay_cleanup_job
            if bridge is not None and (job is None or job['bridge'] is not bridge
                                      or (job['done'].is_set() and job['error'])):
                job = {'bridge': bridge, 'done': threading.Event(), 'error': None}
                self._overlay_cleanup_job = job
                def cleanup():
                    try:
                        result = bridge.hide_overlay()
                        if isinstance(result, dict) and result.get('stop_confirmed') is False:
                            raise RuntimeError(result.get('error') or 'Overlay shutdown is not confirmed')
                        # Legacy injected bridges need not report completion,
                        # but a real retained process always requires proof.
                        process = getattr(bridge, 'process', None)
                        if process is not None and process.poll() is None:
                            raise RuntimeError('Overlay process is still running')
                    except Exception:
                        job['error'] = 'Overlay teardown failed; shutdown is not confirmed. Retry hide overlay and verify the window is gone before sharing.'
                    finally:
                        job['done'].set()
                threading.Thread(target=cleanup, name='meeting-overlay-cleanup', daemon=True).start()
        if bridge is not None:
            job['done'].wait(.8)
        with self.overlay_lock:
            if bridge is not None and self.pending_overlay is bridge and job['done'].is_set():
                error = self._overlay_invalidation_error or job['error']
                if error:
                    self.overlay_cleanup_error = error
                else:
                    self.pending_overlay = None
                    self._overlay_cleanup_job = None
                    if startup is not None and startup['bridge'] is bridge:
                        self._overlay_startup_job = None
                    self.overlay_cleanup_error = None
            confirmed = self.pending_overlay is None and self.overlay_cleanup_error is None
            return {'shown': False if confirmed else None,
                    'overlay_stop_confirmed': confirmed,
                    'overlay_cleanup_error': self.overlay_cleanup_error}

    def overlay_hide(self, *, pane=None, action=None):
        with self.lifecycle_lock:
            with self.overlay_lock:
                if pane is not None and not self._accept_overlay_action_locked(pane, action):
                    return {'shown': None if self.pending_overlay else self.overlay is not None, 'accepted': False}
                self._retire_overlay_locked()
            return {'accepted': True, **self._finish_overlay_cleanup()}

    @contextmanager
    def overlay_clear_boundary(self):
        """Retire atomically; bounded cleanup outside publication locks.

        Order remains lifecycle -> overlay -> bridge -> state. Failed cleanup
        persists across Clear; successful retry never reattaches the old bridge.
        """
        try:
            with self.overlay_lock:
                self._retire_overlay_locked()
                yield
        finally:
            self._finish_overlay_cleanup()

    @contextmanager
    def credential_change_boundary(self):
        """Serialize owner edits with pane lifecycle and new click admission.

        Re-read host secrets even if a writer fails after committing. The edit
        request's pre-write snapshot must never become the overlay snapshot.
        No pane/owner/action/lease is minted or renewed by a credential edit.
        """
        transition = {'overlay_credentials': 'not_shown'}
        with self.lifecycle_lock:
            retired = False
            try:
                with self.overlay_lock:
                    try:
                        yield transition
                    finally:
                        bridge = self.overlay
                        if bridge is None and self.pending_overlay is not None:
                            # A Show can be outside lifecycle_lock awaiting
                            # startup. It captured pre-edit secrets but has not
                            # admitted answers: retire its exact reserved handle
                            # so late startup cannot publish the stale context.
                            self._retire_overlay_locked()
                            retired = True
                            transition['overlay_credentials'] = 'retired'
                        elif bridge is not None:
                            try:
                                bridge.transport.refresh_credentials()
                                transition['overlay_credentials'] = 'refreshed'
                            except Exception:
                                # Never reflect credential/transport exceptions.
                                self._retire_overlay_locked()
                                retired = True
                                transition['overlay_credentials'] = 'retired'
            finally:
                if retired:
                    result = self._finish_overlay_cleanup()
                    transition['overlay_stop_confirmed'] = result['overlay_stop_confirmed']

    def overlay_scope(self, scope):
        # Atomic under overlay_lock: bridge acceptance and authoritative
        # publication cannot interleave with a concurrent update.
        with self.overlay_lock:
            cleaned = self._clean_overlay_scope(scope)
            bridge = self.overlay
            if (bridge is None or not isinstance(scope, dict) or not self.overlay_owner
                    or scope.get('owner') != self.overlay_owner):
                return {'shown': bridge is not None, 'scope': dict(self.overlay_scope_value), 'accepted': False}
            result = bridge.set_scope(cleaned)
            if isinstance(result, dict) and result.get('ignored_stale'):
                return {'shown': True, 'scope': dict(self.overlay_scope_value), 'accepted': False}
            changed = ({k: v for k, v in self.overlay_scope_value.items() if k != 'seq'}
                       != {k: v for k, v in cleaned.items() if k != 'seq'})
            self.overlay_scope_value = cleaned
            self._refresh_overlay_token_locked(force=changed)
            try:
                bridge.update(self.status())
            except Exception:
                pass
            return {'shown': True, 'scope': cleaned, 'accepted': True}

    def _overlay_helper_path(self):
        import os as _os
        from pathlib import Path as _Path
        override = _os.environ.get('MEETING_COPILOT_OVERLAY_HELPER', '').strip()
        if override:
            candidate = _Path(override)
            if candidate.is_file():
                return str(candidate)
        here = _Path(__file__).resolve().parent
        for candidate in (here / 'macos_overlay', here / 'native' / 'macos_overlay'):
            if candidate.is_file():
                return str(candidate)
        return None

    def status(self, touch=True):
        if touch:
            # Preserve pre-pane API compatibility only. After the first claim,
            # unscoped status (including rejected/reloaded renderers) is read-only.
            self.heartbeat()
        with self.lock:
            # Order snapshots at capture, not delivery. Includes successive
            # snapshots within one meeting/capture epoch (e.g. corrections).
            self.status_seq += 1
            live = [dict(e) for e in self.requests.values() if e.get('status') in ('queued', 'streaming', 'running')]
            terminal = [dict(e) for e in self.requests.values() if e.get('status') in ('done', 'error')]
            terminal = terminal[-5:]
            for entry in terminal:
                if isinstance(entry.get('answer'), str) and len(entry['answer']) > 4000:
                    entry['answer'] = entry['answer'][:4000] + '… (shortened; ask again for the rest)'
                    entry['answer_shortened'] = True
            overlay_status = None
            overlay = self.overlay
            uncertain_overlay = self.pending_overlay is not None
            cleanup_error = self.overlay_cleanup_error
            if overlay is not None and getattr(overlay, 'stopping', False) is True:
                uncertain_overlay = uncertain_overlay or overlay.process is not None
                cleanup_error = cleanup_error or getattr(overlay, 'cleanup_error', None)
            if overlay is not None:
                try:
                    overlay_status = overlay.helper_status()
                except Exception:
                    overlay_status = None
                alive = self._overlay_process_alive(overlay.process)
                if overlay.process in getattr(overlay, '_reader_failed', ()):
                    uncertain_overlay = True
                    cleanup_error = cleanup_error or (
                        'Overlay click channel failed; the window may still be visible. '
                        'Overlay shutdown is not confirmed. Retry Hide / Clear before starting capture.')
                if alive is None:
                    uncertain_overlay = True
                if alive is False:
                    # Parent exit is not group/reader cleanup evidence. Keep
                    # visibility unknown until the bridge reaper clears its
                    # owned handle; status must not bypass that confirmation.
                    uncertain_overlay = uncertain_overlay or overlay.process is not None
                    overlay = None
            if uncertain_overlay and not cleanup_error:
                cleanup_error = ('Overlay shutdown is not confirmed; the window may still be visible. '
                                 'Retry Hide / Clear before starting capture.')
            # The last session descriptor lets the pane compare the full
            # session key (backend + recording/model/source) before starting:
            # any change begins a fresh meeting and clears retained content.
            last_session = None
            try:
                key = self.session_key
                if isinstance(key, tuple) and len(key) == 2:
                    if key[0] == 'meetily':
                        last_session = {'backend': 'meetily', 'recording_id': key[1]}
                    else:
                        pair = key[1] if isinstance(key[1], (tuple, list)) else (None, None)
                        pair = list(pair) + [None, None, None, None]
                        last_session = {'backend': key[0], 'transcription_model': pair[0],
                                        'audio_source': pair[1], 'transcription_provider': pair[2] or 'local',
                                        'asr_mode': pair[3] or 'ENDPOINTING'}
            except Exception:
                last_session = None
            overlay_available = self._overlay_capability_available(overlay, overlay_status)
            import sys as _sys_cap
            macos_ready = _sys_cap.platform == 'darwin' and self._macos_runtime_ready()
            return {'listening': self.listening, 'stopping': self.pending_stop is not None,
                    'stop_confirmed': not self.listening and self.pending_stop is None,
                    'stop_attempts': self.stop_attempts,
                    'overlay_cleanup_error': cleanup_error,
                    'overlay_stop_confirmed': not uncertain_overlay and overlay is None and cleanup_error is None,
                    'segments': list(self.segments), 'error': self.stop_error or cleanup_error or self.error or self.capture_error,
                    # Keep the legacy summary, but never infer retry authority
                    # from it: independent failures can coexist behind it.
                    'error_source': ('stop' if self.stop_error else 'overlay' if cleanup_error else
                                     'answer' if self.error else 'capture' if self.capture_error else None),
                    'stop_error': self.stop_error, 'answer_error': self.error, 'capture_error': self.capture_error,
                    'error_request': self.error_request if self.error else None,
                    'busy': self.busy, 'result': self.result, 'requests': {e['request_id']: dict(e) for e in live},
                    'recent': terminal, 'meetily_clear_error': self.meetily_clear_error,
                    'capture': ({**self.capture_info, 'state': 'stopping', 'error': self.stop_error,
                                 'message': 'Capture stop is not confirmed; audio may still be active.'}
                                if self.pending_stop is not None else self.capture_info),
                    'backend': self.backend, 'last_session': last_session,
                    'generation': self.generation, 'status_seq': self.status_seq,
                    'capture_generation': self.capture_generation, 'pane_seen': self.pane_seen,
                    'overlay_shown': None if uncertain_overlay else overlay is not None,
                    'capabilities': {'all_projects': True, 'automatic_web_fallback': True, 'independent_models': True,
                                     'meta_asr': _meta_key_present(),
                                     'macos_transcription': bool(macos_ready),
                                     'overlay': overlay_available, 'overlay_stream': True, 'overlay_cancel': True,
                                     'macos_detail': self._macos_capability_detail(macos_ready)}}

    def _overlay_capability_available(self, overlay, overlay_status):
        # With no bridge yet, capability reflects whether a helper could
        # start; once shown, it reflects the live bridge's availability.
        if overlay is None:
            try:
                path = self._overlay_helper_path()
            except Exception:
                return False
            if not path or not isinstance(path, str):
                return False
            import os as _os
            import sys as _sys
            if _sys.platform not in ('darwin', 'linux'):
                return False
            return _os.path.isfile(path) and _os.access(path, _os.X_OK)
        if isinstance(overlay_status, dict):
            return bool(overlay_status.get('available', True))
        return True

    def _macos_runtime_ready(self):
        """True only when the macOS worker env and helper look present. No probing."""
        import os as _os
        from pathlib import Path as _Path
        try:
            root = _Path(__file__).resolve().parent
            helper = root / 'macos_capture'
            worker = root / 'audio_worker.py'
            runtime = root / 'audio-runtime/bin/python'
            venv = root / '.venv-audio/bin/python'
            override = _os.environ.get('MEETING_COPILOT_AUDIO_PYTHON', '').strip()
            python_ok = (_Path(override).is_file() if override else
                         (runtime.is_file() or venv.is_file()))
            return bool(helper.is_file() and worker.is_file() and python_ok)
        except Exception:
            return False

    def _macos_capability_detail(self, ready):
        import sys as _sys
        if _sys.platform != 'darwin':
            return 'macOS transcription requires macOS.'
        if ready:
            return ''
        return 'On macOS: run prepare_macos.py, allow Microphone / Screen Recording, then restart capture.'


def _unavailable(message):
    from fastapi import HTTPException
    return HTTPException(503, str(message))


def make_router(state):
    router = APIRouter()
    import importlib
    credentials = importlib.import_module(f'{__package__}.credentials' if __package__ else 'credentials')
    router.include_router(credentials.make_router(state.credential_change_boundary))

    @router.get('/status')
    def status():
        return state.status(touch=True)

    @router.post('/heartbeat')
    def heartbeat(body: LeaseRequest | None = None):
        return state.heartbeat(body)

    @router.post('/start')
    def start(body: CaptureRequest):
        with state.control_boundary(body):
            return state.start(body.consent, body.backend, body.recording_id, body.transcription_model, body.audio_source,
                               new_meeting=body.new_meeting, transcription_provider=body.transcription_provider,
                               asr_mode=body.asr_mode)

    @router.get('/recordings')
    def recordings():
        return state.get_bridge().recordings()

    @router.post('/pause')
    def pause(body: LifecycleControlRequest | None = None):
        with state.control_boundary(body):
            return state.pause()

    @router.get('/models')
    def models():
        import importlib
        module = importlib.import_module(f'{__package__}.model_selection' if __package__ else 'model_selection')
        return module.available_models()

    @router.get('/devices')
    def devices(backend: Literal['native', 'macos'] = 'native'):
        # Enumeration must never replace the active capture object: /pause must
        # still target the process that actually owns the microphone/system stream.
        probe = MeetingState(audio_factory=state.audio_factory, macos_factory=state.macos_factory)
        return probe.get_audio(backend=backend).devices()

    @router.post('/assist')
    def assist(body: AssistRequest):
        return state.assist(body.question, body.project, body.web_query, body.all_projects, body.web_fallback, body.answer_provider, body.answer_model,
                            request_id=body.request_id, row_id=body.row_id, transcript=body.transcript)

    @router.get('/assist/{request_id}')
    def assist_request(request_id: str):
        from fastapi import HTTPException
        entry = state.assist_request(request_id[:100])
        if entry is None:
            raise HTTPException(404, 'Unknown answer request.')
        return entry

    @router.post('/assist/cancel')
    def assist_cancel(body: AssistCancelRequest):
        return state.cancel_assist(body.request_id)

    @router.get('/overlay/lifecycle')
    def overlay_lifecycle():
        with state.overlay_lock:
            return {'revision': state.overlay_revision, 'epoch': state.overlay_epoch}

    @router.post('/overlay/claim')
    def overlay_claim(body: OverlayClaimRequest):
        if body.client is None or body.creation is None or body.epoch is None:
            return {'accepted': False, 'reason': 'creation_order_required'}
        return state.overlay_claim(body.revision, client=body.client, creation=body.creation, epoch=body.epoch)

    @router.post('/overlay/show')
    def overlay_show(body: OverlayActionRequest):
        return state.overlay_show(body.scope, pane=body.pane, action=body.action)

    @router.post('/overlay/hide')
    def overlay_hide(body: OverlayActionRequest):
        return state.overlay_hide(pane=body.pane, action=body.action)

    @router.post('/overlay/release')
    def overlay_release(body: OverlayActionRequest):
        return state.overlay_release(body.pane, body.action)

    @router.post('/overlay/scope')
    def overlay_scope(body: OverlayScopeRequest):
        return state.overlay_scope(body.scope if isinstance(body.scope, dict) else {})

    @router.get('/projects')
    def projects():
        return state.get_engine().projects()

    @router.post('/clear')
    def clear(body: LifecycleControlRequest | None = None):
        with state.control_boundary(body):
            return clear_authorized()

    def clear_authorized():
        # Serialize with backend start/stop, which hold lifecycle_lock
        # across their whole operation: a start racing Clear either
        # publishes its bridge first (then Clear forgets via it) or waits
        # until Clear is done (then it observes the published outcome).
        # Without this, Clear could read state.bridge while a start has set
        # listening but not yet created the bridge, skip the forget boundary,
        # and restart capture onto unforgotten adapter state.
        with state.lifecycle_lock, state.overlay_clear_boundary(), state.lock:
            state.segments.clear()
            state.result = None
            state.error = None
            # Failed-request retry data holds the original question and
            # transcript: it must not survive explicit clear or a session
            # switch, or a later error could retry stale meeting content.
            state.error_request = None
            for entry in state.requests.values():
                if entry.get('status') in ('queued', 'streaming', 'running'):
                    entry['cancelled'] = True
                    entry['status'] = 'cancelled'
            # Retention: completed/cancelled/error slots hold Q&A + snippets and
            # remain readable via /assist/<id>. Purge them on explicit clear.
            for key in [k for k, e in state.requests.items()
                        if e.get('status') in ('done', 'error', 'cancelled')]:
                del state.requests[key]
            # A retiring tail window must not resurrect pre-clear speech:
            # late callbacks from the old capture stay rejected after Clear.
            # Retire the generation atomically with clearing: callbacks that
            # land between this lock release and pause() carry the old
            # generation and are rejected instead of appending.
            state.retiring_generations.clear()
            # Tell the Meetily bridge about the forget-everything boundary so
            # its next re-attach suppresses the whole existing snapshot.
            bridge = state.bridge
            if bridge is not None:
                forget = getattr(bridge, 'note_cleared', None)
                if callable(forget):
                    try:
                        forget()
                    except Exception:
                        state.meetily_clear_error = (
                            'Could not install the Meetily forget boundary. '
                            'Clear otherwise completed; try Clear again or start a new meeting.')
                    else:
                        state.meetily_clear_error = None
            was_listening = state.listening
            # Snapshot the generation and session the restart must still match:
            # a newer start (different settings, new generation) racing Clear
            # must never be stopped and replaced by this stale restart.
            clear_generation = state.capture_generation
            clear_session = state.session_key
            if was_listening:
                state.capture_generation += 1
            last_args = getattr(state, 'last_start_args', None)
            try:
                engine = state.engine
                invalidator = getattr(engine, 'invalidate_cache', None)
                if callable(invalidator):
                    invalidator()
                elif isinstance(getattr(engine, '_cache', None), dict):
                    engine._cache.clear()
            except Exception:
                pass
            state.busy = False
            state.generation += 1
        if was_listening and last_args and state.meetily_clear_error is None and state.overlay_cleanup_error is None:
            # Clear is a forget-everything boundary: stop the running capture
            # and start a new generation on the same session so in-flight
            # pre-clear audio can never resurrect cleared content, while live
            # capture continues without the pane leaving the listening state.
            # A failed forget-boundary install skips the restart: resuming
            # capture on top of unforgotten adapter state would resurrect the
            # cleared meeting, so stay paused with the error visible until
            # Clear (or a new meeting, which fails closed with a 503) lands it.
            # No tail window: stop-time audio belongs to the cleared meeting.
            # The generation was already retired under the state lock above,
            # so callbacks landing between clearing and pause stay rejected.
            # The whole restart is one lifecycle transaction: a concurrent
            # pause (or pane close) cannot slip between pause and start and
            # be overridden by the restart. Listening is re-checked inside:
            # a pause that won the race before this section means the user no
            # longer wants capture, so the restart is skipped.
            state.restart_after_clear(last_args, clear_generation, clear_session)
        elif was_listening:
            # The boundary install failed: stop capture and stay paused
            # instead of restarting on top of unforgotten adapter state.
            # No tail window — same as Clear's restart path. Like the
            # restart, this stale failure path must not stop a newer capture
            # that another thread started after Clear released the state
            # lock: pause only the generation and session Clear retired.
            with state.lifecycle_lock:
                if (state.listening
                        and state.capture_generation == clear_generation + 1
                        and state.session_key == clear_session):
                    state.pause(retain_tail=False)
        return state.status(touch=True)

    return router


state = MeetingState()
router = make_router(state)
