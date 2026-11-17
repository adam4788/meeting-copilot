"""Fixed Windows local worker. Private stdin accepts stop only; no credentials.

No helper processes, shell, automatic model downloads, or cloud providers.
stdout is bounded NDJSON only; third-party output is redirected to stderr.
"""
import argparse
import contextlib
import json
import math
import io
import os
from pathlib import Path
import sys
import threading
import time

from macos_audio import MODELS, SOURCES

_OUTPUT = sys.stdout
_OUTPUT_LOCK = threading.Lock()


def emit(value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False)
    if len(data) > 65000:
        raise ValueError('oversized worker event')
    with _OUTPUT_LOCK:
        _OUTPUT.write(data + '\n')
        _OUTPUT.flush()


def control_reader(stream):
    """Return a nonblocking, bounded raw-pipe reader (None means no data).

    Never hold a buffered stdin lock while waiting: on Windows that can stall
    native imports and cause a fatal lock error at interpreter shutdown. This
    pipe has exactly one reader. No commands other than stop are interpreted.
    """
    if isinstance(stream, io.BytesIO):  # finite synthetic fixtures only
        return lambda size: stream.read(size)
    fd = stream.fileno()
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        peek = ctypes.WinDLL('kernel32', use_last_error=True).PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                         ctypes.POINTER(wintypes.DWORD)]
        peek.restype = wintypes.BOOL

        def read(size):
            available = wintypes.DWORD()
            if not peek(handle, None, 0, None, ctypes.byref(available), None):
                raise OSError(ctypes.get_last_error(), 'control pipe unavailable')
            if not available.value:
                return None
            return os.read(fd, min(size, available.value))
        return read
    # For synthetic subprocess tests on POSIX. Windows pipes do not support
    # select(), hence the native readiness probe above.
    import select
    return lambda size: os.read(fd, size) if select.select([fd], [], [], 0)[0] else None


def read_control(stream, stop):
    """Closed, oversized or malformed control data stops capture, fail closed."""
    try:
        read = control_reader(stream)
        line = bytearray()
        while not stop.is_set():
            chunk = read(65 - len(line))
            if chunk is None:
                stop.wait(.02)
                continue
            if not chunk:
                return
            line.extend(chunk)
            if len(line) > 64 or b'\n' in line:
                # No paths, keys, providers, expressions or start commands.
                # Invalid input has exactly the same safe stop outcome.
                return
    except Exception:
        pass
    finally:
        stop.set()


@contextlib.contextmanager
def control_scope(control, stop):
    reader = None
    if control is not None:
        reader = threading.Thread(target=read_control, args=(control, stop),
                                  name='windows-audio-control')
        reader.start()
    try:
        yield
    finally:
        stop.set()
        if reader is not None:
            reader.join(timeout=1)


@contextlib.contextmanager
def windows_com_scope(factory=None):
    """Initialize/release COM on the exact thread making SoundCard calls.

    SoundCard 0.4.6's module-global keeper covers its importing thread only.
    Its audited _COMLibrary destructor balances this additional initialization.
    Keep the local reference inside the same thread and release in finally.
    """
    if factory is None:
        from soundcard import mediafoundation
        factory = mediafoundation._COMLibrary
    keeper = factory()
    try:
        yield
    finally:
        del keeper


from audio import AudioCapture


class WindowsThreadCapture(AudioCapture):
    def __init__(self, *args, com_factory=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._com_factory = com_factory

    def _run(self):
        try:
            with windows_com_scope(self._com_factory):
                super()._run()
        except Exception:
            self._stop.set()
            self._notify('error', 'Windows audio COM initialization or cleanup failed.')

    def _capture(self, speaker, device):
        try:
            with windows_com_scope(self._com_factory):
                super()._capture(speaker, device)
        except Exception:
            self._stop.set()
            self._notify('error', 'Windows audio COM initialization or cleanup failed.')


def run(source, model_name, directory=None, *, control=None, stop=None, probe=None, model_loader=None, capture_factory=None):
    """Dependency seams support synthetic tests; CLI supplies only fixed defaults."""
    if source not in SOURCES or model_name not in MODELS:
        raise ValueError('unsupported capture option')
    stop = stop or threading.Event()
    with control_scope(control, stop):
        return run_capture(source, model_name, directory, stop=stop, probe=probe,
                           model_loader=model_loader, capture_factory=capture_factory)


def run_capture(source, model_name, directory, *, stop, probe, model_loader, capture_factory):
    if stop.is_set():
        return 0
    emit({'type': 'status', 'state': 'loading'})
    try:
        if probe is None:
            from windows_preflight import probe_devices
            probe = probe_devices
        result = probe(source)
        expected = ['microphone', 'system'] if source == 'both' else [source]
        endpoints = result.get('endpoints', {})
        if (result.get('version') != 1 or result.get('ready') is not True
                or result.get('capture_started') is not False or result.get('source') != source
                or any(not isinstance(endpoints.get(name), dict)
                       or endpoints[name].get('ready') is not True
                       or not isinstance(endpoints[name].get('id'), str)
                       or not endpoints[name]['id'] for name in expected)):
            raise ValueError('device initialization failed')
    except Exception:
        emit({'type': 'error', 'code': 'capture'})
        return 1
    if stop.is_set():
        return 0
    try:
        if model_loader is None:
            from audio_worker import load_model
            model_loader = lambda name: load_model(name, offline=True)
        model = model_loader(model_name)
    except Exception:
        emit({'type': 'error', 'code': 'model'})
        return 1
    if stop.is_set():
        return 0
    capture = (capture_factory or WindowsThreadCapture)(
        input_id=endpoints.get('microphone', {}).get('id'),
        output_id=endpoints.get('system', {}).get('id'), model_size=model_name,
        model_factory=lambda *a, **kw: model, source=source)
    capture.require_first_frames = True
    failed = threading.Event()
    allowed_speakers = {'you' if name == 'microphone' else 'remote' for name in expected}

    def segment(value):
        if value.get('speaker') not in allowed_speakers:
            failed.set()
            stop.set()
            return
        text, stamp = value.get('text'), value.get('timestamp')
        if (not isinstance(text, str) or len(text) > 16000
                or not isinstance(stamp, (int, float)) or not math.isfinite(stamp)):
            failed.set()
            stop.set()
            return
        emit({'type': 'segment', 'speaker': value['speaker'], 'text': text, 'timestamp': stamp})

    def status(value):
        state = value.get('state')
        if state == 'error':
            failed.set()
            stop.set()
        # Never forward raw exceptions/device names/paths from third parties.
        event = {'type': 'status', 'state': 'stopped' if state in ('error', 'stopped') else 'starting' if state in ('starting', 'stopping') else 'running'}
        for key in ('worker_dropped_chunks', 'sub_second_dropped_chunks'):
            count = value.get(key, 0)
            event[key] = min(count, 10**12) if type(count) is int and count >= 0 else 0
        if event['worker_dropped_chunks'] or event['sub_second_dropped_chunks']:
            event['warning'] = 'Transcription stopped with dropped audio chunks or tails. Review saved audio before reprocessing.'
        emit(event)

    try:
        capture.set_recording_directory(directory)
        if stop.is_set():
            return 0
        # Device startup is distinct from offline model loading. Do not claim
        # running until each selected source has delivered valid frames.
        emit({'type': 'status', 'state': 'starting'})
        capture.start(segment, status)
        while not stop.wait(.05):
            if capture.status().get('state') == 'error':
                failed.set()
                break
    except Exception:
        failed.set()
    finally:
        # Bounded shutdown: parent grants a larger grace window and ultimately
        # kills this process if native calls or model inference remain blocked.
        capture.stop(timeout=1.5)
        terminal = capture.status()
        status(terminal)
        if terminal.get('running'):
            emit({'type': 'status', 'state': 'stopped', 'reader_incomplete': True,
                  'warning': 'Audio worker did not stop cleanly; saved audio or transcription may be incomplete.'})
            failed.set()
        if terminal.get('state') == 'error':
            failed.set()
        if failed.is_set():
            emit({'type': 'error', 'code': 'capture'})
    return 1 if failed.is_set() else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit isolated Windows local audio capture')
    parser.add_argument('--model', choices=MODELS, default='base.en')
    parser.add_argument('--source', choices=SOURCES, default='microphone')
    parser.add_argument('--recording-directory', type=Path)
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        emit({'type': 'error', 'code': 'capture'})
        return 1
    # Run in an offline allowlisted environment provided by the adapter. These
    # fixed protections are also enforced on explicit CLI invocation.
    import os
    os.environ.update(HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_OFFLINE='1',
                      TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    with contextlib.redirect_stdout(sys.stderr):
        return run(args.source, args.model, args.recording_directory, control=sys.stdin.buffer)


if __name__ == '__main__':
    raise SystemExit(main())
