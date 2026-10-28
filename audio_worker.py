"""Isolated faster-whisper CPU worker; stdout is bounded NDJSON only.

Explicit offline verification (may download allowlisted weights):
  .venv-audio/bin/python audio_worker.py --file synthetic.wav --model tiny.en
No audio or transcripts are written to disk. Model cache writes are expected.
"""
import argparse
import base64
import json
import math
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from macos_audio import MODELS, SOURCES, validate, error_message

MAX_FRAME = 100000
CHUNK_SAMPLES = 64000  # Four seconds, separately buffered per source.
_OUTPUT_LOCK = threading.Lock()


def emit(event):
    data = json.dumps(event, ensure_ascii=True, allow_nan=False)
    if len(data) > 65000:
        raise ValueError('oversized event')
    with _OUTPUT_LOCK:
        print(data, flush=True)


def decode_frame(line):
    import numpy as np
    if len(line) > MAX_FRAME or not line.endswith(b'\n'):
        raise ValueError('frame bounds')
    item = json.loads(line)
    if (item.get('type') != 'pcm' or item.get('source') not in ('microphone', 'system')
            or item.get('rate') != 16000 or not isinstance(item.get('samples'), int)
            or not 1 <= item['samples'] <= 16000):
        raise ValueError('frame format')
    stamp = item.get('timestamp')
    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        raise ValueError('timestamp')
    try:
        raw = base64.b64decode(item['pcm'], validate=True)
    except Exception as exc:
        raise ValueError('PCM encoding') from exc
    if len(raw) != item['samples'] * 4:
        raise ValueError('PCM length')
    samples = np.frombuffer(raw, dtype='<f4')
    if not np.isfinite(samples).all():
        raise ValueError('PCM values')
    return ('you' if item['source'] == 'microphone' else 'remote'), samples, float(stamp)


def load_model(name, factory=None):
    validate(name, 'microphone', 'en')
    if factory is None:
        from faster_whisper import WhisperModel
        factory = WhisperModel
    return factory(name, device='cpu', compute_type='int8', cpu_threads=4)


def transcribe(model, samples, speaker, stamp, language):
    segments, _ = model.transcribe(samples, language=language, beam_size=1,
                                    vad_filter=True, condition_on_previous_text=False)
    text = ' '.join(segment.text.strip() for segment in segments).strip()[:8000]
    return {'type': 'segment', 'speaker': speaker, 'text': text, 'timestamp': stamp}


def run_capture(model, source, language, helper=None, stop=None):
    """Bounded reader queue. Helper inherits worker process group for gateway cleanup."""
    import numpy as np
    stop = stop or threading.Event()
    helper = helper or Path(__file__).resolve().with_name('macos_capture')
    process = subprocess.Popen([str(helper), '--start', '--source', source],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    pending = queue.Queue(maxsize=4)
    done = threading.Event()
    failure = []

    def enqueue(item):
        if pending.full():
            try:
                pending.get_nowait()
            except queue.Empty:
                pass
        pending.put_nowait(item)

    def read():
        buffers = {'you': [], 'remote': []}
        counts = {'you': 0, 'remote': 0}
        stamps = {}
        try:
            while not stop.is_set():
                line = process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME or not line.endswith(b'\n'):
                    raise ValueError('frame bounds')
                header = json.loads(line)
                if header.get('type') == 'error':
                    failure.append(header.get('code', 'capture'))
                    break
                if header.get('type') == 'status':
                    if header.get('state') == 'running':
                        emit({'type': 'status', 'state': 'running'})
                    continue
                speaker, samples, stamp = decode_frame(line)
                if (source == 'microphone' and speaker != 'you') or (source == 'system' and speaker != 'remote'):
                    raise ValueError('unexpected source')
                if not counts[speaker]:
                    stamps[speaker] = stamp
                buffers[speaker].append(samples)
                counts[speaker] += len(samples)
                if counts[speaker] >= CHUNK_SAMPLES:
                    enqueue((speaker, np.concatenate(buffers[speaker]), stamps[speaker]))
                    buffers[speaker], counts[speaker] = [], 0
        except Exception:
            failure.append('protocol')
        finally:
            done.set()

    reader = threading.Thread(target=read, daemon=True, name='native-audio-reader')
    reader.start()
    try:
        while not stop.is_set():
            try:
                speaker, samples, stamp = pending.get(timeout=.1)
            except queue.Empty:
                if done.is_set():
                    break
                continue
            result = transcribe(model, samples, speaker, stamp, language)
            if result['text'] and not stop.is_set():
                emit(result)
        if not stop.is_set():
            code = failure[0] if failure else 'capture'
            emit({'type': 'error', 'code': code, 'message': error_message(code)})
    finally:
        stop.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=.5)
        reader.join(timeout=.5)
        if not reader.is_alive():
            process.stdout.close()
        while not pending.empty():
            try:
                pending.get_nowait()
            except queue.Empty:
                break


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit local CPU int8 transcription; starting may download model weights.')
    parser.add_argument('--model', choices=MODELS, default='base.en')
    parser.add_argument('--source', choices=SOURCES, default='microphone')
    parser.add_argument('--language', choices=['en'], default='en')
    parser.add_argument('--file', type=Path, help='Read an existing synthetic/offline WAV instead of opening audio devices')
    args = parser.parse_args(argv)
    validate(args.model, args.source, args.language)
    if args.file and (args.file.suffix.lower() != '.wav' or not args.file.is_file()):
        parser.error('--file must be an existing WAV')
    helper = Path(__file__).resolve().with_name('macos_capture')
    if not args.file and not helper.is_file():
        emit({'type': 'error', 'code': 'helper', 'message': error_message('helper')})
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    emit({'type': 'status', 'state': 'loading'})
    try:
        model = load_model(args.model)
    except Exception:
        emit({'type': 'error', 'code': 'model', 'message': error_message('model')})
        return 1
    if stop.is_set():
        return 0
    try:
        if args.file:
            result = transcribe(model, str(args.file), 'you', time.time(), args.language)
            if not stop.is_set():
                emit(result)
        else:
            run_capture(model, args.source, args.language, stop=stop)
    except Exception:
        emit({'type': 'error', 'code': 'capture', 'message': error_message('capture')})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
