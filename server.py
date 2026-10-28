"""Read-only meeting copilot backend. Importing never starts audio capture."""
import threading
import time
from collections import deque
from typing import Literal
from fastapi import APIRouter
from pydantic import BaseModel, Field


class AssistRequest(BaseModel):
    question: str = Field(default='', max_length=4000)
    project: str | None = Field(default=None, max_length=1000)
    all_projects: bool = False
    web_fallback: bool = True
    web_query: str | None = Field(default=None, max_length=300)
    answer_provider: str | None = Field(default=None, max_length=100)
    answer_model: str | None = Field(default=None, max_length=200)


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
        self.generation = 0
        self.capture_generation = 0
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.capture_error = None
        self.segments = deque(maxlen=120)
        self.listening = False
        self.error = None

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

    def assist(self, question, project=None, web_query=None, all_projects=False, web_fallback=True, answer_provider=None, answer_model=None):
        from fastapi import HTTPException
        if all_projects and project:
            raise HTTPException(400, 'Choose all projects or one project, not both.')
        with self.lock:
            if self.busy:
                raise HTTPException(409, 'An answer is already being prepared.')
            transcript = '\n'.join(f"{s['speaker']}: {s['text']}" for s in list(self.segments)[-40:])[-16000:]
            if not question.strip():
                if not transcript:
                    raise HTTPException(400, 'Enter a question or start listening first.')
                question = 'Suggest a concise answer to the most recent question in this transcript.'
            self.busy, self.error, self.result = True, None, None
            generation = self.generation

        def work():
            try:
                options = {}
                if answer_provider is not None or answer_model is not None:
                    options = {'answer_provider': answer_provider, 'answer_model': answer_model}
                result = self.get_engine().answer(question, transcript=transcript, project=project, web_query=web_query, all_projects=all_projects, web_fallback=web_fallback, **options)
                with self.lock:
                    if generation == self.generation:
                        self.result = result
            except Exception as exc:
                with self.lock:
                    if generation == self.generation:
                        self.error = f'{type(exc).__name__}: {str(exc)[:500]}'
            finally:
                with self.lock:
                    self.busy = False
        # Provider credentials are request/profile scoped; propagate that scope into the worker.
        import contextvars
        context = contextvars.copy_context()
        threading.Thread(target=lambda: context.run(work), name='meeting-answer', daemon=True).start()
        return {'accepted': True}

    def status(self):
        with self.lock:
            self.last_seen = time.monotonic()
            return {'listening': self.listening, 'segments': list(self.segments), 'error': self.error or self.capture_error,
                    'busy': self.busy, 'result': self.result, 'capture': self.capture_info, 'backend': self.backend,
                    'capabilities': {'all_projects': True, 'automatic_web_fallback': True, 'independent_models': True, 'macos_transcription': True}}


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
        return state.assist(body.question, body.project, body.web_query, body.all_projects, body.web_fallback, body.answer_provider, body.answer_model)

    @router.get('/projects')
    def projects():
        return state.get_engine().projects()

    @router.post('/clear')
    def clear():
        with state.lock:
            state.segments.clear()
            state.result = None
            state.error = None
            state.generation += 1
        return state.status()

    return router


state = MeetingState()
router = make_router(state)
