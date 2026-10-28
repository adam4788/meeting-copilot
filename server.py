"""Read-only meeting copilot backend. Importing never starts audio capture."""
import threading
import time
from collections import deque
from typing import Literal
from fastapi import APIRouter
from pydantic import BaseModel, Field


class AssistRequest(BaseModel):
    question: str = Field(default='', max_length=4000)
    request_id: str | None = Field(default=None, max_length=100)
    row_id: str | None = Field(default=None, max_length=200)
    project: str | None = Field(default=None, max_length=1000)
    all_projects: bool = False
    web_fallback: bool = True
    web_query: str | None = Field(default=None, max_length=300)
    answer_provider: str | None = Field(default=None, max_length=100)
    answer_model: str | None = Field(default=None, max_length=200)


class AssistCancelRequest(BaseModel):
    request_id: str = Field(max_length=100)


class OverlayScopeRequest(BaseModel):
    scope: dict = Field(default_factory=dict)


class CaptureRequest(BaseModel):
    consent: bool = False
    backend: Literal['meetily', 'native', 'macos'] = 'meetily'
    recording_id: str | None = Field(default=None, max_length=500)
    transcription_model: Literal['tiny.en', 'base.en', 'small.en'] = 'base.en'
    audio_source: Literal['microphone', 'system', 'both'] = 'microphone'


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
        self.engine_factory = engine_factory
        self.engine = None
        self.busy = False
        self.result = None
        self.requests = {}
        self.request_seq = 0
        self.generation = 0
        self.capture_generation = 0
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.overlay_lock = threading.RLock()
        self.capture_error = None
        self.segments = deque(maxlen=120)
        self.listening = False
        self.error = None
        self.overlay = None
        self.overlay_scope_value = {}

    def add_segment(self, segment):
        text = str(segment.get('text', '')).strip()[:4000]
        if text:
            with self.lock:
                item = {'speaker': str(segment.get('speaker', 'remote'))[:30], 'text': text,
                        'timestamp': segment.get('timestamp', time.time())}
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

    def get_audio(self, backend='native', transcription_model='base.en', audio_source='microphone'):
        with self.lock:
            options = (backend, transcription_model, audio_source)
            if self.audio is None or self.audio_options != options:
                if backend == 'macos':
                    if self.macos_factory:
                        factory = self.macos_factory
                    else:
                        import importlib
                        module = importlib.import_module(f'{__package__}.macos_audio' if __package__ else 'macos_audio')
                        factory = module.MacOSAudioCapture
                    self.audio = factory(model_size=transcription_model, source=audio_source)
                elif self.audio_factory:
                    self.audio = self.audio_factory()
                else:
                    import importlib
                    module = importlib.import_module(f'{__package__}.audio' if __package__ else 'audio')
                    self.audio = module.AudioCapture()
                self.audio_options = options
            return self.audio

    def start(self, consent, backend='meetily', recording_id=None, transcription_model='base.en', audio_source='microphone'):
        # Serialize the actual backend operation, not merely the UI state transition.
        # Status/segment callbacks take only self.lock, so stop/join cannot deadlock them.
        with self.lifecycle_lock:
            return self._start(consent, backend, recording_id, transcription_model, audio_source)

    def _start(self, consent, backend='meetily', recording_id=None, transcription_model='base.en', audio_source='microphone'):
        from fastapi import HTTPException
        if not consent:
            raise HTTPException(400, 'Confirm participant consent and audio capture before starting.')
        if backend == 'meetily' and not recording_id:
            raise HTTPException(400, 'Start a recording in Meetily, then explicitly select it here.')
        with self.lock:
            if self.listening:
                return {'listening': True}
            if backend in ('native', 'macos'):
                self.get_audio(backend, transcription_model, audio_source)
            session_key = (backend, recording_id if backend == 'meetily' else (transcription_model, audio_source))
            if self.session_key != session_key:
                self.segments.clear()
                self.result = None
                self.capture_info = {}
                self.generation += 1
                self.session_key = session_key
            self.backend = backend
            self.listening = True
            self.error = None
            self.last_seen = time.monotonic()
            self.capture_generation += 1
            capture_generation = self.capture_generation
        def segment_callback(segment):
            with self.lock:
                if self.listening and self.capture_generation == capture_generation:
                    self.add_segment(segment)
        def status_callback(info):
            with self.lock:
                if self.listening and self.capture_generation == capture_generation:
                    self.audio_status(info)
        try:
            if backend == 'meetily':
                self.get_bridge().attach(recording_id, on_segment=segment_callback, on_status=status_callback)
            else:
                self.audio.start(on_segment=segment_callback, on_status=status_callback)
        except Exception as exc:
            self.pause()
            raise HTTPException(503, f'Audio could not start: {type(exc).__name__}: {str(exc)[:300]}') from exc
        threading.Thread(target=self.watchdog, name='meeting-capture-lease', daemon=True).start()
        return {'listening': self.listening}

    def audio_status(self, info):
        with self.lock:
            self.capture_info = info if isinstance(info, dict) else {'message': str(info)}
            lifecycle = info.get('state', info.get('status')) if isinstance(info, dict) else None
            if isinstance(info, dict) and (info.get('listening') is False or lifecycle in ('error', 'stopped', 'completed', 'idle')):
                self.listening = False
            self.capture_error = str(info['error'])[:500] if isinstance(info, dict) and info.get('error') else None

    def watchdog(self):
        while self.listening:
            time.sleep(2)
            if time.monotonic() - self.last_seen > 30:
                self.pause()
                break

    def pause(self):
        with self.lifecycle_lock:
            with self.lock:
                self.listening = False
                self.capture_generation += 1
                audio = self.audio if self.backend in ('native', 'macos') else self.bridge
            if audio:
                audio.stop()
            with self.lock:
                for entry in self.requests.values():
                    if entry.get('status') in ('queued', 'streaming', 'running'):
                        entry['cancelled'] = True
                        entry['status'] = 'cancelled'
                self.busy = False
                self.capture_error = None
                self.capture_info = {'state': 'stopped', 'backend': self.backend, 'message': 'Paused / detached', 'error': None}
            return {'listening': False}

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

    def assist(self, question, project=None, web_query=None, all_projects=False, web_fallback=True, answer_provider=None, answer_model=None, request_id=None, row_id=None):
        from fastapi import HTTPException
        import secrets
        if all_projects and project:
            raise HTTPException(400, 'Choose all projects or one project, not both.')
        with self.lock:
            live = [entry for entry in self.requests.values() if entry.get('status') in ('queued', 'streaming', 'running')]
            if live:
                # Explicit row requests never hijack another row: 409 carries the
                # owner so the caller keeps attribution instead of polling /status.
                raise HTTPException(409, f"Request {live[0]['request_id']} is already being prepared.")
            transcript = '\n'.join(f"{s['speaker']}: {s['text']}" for s in list(self.segments)[-40:])[-16000:]
            if not question.strip():
                if not transcript:
                    raise HTTPException(400, 'Enter a question or start listening first.')
                question = 'Suggest a concise answer to the most recent question in this transcript.'
            self.request_seq += 1
            request_id = (str(request_id or '').strip() or f'r-{self.request_seq}-{secrets.token_hex(4)}')[:100]
            if request_id in self.requests:
                raise HTTPException(409, f'Request {request_id} is already being prepared.')
            if len(self.requests) >= 8:
                # Bound the slot table; drop only terminal entries, never live ones.
                terminal = [key for key, entry in self.requests.items() if entry.get('status') in ('done', 'error', 'cancelled')]
                for key in terminal[:len(self.requests) - 7]:
                    del self.requests[key]
                if len(self.requests) >= 8:
                    raise HTTPException(409, 'Too many pending answer requests.')
            entry = {'request_id': request_id, 'row_id': row_id, 'question': question[:4000],
                     'status': 'queued', 'answer': '', 'sources': [], 'warnings': [],
                     'web_search': {'used': False}, 'model_route': '', 'streaming': False,
                     'completed_only': True, 'error': None, 'cancelled': False, 'generation': self.generation}
            self.requests[request_id] = entry
            self.busy, self.error, self.result = True, None, None
            generation = self.generation

        def work(entry_ref):
            try:
                options = {}
                if answer_provider is not None or answer_model is not None:
                    options = {'answer_provider': answer_provider, 'answer_model': answer_model}
                engine = self.get_engine()
                stream_header, chunks = None, None
                try:
                    stream_header, chunks = engine.answer_stream(
                        question, transcript=transcript, project=project, web_query=web_query,
                        all_projects=all_projects, web_fallback=web_fallback, **options)
                except (NotImplementedError, AttributeError):
                    stream_header, chunks = None, None
                if chunks is None:
                    result = engine.answer(question, transcript=transcript, project=project, web_query=web_query, all_projects=all_projects, web_fallback=web_fallback, **options)
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
                            self.result = result
                    return
                with self.lock:
                    entry_ref['status'] = 'streaming'
                    entry_ref['streaming'] = True
                    entry_ref['completed_only'] = False
                    header = stream_header or {}
                    entry_ref['model_route'] = header.get('model_route', '')
                    entry_ref['sources'] = header.get('sources', [])
                    entry_ref['warnings'] = header.get('warnings', [])
                    entry_ref['web_search'] = header.get('web_search', {'used': False})
                for delta in chunks:
                    with self.lock:
                        if entry_ref.get('cancelled') or generation != self.generation:
                            entry_ref['status'] = 'cancelled'
                            return
                        if isinstance(delta, str) and delta:
                            entry_ref['answer'] = (entry_ref['answer'] + delta)[:12000]
                with self.lock:
                    if generation == self.generation and not entry_ref.get('cancelled'):
                        entry_ref['status'] = 'done'
                        self.result = {'answer': entry_ref['answer'], 'sources': entry_ref['sources'],
                                       'warnings': entry_ref['warnings'], 'web_search': entry_ref['web_search'],
                                       'model_route': entry_ref['model_route'], 'streaming': True,
                                       'completed_only': False}
                    elif entry_ref.get('cancelled'):
                        entry_ref['status'] = 'cancelled'
            except Exception as exc:
                with self.lock:
                    if entry_ref.get('cancelled'):
                        entry_ref['status'] = 'cancelled'
                    elif generation == self.generation:
                        entry_ref.update({'status': 'error', 'error': f'{type(exc).__name__}: {str(exc)[:500]}'})
                        self.error = f'{type(exc).__name__}: {str(exc)[:500]}'
            finally:
                with self.lock:
                    if not any(e.get('status') in ('queued', 'streaming', 'running') for e in self.requests.values()):
                        self.busy = False
        # Provider credentials are request/profile scoped; propagate that scope into the worker.
        # Native click handlers run on overlay threads, so the bridge snapshot
        # also carries the originating context with it (see BackendTransport).
        import contextvars
        context = contextvars.copy_context()
        entry = self.requests[request_id]
        threading.Thread(target=lambda: context.run(work, entry), name=f'meeting-answer-{request_id}', daemon=True).start()
        return {'accepted': True, 'request_id': request_id, 'streaming': None}

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
        project = scope.get('project')
        if isinstance(project, str) and project:
            cleaned['project'] = project[:1000]
        if scope.get('all_projects') is True:
            cleaned['all_projects'] = True
        if scope.get('web_fallback') is False:
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

    def overlay_show(self, scope):
        """Explicit pane action: backend-managed bridge owns one helper process."""
        import importlib
        with self.overlay_lock:
            cleaned = self._clean_overlay_scope(scope)
            self.overlay_scope_value = cleaned
            if self.overlay is not None:
                self.overlay.set_scope(cleaned)
                try:
                    self.overlay.update(self.status())
                except Exception:
                    pass
                return {'shown': True, 'reused': True, 'available': True}
            try:
                module = importlib.import_module(f'{__package__}.overlay_bridge' if __package__ else 'overlay_bridge')
            except ImportError:
                from fastapi import HTTPException
                raise HTTPException(503, 'Overlay helper is not installed in this backend.')
            bridge = module.OverlayBridge(helper=self._overlay_helper_path(), transport=module.BackendTransport(self))
            bridge.set_scope(cleaned)
            try:
                bridge.update(self.status())
            except Exception:
                pass
            result = bridge.show_overlay()
            # show_overlay returns a dict; fail closed when no helper could start.
            shown = bool(result.get('shown')) if isinstance(result, dict) else bool(result)
            if not shown:
                error = result.get('error') if isinstance(result, dict) else None
                raise _unavailable(error or 'Overlay helper is not prepared on this backend.')
            try:
                bridge.update(self.status())
            except Exception:
                pass
            self.overlay = bridge
            if isinstance(result, dict):
                out = dict(result)
                out.setdefault('available', True)
                return out
            return {'shown': True, 'reused': False, 'available': True}

    def overlay_hide(self):
        with self.overlay_lock:
            bridge, self.overlay = self.overlay, None
        if bridge is not None:
            try:
                bridge.hide_overlay()
            except Exception:
                pass
        return {'shown': False}

    def overlay_scope(self, scope):
        with self.overlay_lock:
            cleaned = self._clean_overlay_scope(scope)
            self.overlay_scope_value = cleaned
            bridge = self.overlay
        if bridge is not None:
            bridge.set_scope(cleaned)
            try:
                bridge.update(self.status())
            except Exception:
                pass
            return {'shown': True, 'scope': cleaned}
        return {'shown': False, 'scope': cleaned}

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

    def status(self):
        with self.lock:
            self.last_seen = time.monotonic()
            live = [dict(e) for e in self.requests.values() if e.get('status') in ('queued', 'streaming', 'running')]
            overlay_status = None
            if self.overlay is not None:
                try:
                    overlay_status = self.overlay.helper_status()
                except Exception:
                    overlay_status = None
            overlay_available = True
            if isinstance(overlay_status, dict):
                overlay_available = bool(overlay_status.get('available', True))
            return {'listening': self.listening, 'segments': list(self.segments), 'error': self.error or self.capture_error,
                    'busy': self.busy, 'result': self.result, 'requests': {e['request_id']: dict(e) for e in live},
                    'capture': self.capture_info, 'backend': self.backend, 'generation': self.generation,
                    'capture_generation': self.capture_generation,
                    'capabilities': {'all_projects': True, 'automatic_web_fallback': True, 'independent_models': True, 'macos_transcription': True,
                                     'overlay': overlay_available, 'overlay_stream': True, 'overlay_cancel': True}}


def _unavailable(message):
    from fastapi import HTTPException
    return HTTPException(503, str(message))


def make_router(state):
    router = APIRouter()

    @router.get('/status')
    def status():
        return state.status()

    @router.post('/start')
    def start(body: CaptureRequest):
        return state.start(body.consent, body.backend, body.recording_id, body.transcription_model, body.audio_source)

    @router.get('/recordings')
    def recordings():
        return state.get_bridge().recordings()

    @router.post('/pause')
    def pause():
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
                            request_id=body.request_id, row_id=body.row_id)

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

    @router.post('/overlay/show')
    def overlay_show(body: OverlayScopeRequest):
        return state.overlay_show(body.scope if isinstance(body.scope, dict) else {})

    @router.post('/overlay/hide')
    def overlay_hide():
        return state.overlay_hide()

    @router.post('/overlay/scope')
    def overlay_scope(body: OverlayScopeRequest):
        return state.overlay_scope(body.scope if isinstance(body.scope, dict) else {})

    @router.get('/projects')
    def projects():
        return state.get_engine().projects()

    @router.post('/clear')
    def clear():
        with state.lock:
            state.segments.clear()
            state.result = None
            state.error = None
            for entry in state.requests.values():
                if entry.get('status') in ('queued', 'streaming', 'running'):
                    entry['cancelled'] = True
                    entry['status'] = 'cancelled'
            state.busy = False
            state.generation += 1
        return state.status()

    return router


state = MeetingState()
router = make_router(state)
