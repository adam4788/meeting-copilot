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
    web_query: str | None = Field(default=None, max_length=300)


class CaptureRequest(BaseModel):
    consent: bool = False
    backend: Literal['meetily', 'native'] = 'meetily'
    recording_id: str | None = Field(default=None, max_length=500)


class MeetingState:
    def __init__(self, engine_factory=None, audio_factory=None, bridge_factory=None):
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

    def get_audio(self):
        with self.lock:
            if self.audio is None:
                if self.audio_factory:
                    self.audio = self.audio_factory()
                else:
                    import importlib
                    module = importlib.import_module(f'{__package__}.audio' if __package__ else 'audio')
                    self.audio = module.AudioCapture()
            return self.audio

    def start(self, consent, backend='meetily', recording_id=None):
        # Serialize the actual backend operation, not merely the UI state transition.
        # Status/segment callbacks take only self.lock, so stop/join cannot deadlock them.
        with self.lifecycle_lock:
            return self._start(consent, backend, recording_id)

    def _start(self, consent, backend='meetily', recording_id=None):
        from fastapi import HTTPException
        if not consent:
            raise HTTPException(400, 'Confirm participant consent and audio capture before starting.')
        if backend == 'meetily' and not recording_id:
            raise HTTPException(400, 'Start a recording in Meetily, then explicitly select it here.')
        with self.lock:
            if self.listening:
                return {'listening': True}
            if backend == 'native':
                self.get_audio()
            session_key = (backend, recording_id if backend == 'meetily' else None)
            if self.session_key != session_key:
                self.segments.clear()
                self.result = None
                self.capture_info = {}
                self.generation += 1
                self.session_key = session_key
            self.capture_generation += 1
            capture_generation = self.capture_generation
            self.backend = backend
            self.listening = True
            self.error = None
            self.last_seen = time.monotonic()
        # Stops are bounded: a previous worker may still return after detach/restart.
        # Check the lease and apply each callback under the same lock so it cannot
        # race with pause or contaminate a newly selected recording.
        def deliver(callback, value):
            with self.lock:
                if capture_generation == self.capture_generation:
                    callback(value)

        on_segment = lambda value: deliver(self.add_segment, value)
        on_status = lambda value: deliver(self.audio_status, value)
        try:
            if backend == 'meetily':
                self.get_bridge().attach(recording_id, on_segment=on_segment, on_status=on_status)
            else:
                self.audio.start(on_segment=on_segment, on_status=on_status)
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
                audio = self.audio if self.backend == 'native' else self.bridge
            if audio:
                audio.stop()
            with self.lock:
                self.capture_error = None
                self.capture_info = {'status': 'idle', 'backend': self.backend,
                                     'message': 'Not attached' if self.backend == 'meetily' else 'Paused'}
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

    def assist(self, question, project=None, web_query=None):
        from fastapi import HTTPException
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
                result = self.get_engine().answer(question, transcript=transcript, project=project, web_query=web_query)
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
                    'busy': self.busy, 'result': self.result, 'capture': self.capture_info, 'backend': self.backend}


def make_router(state):
    router = APIRouter()

    @router.get('/status')
    def status():
        return state.status()

    @router.post('/start')
    def start(body: CaptureRequest):
        return state.start(body.consent, body.backend, body.recording_id)

    @router.get('/recordings')
    def recordings():
        return state.get_bridge().recordings()

    @router.post('/pause')
    def pause():
        return state.pause()

    @router.get('/devices')
    def devices():
        return state.get_audio().devices()

    @router.post('/assist')
    def assist(body: AssistRequest):
        return state.assist(body.question, body.project, body.web_query)

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
