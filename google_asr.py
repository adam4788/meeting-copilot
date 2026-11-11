"""Explicit, bounded Gemini Transcribe Live preview transport.

Contract: https://ai.google.dev/gemini-api/docs/live-api/live-transcribe
Header authentication matches the official googleapis/python-genai live client.
No ambient credentials, SDK defaults, alternate origins, retries or audio files.
The caller owns scoped key admission and a conservative shared budget reservation.
"""
import asyncio
import base64
import json
import io
import wave
import logging
import math
import time

REALTIME_URL = ('wss://generativelanguage.googleapis.com/ws/'
                'google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent')
DEFAULT_MODEL = 'gemini-3.5-transcribe-live'
ASR_MODES = ('ENDPOINTING',)
MAX_AUDIO_SECONDS = 60
MAX_WALL_SECONDS = 75
MAX_OUTPUT_TOKENS = 256
MAX_FINAL_EVENTS = 32
MAX_OUTPUT_CHARACTERS = 8192
MAX_FRAME_BYTES = 32000
CHUNK_BYTES = 3200
MAX_MESSAGE_BYTES = 32768
MAX_RECEIVED_BYTES = 1048576
MAX_MESSAGES = 4096
OPEN_TIMEOUT = 10
SEND_TIMEOUT = 3
DRAIN_TIMEOUT = 5
CLOSE_TIMEOUT = 2
MAX_REPORTED_INPUT_TOKENS = 32768
MAX_REPORTED_OUTPUT_TOKENS = 8192

_MESSAGES = {
    'google_auth': 'Google transcription needs its plugin-scoped API key. Configure it securely and reopen the pane.',
    'google_runtime': 'Google transcription needs the reviewed websockets 15 runtime.',
    'google_limit': 'Google transcription preview limit reached. Capture stopped; review usage before another explicit test.',
    'google_transcription': 'Google transcription was not completed. Check the connection and review the partial transcript before retrying.',
}


class GoogleAsrError(RuntimeError):
    """Fixed code/message only; never provider text, URLs, audio or credentials."""
    def __init__(self, code='google_transcription'):
        self.code = code if code in _MESSAGES else 'google_transcription'
        super().__init__(_MESSAGES[self.code])


class _RedirectBlocked(Exception):
    pass


def validate(model=DEFAULT_MODEL, mode='ENDPOINTING'):
    if model != DEFAULT_MODEL or mode not in ASR_MODES:
        raise GoogleAsrError()
    return model, mode


def _validate_key(key):
    if (not isinstance(key, str) or not 1 <= len(key) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in key)
            or key.startswith('auth_tokens/')):
        raise GoogleAsrError('google_auth')


def _connector_class():
    try:
        import websockets
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        if int(websockets.__version__.split('.')[0]) != 15 or not callable(getattr(connect, 'process_redirect', None)):
            raise ValueError()

        class FixedOriginConnect(connect):
            def process_redirect(self, exc):
                if isinstance(exc, InvalidStatus) and 300 <= exc.response.status_code < 400:
                    return _RedirectBlocked()
                return exc
        return FixedOriginConnect
    except Exception:
        raise GoogleAsrError('google_runtime') from None


def runtime_available():
    """Import/version contract only; no connection or credential lookup."""
    try:
        _connector_class()
        return True
    except GoogleAsrError:
        return False


def _connect(key):
    # An isolated disabled logger prevents websocket DEBUG header/body dumps.
    logger = logging.Logger('meeting-copilot-google-transport')
    logger.disabled = True
    logger.propagate = False
    return _connector_class()(REALTIME_URL, additional_headers={'x-goog-api-key': key},
                              proxy=None, compression=None, logger=logger,
                              max_size=MAX_MESSAGE_BYTES, max_queue=8,
                              open_timeout=OPEN_TIMEOUT, close_timeout=CLOSE_TIMEOUT,
                              ping_interval=20, ping_timeout=10)


def build_handshake(model=DEFAULT_MODEL, mode='ENDPOINTING'):
    validate(model, mode)
    return {'setup': {'model': 'models/' + DEFAULT_MODEL,
                      'generationConfig': {'responseModalities': ['TEXT'], 'maxOutputTokens': MAX_OUTPUT_TOKENS},
                      'inputAudioTranscription': {'languageCodes': []}}}


def pcm16(samples):
    """Bounded 16 kHz float mono samples to little-endian signed PCM16."""
    import numpy as np
    values = np.asarray(samples, dtype=np.float32)
    if values.ndim != 1 or values.size > MAX_FRAME_BYTES // 2 or not np.isfinite(values).all():
        raise GoogleAsrError()
    return (np.clip(values, -1, 1) * 32767).astype('<i2').tobytes()


def _parse(message):
    if not isinstance(message, (str, bytes)):
        raise GoogleAsrError()
    size = len(message.encode('utf-8')) if isinstance(message, str) else len(message)
    if size > MAX_MESSAGE_BYTES:
        raise GoogleAsrError('google_limit')
    try:
        event = json.loads(message)
    except Exception:
        raise GoogleAsrError() from None
    if not isinstance(event, dict):
        raise GoogleAsrError()
    if 'error' in event:
        raise GoogleAsrError()
    return event, size


class EventMapper:
    """Only the dedicated ASR's final inputTranscription becomes a segment.

    Returned token counts are the latest provider report, not an invoice or an
    assumed session total. Sums of reports are conservative safety counters;
    reports may overlap. Missing fields never become fabricated zero usage.
    """
    def __init__(self, default_speaker='you'):
        if default_speaker not in ('you', 'remote'):
            raise GoogleAsrError()
        self.speaker = default_speaker
        self.report = {'audio_bytes': 0, 'audio_seconds': 0.0, 'final_segments': 0,
                       'output_characters': 0, 'usage_available': False,
                       'input_tokens': None, 'output_tokens': None, 'total_tokens': None,
                       'usage_reports': 0, 'reported_input_tokens_sum': 0,
                       'reported_output_tokens_sum': 0, 'drain_confirmed': False, 'tail_confirmed': False, 'drain_complete': False}

    def map(self, event, stamp):
        if not isinstance(event, dict) or 'error' in event or 'goAway' in event or 'toolCall' in event:
            raise GoogleAsrError()
        usage = event.get('usageMetadata')
        if usage is not None:
            if not isinstance(usage, dict):
                raise GoogleAsrError()
            for name, dest, maximum in [('promptTokenCount', 'input_tokens', MAX_REPORTED_INPUT_TOKENS),
                                         ('responseTokenCount', 'output_tokens', MAX_REPORTED_OUTPUT_TOKENS),
                                         ('totalTokenCount', 'total_tokens', MAX_REPORTED_INPUT_TOKENS + MAX_REPORTED_OUTPUT_TOKENS)]:
                count = usage.get(name)
                if count is not None and (type(count) is not int or count < 0):
                    raise GoogleAsrError()
                if count is not None and count > maximum:
                    raise GoogleAsrError('google_limit')
                self.report[dest] = count
            self.report['usage_reports'] += 1
            self.report['usage_available'] = all(self.report[name] is not None for name in ('input_tokens', 'output_tokens', 'total_tokens'))
            for name, maximum in [('input', MAX_REPORTED_INPUT_TOKENS), ('output', MAX_REPORTED_OUTPUT_TOKENS)]:
                count = self.report[name + '_tokens']
                if count is not None:
                    self.report['reported_' + name + '_tokens_sum'] += count
                    if self.report['reported_' + name + '_tokens_sum'] > maximum:
                        raise GoogleAsrError('google_limit')
        content = event.get('serverContent')
        if content is None:
            return None
        if not isinstance(content, dict) or 'modelTurn' in content or 'outputTranscription' in content:
            raise GoogleAsrError()
        final = content.get('inputTranscription')
        if final is None:
            return None
        if not isinstance(final, dict) or not isinstance(final.get('text'), str):
            raise GoogleAsrError()
        if final.get('finished') is False:
            return None
        text = final['text'].strip()
        if not text:
            return None
        # Do not truncate a transcript and silently call it complete.
        if (self.report['final_segments'] >= MAX_FINAL_EVENTS
                or self.report['output_characters'] + len(text) > MAX_OUTPUT_CHARACTERS):
            raise GoogleAsrError('google_limit')
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise GoogleAsrError()
        self.report['final_segments'] += 1
        self.report['output_characters'] += len(text)
        return {'type': 'segment', 'speaker': self.speaker, 'text': text, 'timestamp': stamp}


async def _bounded(awaitable, timeout):
    # Python 3.11's timeout context preserves external cancellation, including
    # cancellation racing with a just-completed websocket send/receive.
    async with asyncio.timeout(timeout):
        return await awaitable


async def _close(ws):
    try:
        await _bounded(ws.close(), timeout=CLOSE_TIMEOUT)
    except BaseException:
        # Force local transport retirement, without assuming provider receipt.
        transport = getattr(ws, 'transport', None)
        if transport is not None:
            transport.abort()
        raise


async def run_session(pcm_queue, emit, api_key, model=DEFAULT_MODEL,
                      mode='ENDPOINTING', default_speaker='you', *, connect=None,
                      clock=None, max_audio_seconds=MAX_AUDIO_SECONDS,
                      usage_callback=None):
    """Explicit PCM preview, no retry/rollover; caller must reserve its budget.

    `connect` is a no-argument test-only async-context-manager factory. None
    ends input. A bounded grace period collects final transcription events;
    the ASR contract has no documented final-tail acknowledgment. Reports
    therefore never claim a confirmed complete tail.
    """
    validate(model, mode)
    _validate_key(api_key)
    if (type(max_audio_seconds) not in (int, float) or not math.isfinite(max_audio_seconds)
            or not 0 < max_audio_seconds <= MAX_AUDIO_SECONDS):
        raise GoogleAsrError('google_limit')
    mapper = EventMapper(default_speaker)
    clock = clock or time.time
    max_bytes = int(max_audio_seconds * 32000)
    sent_end = asyncio.Event()
    tasks = []
    ws = None

    def report():
        if usage_callback is not None:
            try:
                usage_callback(dict(mapper.report))
            except Exception:
                raise GoogleAsrError() from None

    async def body():
        nonlocal ws
        connection = connect() if connect is not None else _connect(api_key)
        # Explicit enter/close rather than an unbounded third-party __aexit__.
        ws = await _bounded(connection.__aenter__(), OPEN_TIMEOUT)
        await _bounded(ws.send(json.dumps(build_handshake(model, mode))), SEND_TIMEOUT)
        ack, _ = _parse(await _bounded(ws.recv(), OPEN_TIMEOUT))
        if not isinstance(ack.get('setupComplete'), dict):
            raise GoogleAsrError()
        start = asyncio.get_running_loop().time()

        async def sender():
            while True:
                frame = await pcm_queue.get()
                if frame is None:
                    await _bounded(ws.send(json.dumps({'realtimeInput': {'audioStreamEnd': True}})), SEND_TIMEOUT)
                    sent_end.set()
                    return
                if not isinstance(frame, bytes) or not frame or len(frame) % 2 or len(frame) > MAX_FRAME_BYTES:
                    raise GoogleAsrError()
                if mapper.report['audio_bytes'] + len(frame) > max_bytes:
                    raise GoogleAsrError('google_limit')
                for offset in range(0, len(frame), CHUNK_BYTES):
                    chunk = frame[offset:offset + CHUNK_BYTES]
                    # Files cannot be burst-sent as if they were real-time input.
                    delay = start + mapper.report['audio_seconds'] - asyncio.get_running_loop().time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    packet = {'realtimeInput': {'audio': {'data': base64.b64encode(chunk).decode('ascii'), 'mimeType': 'audio/pcm;rate=16000'}}}
                    # Count attempted audio before send: a lost response cannot
                    # prove it wasn't transmitted, so never refund it silently.
                    mapper.report['audio_bytes'] += len(chunk)
                    mapper.report['audio_seconds'] = mapper.report['audio_bytes'] / 32000
                    report()
                    await _bounded(ws.send(json.dumps(packet)), SEND_TIMEOUT)

        async def receiver():
            messages, total_bytes = 0, 0
            while True:
                raw = await ws.recv()
                event, size = _parse(raw)
                messages += 1
                total_bytes += size
                if messages > MAX_MESSAGES or total_bytes > MAX_RECEIVED_BYTES:
                    raise GoogleAsrError('google_limit')
                segment = mapper.map(event, clock())
                if segment is not None:
                    emit(segment)
                report()

        sender_task = asyncio.create_task(sender())
        receiver_task = asyncio.create_task(receiver())
        tasks.extend([sender_task, receiver_task])
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if receiver_task in done:
            await receiver_task
            if not sent_end.is_set():
                raise GoogleAsrError()
        await sender_task
        done, _ = await asyncio.wait([receiver_task], timeout=DRAIN_TIMEOUT)
        if receiver_task in done:
            await receiver_task  # Early EOF / provider failure remains an error.
        # Do not infer an ASR end acknowledgment from conversational events.
        mapper.report['warning'] = 'Final audio tail is unconfirmed; review the partial transcript.'
        report()

    try:
        await _bounded(body(), MAX_WALL_SECONDS - CLOSE_TIMEOUT - 0.25)
        return dict(mapper.report)
    except asyncio.CancelledError:
        raise
    except GoogleAsrError:
        raise
    except TimeoutError:
        raise GoogleAsrError() from None
    except Exception:
        raise GoogleAsrError() from None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            # Do not let a misbehaving consumer stretch shutdown indefinitely.
            await asyncio.wait(tasks, timeout=0.25)
            for task in tasks:
                if task.done() and not task.cancelled():
                    task.exception()
        try:
            report()
        finally:
            if ws is not None:
                try:
                    await _close(ws)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise GoogleAsrError() from None


def pcm_from_wav(audio):
    """Validate a bounded in-memory RIFF PCM16 mono16k fixture, no conversion."""
    if not isinstance(audio, bytes) or not 44 <= len(audio) <= 32000 * MAX_AUDIO_SECONDS + 65536:
        raise GoogleAsrError('google_limit')
    # Enforce the declared container length, including rejection of trailing data.
    if audio[:4] != b'RIFF' or audio[8:12] != b'WAVE' or int.from_bytes(audio[4:8], 'little') + 8 != len(audio):
        raise GoogleAsrError()
    try:
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            frames = wav.getnframes()
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000
                    or wav.getcomptype() != 'NONE' or not 16000 <= frames <= 16000 * MAX_AUDIO_SECONDS):
                raise GoogleAsrError()
            pcm = wav.readframes(frames + 1)
            if len(pcm) != frames * 2:
                raise GoogleAsrError()
            return pcm
    except GoogleAsrError:
        raise
    except Exception:
        raise GoogleAsrError() from None


async def transcribe_wav(audio, emit, api_key, default_speaker='you', *, connect=None, usage_callback=None):
    """Same real-time transport for an explicitly authorized synthetic WAV.

    No devices, uploads to a file API, retries, key lookup or file persistence.
    Caller reserves its shared allowance before this function is called.
    """
    pcm = pcm_from_wav(audio)
    queue = asyncio.Queue(maxsize=8)

    async def produce():
        for start in range(0, len(pcm), CHUNK_BYTES):
            await queue.put(pcm[start:start + CHUNK_BYTES])
        await queue.put(None)

    producer = asyncio.create_task(produce())
    try:
        return await run_session(queue, emit, api_key, default_speaker=default_speaker,
                                 connect=connect, usage_callback=usage_callback)
    finally:
        producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass
