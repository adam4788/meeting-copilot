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
import urllib.error
import urllib.request
import uuid

REALTIME_URL = 'wss://api.meta.ai/v1/asr/realtime'
TRANSCRIBE_URL = 'https://api.meta.ai/v1/asr/transcribe'
DEFAULT_MODEL = os.environ.get('ASR_MODEL', 'muse-voice-transcribe-1.0')
ASR_MODES = ('ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK')
MAX_TEXT = 8000


class MetaAsrError(RuntimeError):
    """Sanitized ASR failure (never carries key material)."""


class _RedirectBlocked(Exception):
    pass


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
        raise MetaAsrError('Unsupported ASR mode')
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
            raise MetaAsrError('Meta ASR provider returned an error')
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
    handshake = json.dumps(build_handshake(api_key, model, mode))
    if connect is None:
        try:
            import websockets
            version = int(getattr(websockets, '__version__').split('.')[0])
            if not 12 <= version < 16:
                raise ValueError()

            # Use each version's default connector so existing transport and
            # proxy behavior stays intact. Override its redirect hook only on
            # this connector, never on the library's global defaults.
            if version >= 14:
                from websockets.asyncio.client import connect as modern_connect
                from websockets.exceptions import InvalidStatus
                if not callable(getattr(modern_connect, 'process_redirect', None)):
                    raise ValueError()

                class NoRedirectModern(modern_connect):
                    def process_redirect(self, exc):
                        if isinstance(exc, InvalidStatus) and 300 <= exc.response.status_code < 400:
                            return _RedirectBlocked()
                        return exc
                connector_cls = NoRedirectModern
            else:
                from websockets.legacy.client import connect as legacy_connect
                if not callable(getattr(legacy_connect, 'handle_redirect', None)):
                    raise ValueError()

                class NoRedirectLegacy(legacy_connect):
                    def handle_redirect(self, uri):
                        raise _RedirectBlocked()
                connector_cls = NoRedirectLegacy
        except Exception:
            raise MetaAsrError('Meta ASR requires websockets >=12,<16 with redirect hook support') from None
        session_id = f'mc-{uuid.uuid4()}'
        connect = lambda: connector_cls(  # noqa: E731
            f'{REALTIME_URL}?sessionId={session_id}', open_timeout=30)
    mapper = EventMapper(default_speaker)
    try:
        async with connect() as ws:
            await ws.send(handshake)
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
    except asyncio.CancelledError:
        raise
    except _RedirectBlocked:
        raise MetaAsrError('Meta ASR WebSocket redirects are not allowed') from None
    except Exception:
        raise MetaAsrError('Meta ASR realtime transcription failed; check the connection and retry') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transcribe_file(path, api_key, model=DEFAULT_MODEL, mode='ENDPOINTING',
                    opener=None):
    """One-shot file transcription via POST /v1/asr/transcribe.

    Returns a list of segment dicts. `opener` is injectable for tests
    (a callable taking a urllib Request and returning a response with
    .read()). The WAV must be mono 16-bit PCM at 16/24 kHz.
    """
    import wave
    if not api_key:
        raise MetaAsrError('Meta ASR requires MODEL_API_KEY')
    with open(path, 'rb') as handle:
        audio = handle.read()
    try:
        with wave.open(path, 'rb') as wav:
            rate = wav.getframerate()
    except Exception:
        raise MetaAsrError('Meta ASR file transcription requires a WAV file') from None
    if rate not in (16000, 24000):
        raise MetaAsrError('Meta ASR requires 16 kHz or 24 kHz WAV audio')
    boundary = f'mc{uuid.uuid4().hex}'
    # The file endpoint takes the WAV container as-is: audioEncoding is
    # 'WAV' here (PCM_16KHZ/PCM_24KHZ are realtime stream encodings).
    request_json = json.dumps({'model': model or DEFAULT_MODEL, 'mode': mode,
                               'audioEncoding': 'WAV',
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
    open_fn = opener if opener is not None else urllib.request.build_opener(_NoRedirect()).open
    try:
        with open_fn(request) as response:
            payload = json.loads(response.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as exc:
        is_redirect = 300 <= exc.code < 400
        exc.close()
        if is_redirect:
            raise MetaAsrError('Meta ASR file transcription redirects are not allowed') from None
        raise MetaAsrError('Meta ASR file transcription failed; check the connection and retry') from None
    except Exception:
        raise MetaAsrError('Meta ASR file transcription failed; check the connection and retry') from None
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
