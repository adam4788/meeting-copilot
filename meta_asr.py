"""Meta Muse Voice Transcribe client (opt-in cloud ASR).

Implements the Meta Model API speech-to-text surface documented at
https://dev.meta.ai/docs/speech-to-text : a realtime WebSocket
(wss://api.meta.ai/v1/asr/realtime, PCM 24 kHz, first-frame handshake
carrying the key) and a multipart file endpoint
(POST https://api.meta.ai/v1/asr/transcribe).

Privacy: audio leaves the device only when a caller explicitly selects
this provider. The API key is read from MODEL_API_KEY by callers and is
never logged or embedded in error messages.
"""
import asyncio
import json
import os
import uuid

REALTIME_URL = 'wss://api.meta.ai/v1/asr/realtime'
TRANSCRIBE_URL = 'https://api.meta.ai/v1/asr/transcribe'
DEFAULT_MODEL = os.environ.get('ASR_MODEL', 'muse-voice-transcribe-1.0')
ASR_MODES = ('ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK')
MAX_TEXT = 8000


class MetaAsrError(RuntimeError):
    """Sanitized ASR failure (never carries key material)."""


def resample_to_24k(samples):
    """16 kHz float32 mono -> 24 kHz int16 PCM bytes (linear interp)."""
    import numpy as np
    values = np.asarray(samples, dtype=np.float32).ravel()
    if values.size == 0:
        return b''
    target = int(round(values.size * 1.5))
    if target <= 1:
        pcm = np.clip(values[:1], -1.0, 1.0)
    else:
        positions = np.linspace(0, values.size - 1, target)
        pcm = np.interp(positions, np.arange(values.size), values)
    pcm = np.clip(pcm, -1.0, 1.0)
    return (pcm * 32767.0).astype('<i2').tobytes()


def build_handshake(api_key, model=DEFAULT_MODEL, mode='ENDPOINTING', language='en'):
    if not api_key:
        raise MetaAsrError('Meta ASR requires MODEL_API_KEY')
    if mode not in ASR_MODES:
        raise MetaAsrError(f'Unsupported ASR mode: {mode}')
    return {
        'authorization': {'accessToken': f'Bearer {api_key}'},
        'audioEncoding': 'PCM_24KHZ',
        'model': model or DEFAULT_MODEL,
        'mode': mode,
        'partialMode': 'CUMULATIVE',
        'emitAudioProgress': False,
        'languageBias': ['English'] if language == 'en' else [language],
    }


class EventMapper:
    """Maps Meta ASR events to worker segment dicts.

    Partials and turn-boundary markers are not segments: only final
    transcripts (PUSH_TO_TALK) and speechComplete events become text.
    Speaker labels from DIARIZATION are session-scoped ('A', 'B', ...).
    """

    def __init__(self, default_speaker='you'):
        self.default_speaker = default_speaker
        self._turn_speaker = {}
        self._current_turn = None

    def map(self, event, stamp):
        if not isinstance(event, dict):
            return None
        kind = event.get('type')
        if kind == 'error':
            raise MetaAsrError(str(event.get('message') or 'Meta ASR error')[:200])
        if kind == 'speechStart':
            self._current_turn = event.get('turnId')
            return None
        if kind == 'speaker':
            # Speaker events carry no turnId: they label the audio behind
            # them, i.e. the turn opened by the most recent speechStart.
            label = str(event.get('label') or '').strip()
            if label:
                turn = event.get('turnId', self._current_turn)
                self._turn_speaker[turn] = f'{self.default_speaker} \u00b7 Speaker {label}'
            return None
        text = None
        if kind == 'speechComplete':
            text = str(event.get('transcript') or '').strip()
        elif kind == 'transcript' and event.get('final'):
            text = str(event.get('transcript') or '').strip()
        if not text:
            return None
        # Preserve the audio-source identity (you/remote) and append the
        # diarization label: consumers must never lose which source a
        # labeled turn came from.
        speaker = self._turn_speaker.get(event.get('turnId'), self.default_speaker)
        return {'type': 'segment', 'speaker': speaker,
                'text': text[:MAX_TEXT], 'timestamp': stamp}


async def run_session(pcm_queue, emit, api_key, model=DEFAULT_MODEL,
                      mode='ENDPOINTING', default_speaker='you',
                      connect=None, clock=None):
    """Stream PCM bytes from an asyncio.Queue to Meta ASR.

    Queue items are 24 kHz int16 PCM byte frames; None ends input
    (endStream) and drains remaining events until the server closes.
    `connect` is injectable for tests: an async context manager factory
    compatible with websockets.connect.
    """
    import time as _time
    clock = clock or _time.time
    if connect is None:
        import websockets
        session_id = f'mc-{uuid.uuid4()}'
        connect = lambda: websockets.connect(  # noqa: E731
            f'{REALTIME_URL}?sessionId={session_id}', open_timeout=30)
    mapper = EventMapper(default_speaker)
    async with connect() as ws:
        await ws.send(json.dumps(build_handshake(api_key, model, mode)))
        ack = json.loads(await ws.recv())
        if not isinstance(ack, dict) or 'sessionId' not in ack:
            raise MetaAsrError('Meta ASR handshake failed')

        async def sender():
            while True:
                frame = await pcm_queue.get()
                if frame is None:
                    await ws.send(json.dumps({'type': 'endStream'}))
                    return
                await ws.send(frame)

        send_task = asyncio.ensure_future(sender())
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    continue
                segment = mapper.map(json.loads(message), clock())
                if segment is not None:
                    emit(segment)
        finally:
            send_task.cancel()


def transcribe_file(path, api_key, model=DEFAULT_MODEL, mode='ENDPOINTING',
                    opener=None):
    """One-shot file transcription via POST /v1/asr/transcribe.

    Returns a list of segment dicts. `opener` is injectable for tests
    (a callable taking a urllib Request and returning a response with
    .read()). The WAV must be mono 16-bit PCM at 16/24 kHz.
    """
    import urllib.request
    if not api_key:
        raise MetaAsrError('Meta ASR requires MODEL_API_KEY')
    with open(path, 'rb') as handle:
        audio = handle.read()
    boundary = f'mc{uuid.uuid4().hex}'
    request_json = json.dumps({'model': model or DEFAULT_MODEL, 'mode': mode,
                               'languageBias': ['English']}).encode()
    body = b''.join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="request"\r\n'
        f'Content-Type: application/json\r\n\r\n'.encode(), request_json, b'\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; '
        f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
        audio, b'\r\n', f'--{boundary}--\r\n'.encode(),
    ])
    request = urllib.request.Request(
        TRANSCRIBE_URL, data=body, method='POST',
        headers={'Authorization': f'Bearer {api_key}',
                 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request) as response:
            payload = json.loads(response.read().decode('utf-8', 'replace'))
    except MetaAsrError:
        raise
    except Exception as exc:
        raise MetaAsrError(f'Meta ASR file transcription failed ({type(exc).__name__})') from None
    segments = []
    for turn in payload.get('turns') or []:
        text = str(turn.get('transcript') or '').strip()
        if text:
            label = turn.get('speaker') or turn.get('label')
            segments.append({'type': 'segment',
                             'speaker': f'you \u00b7 Speaker {label}' if label else 'you',
                             'text': text[:MAX_TEXT], 'timestamp': 0.0})
    if not segments and payload.get('transcript'):
        segments.append({'type': 'segment', 'speaker': 'you',
                         'text': str(payload['transcript']).strip()[:MAX_TEXT],
                         'timestamp': 0.0})
    return segments
