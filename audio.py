"""Opt-in local meeting audio. Import/construction never opens devices."""
import queue
import threading
import time


class AudioCapture:
    def __init__(self, *, input_id=None, output_id=None, model_size='base.en',
                 chunk_seconds=5.0, queue_size=4, model_factory=None, backend=None):
        if not isinstance(queue_size, int) or isinstance(queue_size, bool) or not 1 <= queue_size <= 32:
            raise ValueError('queue_size must be an integer from 1 to 32')
        if not isinstance(chunk_seconds, (int, float)) or not .2 <= chunk_seconds <= 30:
            raise ValueError('chunk_seconds must be between 0.2 and 30')
        self.input_id, self.output_id = input_id, output_id
        self.chunk_seconds = chunk_seconds
        self._model_factory = model_factory
        self._model_size = model_size
        self._model = None
        self._model_lock = threading.Lock()
        self._backend = backend
        self._state = 'idle'
        self._lock = threading.RLock()
        self._error = None
        self._callback_error = None
        self._threads = []
        self._stop = threading.Event()
        self._queue = queue.Queue(maxsize=queue_size)
        self._dropped = 0
        self._on_segment = lambda segment: None
        self._on_status = lambda status: None

    def _audio_backend(self):
        if self._backend is None:
            import soundcard
            self._backend = soundcard
        return self._backend

    def devices(self):
        backend = self._audio_backend()
        mic, out = backend.default_microphone(), backend.default_speaker()
        return {'inputs': [{'id': d.id, 'name': d.name} for d in backend.all_microphones()],
                'outputs': [{'id': d.id, 'name': d.name} for d in backend.all_speakers()],
                'default_input': mic.id if mic else None,
                'default_output': out.id if out else None}

    def warmup(self):
        """Explicit opt-in: may download model weights; never captures audio."""
        with self._model_lock:
            if self._model is None:
                factory = self._model_factory
                if factory is None:
                    from faster_whisper import WhisperModel
                    factory = WhisperModel
                self._model = factory(self._model_size, device='cpu', compute_type='int8', cpu_threads=4)
            return self._model

    def status(self):
        with self._lock:
            return {'state': self._state,
                    'running': any(t.is_alive() for t in self._threads),
                    'callback_error': self._callback_error,
                    'error': self._error, 'queued': self._queue.qsize(), 'dropped': self._dropped}

    def _notify(self, state, error=None):
        with self._lock:
            if state == 'running' and self._stop.is_set():
                return
            self._state, self._error = state, error
        try:
            self._on_status(self.status())
        except Exception as exc:
            with self._lock:
                self._callback_error = f'{type(exc).__name__}: {exc}'

    def start(self, on_segment, on_status=None):
        """Explicit opt-in; model loading is asynchronous. Callbacks are worker-thread calls."""
        if not callable(on_segment) or (on_status is not None and not callable(on_status)):
            raise TypeError('on_segment and on_status must be callable')
        with self._lock:
            if any(t.is_alive() for t in self._threads):
                raise RuntimeError('AudioCapture is already active or still stopping')
            self._on_segment = on_segment
            self._on_status = on_status or (lambda status: None)
            self._stop.clear()
            self._state, self._error = 'starting', None
            self._callback_error, self._dropped = None, 0
            self._discard_pending()
            worker = threading.Thread(target=self._run, name='meeting-transcription', daemon=True)
            self._threads = [worker]
            worker.start()

    def _run(self):
        try:
            self.warmup()
            if self._stop.is_set():
                return
            backend = self._audio_backend()
            input_id = self.input_id or backend.default_microphone().id
            output_id = self.output_id or backend.default_speaker().id
            sources = [('you', backend.get_microphone(id=input_id)),
                       ('remote', backend.get_microphone(id=output_id, include_loopback=True))]
            for (speaker, device), expected in zip(sources, (input_id, output_id)):
                if device is None or device.id != expected:
                    raise ValueError(f'{speaker}: exact device ID unavailable: {expected}')
            for speaker, device in sources:
                thread = threading.Thread(target=self._capture, args=(speaker, device),
                                          name='meeting-audio-' + speaker, daemon=True)
                with self._lock:
                    if self._stop.is_set():
                        return
                    self._threads.append(thread)
                    thread.start()
            self._notify('running')
            while not self._stop.is_set():
                try:
                    speaker, samples, timestamp = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                segments, _ = self._model.transcribe(samples, language='en', beam_size=1,
                                                     vad_filter=True, condition_on_previous_text=False)
                text = ' '.join(s.text.strip() for s in segments).strip()
                if text and not self._stop.is_set():
                    self._on_segment({'speaker': speaker, 'text': text, 'timestamp': timestamp})
        except Exception as exc:
            self._stop.set()
            self._notify('error', f'{type(exc).__name__}: {exc}')
        finally:
            self._discard_pending()

    def _enqueue(self, item):
        with self._lock:
            if self._stop.is_set():
                return
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except queue.Empty:
                    pass
            self._queue.put_nowait(item)

    def _capture(self, speaker, device):
        import numpy as np
        try:
            if self._stop.is_set():
                return
            # All channels avoids SoundCard's Windows single-channel capture bug.
            with device.recorder(samplerate=16000, blocksize=1600) as recorder:
                parts, count, timestamp = [], 0, time.time()
                while not self._stop.is_set():
                    block = recorder.record(numframes=1600)
                    mono = np.asarray(block, dtype=np.float32).mean(axis=1)
                    parts.append(mono)
                    count += len(mono)
                    if count >= int(self.chunk_seconds * 16000):
                        self._enqueue((speaker, np.concatenate(parts), timestamp))
                        parts, count, timestamp = [], 0, time.time()
        except Exception as exc:
            self._stop.set()
            self._notify('error', f'{speaker}: {type(exc).__name__}: {exc}')

    def _discard_pending(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self, timeout=3.0):
        """Bounded wait; pending CTranslate2 inference/download cannot be interrupted."""
        with self._lock:
            self._stop.set()
            threads = list(self._threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        self._discard_pending()
        if self._state != 'error':
            self._notify('stopping' if self.status()['running'] else 'stopped')
