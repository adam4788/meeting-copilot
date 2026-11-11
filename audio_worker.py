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

from macos_audio import MODELS, SOURCES, validate, error_message, clean_writer_timing

MAX_FRAME = 100000
CHUNK_SAMPLES = 64000  # Earliest utterance boundary, separately per source.
MAX_CHUNK_SAMPLES = 192000  # Twelve-second bound for continuous speech.
SILENCE_SAMPLES = 6400  # 400ms trailing silence; avoid cutting words at four seconds.
# Keep helper retirement plus session drain within the macOS adapter's default
# 2.5s graceful-stop window, leaving a margin for scheduling and publication.
META_SESSION_DRAIN_TIMEOUT = .75


def utterance_boundary(buffers, count):
    """Bounded energy endpointing, not speaker detection or a semantic VAD.

    Quiet microphones may need system gain adjustment. Continuous/noisy input
    still flushes at the hard bound; no claim that every word boundary is safe.
    """
    if count >= MAX_CHUNK_SAMPLES:
        return True
    if count < CHUNK_SAMPLES:
        return False
    import numpy as np
    # Microphone frames can be only ~171 samples. Never copy the entire
    # growing utterance on every callback just to inspect its trailing 400ms.
    parts, remaining = [], SILENCE_SAMPLES
    for samples in reversed(buffers):
        if remaining <= 0:
            break
        part = samples[-remaining:]
        parts.append(part)
        remaining -= len(part)
    tail = np.concatenate(parts[::-1]) if parts else np.empty(0, dtype=np.float32)
    return len(tail) >= SILENCE_SAMPLES and float(np.sqrt(np.mean(tail.astype(np.float64) ** 2))) < 0.008

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
    native_drops = item.get('native_dropped_chunks', item.get('dropped_chunks'))
    native_drops = int(native_drops) if type(native_drops) is int and native_drops > 0 else 0
    return ('you' if item['source'] == 'microphone' else 'remote'), samples, float(stamp), native_drops


def load_model(name, factory=None, *, offline=False):
    validate(name, 'microphone', 'en')
    if factory is None:
        from faster_whisper import WhisperModel
        factory = WhisperModel
    if offline:
        # A cache can disappear between preflight and Start. Do not turn a
        # successful check into an implicit download or tokenizer fallback.
        try:
            from .capture_preflight import cached_model_path
        except ImportError:
            from capture_preflight import cached_model_path
        cached = cached_model_path(name)
        return factory(str(cached), device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)
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
    helper_proc = {}
    def _graceful_stop(signum, frame):
        # SIGTERM (adapter teardown) means flush-and-exit, not die-mid-chunk:
        # stop the helper so the reader sees EOF, then drain, flush the tail,
        # and exit cleanly with a final summary.
        stop.set()
        try:
            proc = helper_proc.get('proc')
            if proc is not None and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    try:
        signal.signal(signal.SIGTERM, _graceful_stop)
    except Exception:
        pass
    helper = helper or Path(__file__).resolve().with_name('macos_capture')
    process = subprocess.Popen([str(helper), '--start', '--source', source],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    helper_proc['proc'] = process
    pending = queue.Queue(maxsize=4)
    done = threading.Event()
    failure = []
    dropped = {'count': 0}
    native_lost = {'count': 0}
    native_timing = {}
    tail_items = []
    sub_second_lost = {'count': 0}
    # faster-whisper inference is not guaranteed thread-safe: the reader's
    # tail flush and the main drain must never transcribe concurrently.
    infer_lock = threading.Lock()

    def enqueue(item):
        if pending.full():
            try:
                pending.get_nowait()
                dropped['count'] += 1
            except queue.Empty:
                pass
        pending.put_nowait(item)
        # Report the very first drop immediately, then every fourth: audio
        # loss must be visible, not just hinted after sustained overload.
        # Always the combined total: a worker-only count would rewind the
        # pane's latched combined total on arrival.
        if dropped['count'] == 1 or (dropped['count'] and dropped['count'] % 4 == 0):
            try:
                total = native_lost['count'] + dropped['count']
                emit({'type': 'status', 'state': 'running', 'dropped_chunks': total,
                      'native_dropped_chunks': native_lost['count'],
                      'worker_dropped_chunks': dropped['count'],
                      'warning': f"Transcription falling behind; worker dropped {dropped['count']} chunk(s), "
                                 f"native dropped {native_lost['count']} frame(s). Some words may be missing."})
            except Exception:
                pass

    def read():
        buffers = {'you': [], 'remote': []}
        counts = {'you': 0, 'remote': 0}
        stamps = {}
        # DEPRECATED mixed-unit field: 'dropped_chunks' sums worker chunks and
        # native frames for backward compatibility only. Consumers must
        # prefer native_dropped_chunks / worker_dropped_chunks /
        # sub_second_dropped_chunks, which are reported separately and never
        # summed in user-facing text.
        try:
            # Drain to EOF unconditionally: stopping teardown must not strand
            # helper-pipe audio. EOF (helper exit) ends the loop; the thread
            # is a daemon and joins are bounded, so a live helper cannot hang
            # shutdown.
            while True:
                line = process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME or not line.endswith(b'\n'):
                    raise ValueError('frame bounds')
                header = json.loads(line)
                if header.get('type') == 'status' and header.get('state') == 'stopped':
                    native_timing.update(clean_writer_timing(header.get('native_writer_timing_ms')))
                # Loss accounting comes first: status and error frames carry
                # pending native drops that must not be lost with the frame.
                # PCM frames account their own drops via decode_frame below,
                # so they are excluded here to avoid double counting.
                reported = header.get('native_dropped_chunks', header.get('dropped_chunks'))
                if header.get('type') in ('status', 'error') and isinstance(reported, int) and reported > 0:
                    native_lost['count'] += reported
                if header.get('type') == 'error':
                    failure.append(header.get('code', 'capture'))
                    break
                if header.get('type') == 'status':
                    if header.get('state') == 'running':
                        running = {'type': 'status', 'state': 'running',
                                   'dropped_chunks': native_lost['count'] + dropped['count'],
                                   'native_dropped_chunks': native_lost['count'],
                                   'worker_dropped_chunks': dropped['count']}
                        if native_lost['count'] or dropped['count']:
                            running['warning'] = (
                                f"Transcription falling behind; worker dropped {dropped['count']} chunk(s), "
                                f"native dropped {native_lost['count']} frame(s). Some words may be missing.")
                        elif header.get('warning'):
                            running['warning'] = header.get('warning')
                        emit(running)
                    continue
                speaker, samples, stamp, native_drops = decode_frame(line)
                if native_drops:
                    native_lost['count'] += native_drops
                    try:
                        emit({'type': 'status', 'state': 'running',
                              'dropped_chunks': native_lost['count'] + dropped['count'],
                              'native_dropped_chunks': native_lost['count'],
                              'worker_dropped_chunks': dropped['count'],
                              'warning': f"Capture dropped {native_lost['count']} native frame(s) under backpressure; some audio may be missing."})
                    except Exception:
                        pass
                if (source == 'microphone' and speaker != 'you') or (source == 'system' and speaker != 'remote'):
                    raise ValueError('unexpected source')
                if not counts[speaker]:
                    stamps[speaker] = stamp
                buffers[speaker].append(samples)
                counts[speaker] += len(samples)
                if utterance_boundary(buffers[speaker], counts[speaker]):
                    enqueue((speaker, np.concatenate(buffers[speaker]), stamps[speaker]))
                    buffers[speaker], counts[speaker] = [], 0
        except Exception:
            failure.append('protocol')
        finally:
            # Hand sub-chunk buffers to the ordered tail list instead of
            # transcribing them here: the main flush transcribes queued
            # audio oldest-first, then these tails in timestamp order, so a
            # trailing fragment can never overtake earlier chunks.
            # Anything that cannot be queued is counted as lost audio, never
            # silently cleared. The main flush counts sub-second leftovers.
            for speaker in ('you', 'remote'):
                try:
                    if counts.get(speaker, 0) > 0 and buffers.get(speaker):
                        chunk = np.concatenate(buffers[speaker])
                        try:
                            tail_items.append((stamps.get(speaker, 0.0), speaker, chunk))
                        except Exception:
                            if len(chunk) >= 16000:
                                dropped['count'] += 1
                            else:
                                sub_second_lost['count'] += 1
                except Exception:
                    dropped['count'] += 1
                    continue
                finally:
                    buffers[speaker], counts[speaker] = [], 0
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
            with infer_lock:
                result = transcribe(model, samples, speaker, stamp, language)
            # A finished transcription is valid speech: emit it even during
            # teardown (delivery is best-effort) instead of silently dropping
            # a chunk that was already removed from the queue.
            if result['text']:
                try:
                    emit(result)
                except Exception:
                    break
        # Graceful stop: wait briefly for the reader to finish consuming the
        # helper pipe, then flush trailing full chunks instead of discarding.
        # Best effort even when stop is set: this is the point of the flush.
        reader.join(timeout=1.0)
        reader_incomplete = reader.is_alive()
        try:
            leftover = []
            while True:
                try:
                    leftover.append(pending.get_nowait())
                except queue.Empty:
                    break
            # Reader tails join the leftovers; everything emits in
            # timestamp order so a tail from one speaker can never
            # overtake — or be overtaken by — an older chunk from the other.
            try:
                leftover.extend((speaker, samples, stamp)
                                for stamp, speaker, samples in sorted(tail_items, key=lambda t: t[0]))
            except Exception:
                pass
            # Cross-source chronological order: a tail from one speaker can
            # predate a queued chunk from the other, so emit by timestamp.
            try:
                leftover.sort(key=lambda item: item[2])
            except Exception:
                pass
            for speaker, samples, stamp in leftover:
                try:
                    if getattr(samples, 'size', len(samples)) >= 16000:
                        with infer_lock:
                            flushed = transcribe(model, samples, speaker, stamp, language)
                        if flushed['text']:
                            flushed['partial_tail'] = True
                            try:
                                emit(flushed)
                            except Exception:
                                dropped['count'] += 1
                    elif getattr(samples, 'size', len(samples)) > 0:
                        # Honest accounting: sub-second trailing audio that
                        # cannot form a chunk is lost, not silently kept.
                        # Tracked separately from full worker chunks.
                        sub_second_lost['count'] += 1
                except Exception:
                    # A failed flush transcription is lost audio too.
                    dropped['count'] += 1
                    continue
            total_lost = dropped['count'] + native_lost['count']
            if total_lost or sub_second_lost['count'] or reader_incomplete or native_timing:
                try:
                    warning = (f"Stopped with worker {dropped['count']} dropped chunk(s), "
                               f"native {native_lost['count']} dropped frame(s), "
                               f"{sub_second_lost['count']} sub-second tail(s); some words may be missing.")
                    if reader_incomplete:
                        warning += ' Reader had not finished; tail accounting may be incomplete.'
                    summary = {'type': 'status', 'state': 'stopped', 'dropped_chunks': total_lost,
                          'native_dropped_chunks': native_lost['count'],
                          'worker_dropped_chunks': dropped['count'],
                          'sub_second_dropped_chunks': sub_second_lost['count'],
                          'reader_incomplete': bool(reader_incomplete),
                          **({'native_writer_timing_ms': dict(native_timing)} if native_timing else {})}
                    if total_lost or sub_second_lost['count'] or reader_incomplete:
                        summary['warning'] = warning
                    emit(summary)
                except Exception:
                    pass
        except Exception:
            pass
        if not stop.is_set() or failure:
            # A recorded helper failure is always reported — even when
            # teardown already set stop. Silence here would hide a real
            # capture failure behind an orderly-looking shutdown.
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


def run_capture_meta(source, mode, api_key, model_id, helper=None, stop=None):
    """Stream helper PCM to Meta Muse Voice (opt-in cloud ASR).

    One realtime session per active source, so microphone audio keeps
    the 'you' label and system audio keeps 'remote'; DIARIZATION may
    refine speakers within a source. Only final turns are emitted.
    """
    import asyncio
    import os
    import meta_asr
    stop = stop or threading.Event()
    helper = helper or Path(__file__).resolve().with_name('macos_capture')
    process = subprocess.Popen([str(helper), '--start', '--source', source],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    emit({'type': 'status', 'state': 'running'})
    loop = asyncio.new_event_loop()
    sources = {'microphone': 'you', 'system': 'remote'}
    active = sources if source == 'both' else {source: sources[source]}
    queues = {name: asyncio.Queue(maxsize=64) for name in active}
    ended_queues = set()
    stop_requested = asyncio.Event()
    native_lost = {'count': 0}
    worker_lost = {'count': 0}
    native_error = {'code': None}
    schedule_lock = threading.Lock()
    accepting = {'value': True}
    watcher_done = threading.Event()
    terminate_lock = threading.Lock()
    termination_requested = {'value': False}

    def schedule(callback, *args):
        # Seal scheduling before closing the loop. The lock linearizes the
        # check with call_soon_threadsafe, including on a stuck reader.
        with schedule_lock:
            if not accepting['value']:
                return False
            try:
                loop.call_soon_threadsafe(callback, *args)
            except RuntimeError:  # The loop may already be closed.
                return False
            return True

    def terminate_helper():
        with terminate_lock:
            # A second SIGTERM makes the native helper exit immediately,
            # before its final loss report. The watcher and retirement can
            # both reach here while the first termination is still settling.
            if termination_requested['value']:
                return
            termination_requested['value'] = True
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass

    def report_loss():
        # Compatibility total combines native frames and Meta worker chunks;
        # the warning and separate fields preserve their distinct units.
        try:
            emit({'type': 'status', 'state': 'running',
                  'dropped_chunks': native_lost['count'] + worker_lost['count'],
                  'native_dropped_chunks': native_lost['count'],
                  'worker_dropped_chunks': worker_lost['count'],
                  'warning': (f"Transcription falling behind; worker dropped {worker_lost['count']} chunk(s), "
                              f"native dropped {native_lost['count']} frame(s). Some words may be missing.")})
        except Exception:
            pass

    def report_native_loss(value):
        # A helper frame carries a delta, not a cumulative total. Never copy
        # untrusted helper warning text into the pane or count a boolean as 1.
        if type(value) is not int or value <= 0:
            return
        native_lost['count'] += value
        report_loss()

    def remember_native_error(code):
        native_error['code'] = code
        # A provider may never finish even after reader EOF. Fatal native
        # errors must initiate the same bounded retirement as an explicit stop.
        stop_requested.set()

    def reader():
        try:
            for line in process.stdout:
                try:
                    if len(line) > MAX_FRAME or not line.endswith(b'\n'):
                        continue
                    header = json.loads(line)
                    if not isinstance(header, dict):
                        continue
                    if header.get('type') in ('status', 'error'):
                        if not schedule(report_native_loss, header.get('native_dropped_chunks', header.get('dropped_chunks'))):
                            break
                        if header.get('type') == 'error':
                            code = header.get('code')
                            allowed = ('microphone_usage', 'microphone_permission', 'system_permission', 'capture', 'protocol')
                            if not isinstance(code, str) or code not in allowed:
                                code = 'capture'
                            schedule(remember_native_error, code)
                            break
                        continue
                    speaker, samples, stamp, drops = decode_frame(line)
                except Exception:
                    continue
                if not schedule(report_native_loss, drops):
                    break
                name = 'microphone' if speaker == 'you' else 'system'
                if name not in queues:
                    continue
                frame = meta_asr.resample_to_24k(samples)
                if not schedule(_offer, queues[name], frame):
                    break
        except Exception:
            pass
        finally:
            schedule(_end_queues, queues)

    def _offer(target, frame):
        # All queue and counter operations run on the loop thread. EOF also
        # needs room; evicting an audio frame for it is still a worker loss.
        if target.full():
            try:
                evicted = target.get_nowait()
                if evicted is not None:
                    worker_lost['count'] += 1
                    report_loss()
            except asyncio.QueueEmpty:
                pass
        target.put_nowait(frame)

    def _end_queues(targets):
        for name, target in targets.items():
            if name not in ended_queues:
                ended_queues.add(name)
                _offer(target, None)

    def watcher():
        while not watcher_done.is_set():
            if stop.wait(.1):
                if not watcher_done.is_set():
                    terminate_helper()
                    schedule(stop_requested.set)
                return

    reader_thread = threading.Thread(target=reader, daemon=True, name='meta-audio-reader')
    reader_thread.start()
    threading.Thread(target=watcher, daemon=True, name='meta-audio-watcher').start()

    reader_incomplete = False
    forced_kill = False
    session_incomplete = False
    early_session_exit = False
    retired = False

    async def retire_reader():
        nonlocal reader_incomplete, forced_kill, retired
        if retired:
            return
        watcher_done.set()
        terminate_helper()
        try:
            if process.poll() is None:
                try:
                    # The native helper itself settles for 0.3s before its
                    # final writer barrier; allow time for that tail report.
                    process.wait(timeout=.7)
                except subprocess.TimeoutExpired:
                    # EOF after a kill does not prove the terminal loss delta
                    # was delivered. Record uncertainty even if the reader exits.
                    forced_kill = True
                    process.kill()
                    process.wait(timeout=.1)
        except Exception:
            pass
        # Stop the helper before waiting for its final pipe frames. A stuck
        # reader must not block exit; do not close its pipe while it is alive.
        reader_thread.join(timeout=.25)
        reader_incomplete = forced_kill or reader_thread.is_alive()
        with schedule_lock:
            accepting['value'] = False
        # Every accepted callback precedes this barrier, including an EOF
        # from the reader. No reader event can follow the eventual summary.
        await asyncio.sleep(0)
        _end_queues(queues)  # Idempotent; also unblocks a stuck-reader session.
        retired = True

    async def main_async():
        nonlocal session_incomplete, early_session_exit
        tasks = [asyncio.ensure_future(meta_asr.run_session(
            queues[name], emit, api_key, model_id, mode, label))
            for name, label in active.items()]
        stop_task = asyncio.ensure_future(stop_requested.wait())
        try:
            # A single failed/finished source or an explicit stop retires
            # capture rather than waiting forever for another source's EOF.
            await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            early_session_exit = any(task.done() and name not in ended_queues
                                     for name, task in zip(active, tasks))
            await retire_reader()
            pending = {task for task in tasks if not task.done()}
            if pending:
                _, pending = await asyncio.wait(pending, timeout=META_SESSION_DRAIN_TIMEOUT)
            if pending:
                session_incomplete = True
                for task in pending:
                    task.cancel()
                await asyncio.sleep(0)
            errors = [task.exception() for task in tasks if task.done() and not task.cancelled()]
            # Prefer the provider-specific error, but never turn another
            # session exception into an apparently successful capture.
            for error in errors:
                if isinstance(error, meta_asr.MetaAsrError):
                    raise error
            for error in errors:
                if error is not None:
                    raise error
        finally:
            stop_task.cancel()

    try:
        loop.run_until_complete(main_async())
    finally:
        try:
            loop.run_until_complete(retire_reader())
            # Sessions may stop before consuming all queued audio. Count each
            # abandoned frame once; a terminal sentinel is not audio.
            for target in queues.values():
                while not target.empty():
                    if target.get_nowait() is not None:
                        worker_lost['count'] += 1
            if (native_lost['count'] or worker_lost['count'] or reader_incomplete
                    or session_incomplete or early_session_exit or native_error['code']):
                warning = (f"Stopped with worker {worker_lost['count']} dropped chunk(s), "
                           f"native {native_lost['count']} dropped frame(s); some words may be missing.")
                if reader_incomplete:
                    if forced_kill:
                        warning += ' Helper was forcibly stopped; tail accounting may be incomplete.'
                    else:
                        warning += ' Reader had not finished; tail accounting may be incomplete.'
                if session_incomplete:
                    warning += ' Transcription session drain was incomplete.'
                if early_session_exit:
                    warning += ' Transcription session ended before capture finished.'
                emit({'type': 'status', 'state': 'stopped',
                      'dropped_chunks': native_lost['count'] + worker_lost['count'],
                      'native_dropped_chunks': native_lost['count'],
                      'worker_dropped_chunks': worker_lost['count'],
                      'reader_incomplete': reader_incomplete, 'warning': warning})
            # The adapter stops reading after an error. Publish the terminal
            # loss summary first so the user can still see missing audio.
            if native_error['code']:
                code = native_error['code']
                emit({'type': 'error', 'code': code, 'message': error_message(code)})
        finally:
            loop.close()
            if not reader_thread.is_alive():
                try:
                    if process.stdout is not None:
                        process.stdout.close()
                except Exception:
                    pass


def _read_meta_worker_key(stream):
    """Read only the parent worker pipe, never the process environment."""
    try:
        if stream.isatty():
            return ''
        frame = stream.readline(4097)
        if not frame.endswith(b'\n') or len(frame) > 4096:
            return ''
        payload = json.loads(frame)
        if not isinstance(payload, dict) or set(payload) != {'type', 'key'} or payload['type'] != 'scoped_meta_key':
            return ''
        key = payload['key']
        if not isinstance(key, str) or not key.strip() or '\n' in key or '\r' in key:
            return ''
        return key
    except (OSError, ValueError, TypeError, AttributeError):
        return ''


def main(argv=None):
    parser = argparse.ArgumentParser(description='Live capture requires prepared cached weights; explicit file runs may download weights.')
    parser.add_argument('--model', choices=MODELS, default='base.en')
    parser.add_argument('--source', choices=SOURCES, default='microphone')
    parser.add_argument('--language', choices=['en'], default='en')
    parser.add_argument('--file', type=Path, help='Read an existing synthetic/offline WAV instead of opening audio devices')
    parser.add_argument('--helper', type=Path, help='Prepared native capture helper')
    parser.add_argument('--provider', choices=['local', 'meta'], default='local',
                        help='Transcription provider: local faster-whisper (default) or Meta Muse Voice cloud ASR')
    parser.add_argument('--asr-mode', choices=['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK'], default='ENDPOINTING')
    parser.add_argument('--asr-model', default=None, help='Meta ASR model id (default: ASR_MODEL or muse-voice-transcribe-1.0)')
    args = parser.parse_args(argv)
    validate(args.model, args.source, args.language, args.provider, args.asr_mode)
    if args.file and (args.file.suffix.lower() != '.wav' or not args.file.is_file()):
        parser.error('--file must be an existing WAV')
    helper = args.helper or Path(__file__).resolve().with_name('macos_capture')
    if not args.file and not helper.is_file():
        emit({'type': 'error', 'code': 'helper', 'message': error_message('helper')})
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    emit({'type': 'status', 'state': 'loading'})
    if args.provider == 'meta':
        import meta_asr
        api_key = _read_meta_worker_key(sys.stdin.buffer)
        if not api_key:
            emit({'type': 'error', 'code': 'asr_auth', 'message': error_message('asr_auth')})
            return 1
        model_id = args.asr_model or meta_asr.DEFAULT_MODEL
        try:
            if args.file:
                for segment in meta_asr.transcribe_file(str(args.file), api_key, model_id, args.asr_mode):
                    segment['timestamp'] = time.time()
                    if not stop.is_set():
                        emit(segment)
            elif not stop.is_set():
                run_capture_meta(args.source, args.asr_mode, api_key, model_id, helper=helper, stop=stop)
        except meta_asr.MetaAsrError:
            emit({'type': 'error', 'code': 'asr', 'message': error_message('asr')})
            return 1
        except Exception:
            emit({'type': 'error', 'code': 'capture', 'message': error_message('capture')})
            return 1
        return 0
    try:
        model = load_model(args.model, offline=not bool(args.file))
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
            run_capture(model, args.source, args.language, helper=helper, stop=stop)
    except Exception:
        emit({'type': 'error', 'code': 'capture', 'message': error_message('capture')})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
