"""Explicit local PCM archival. Never opens devices or chooses a storage location."""
import json
import math
import os
from pathlib import Path
import threading
import wave


class RecordingAudioError(RuntimeError):
    """A durable recording could not be safely written."""


def recording_directory(value):
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_dir() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RecordingAudioError('Recording directory must be an existing absolute local directory without symbolic links.')
    return path


class AudioArchive:
    """Per-source WAVs, written before ASR queues. No overwrite or arbitrary names.

    WAV headers are patched on every frame so a terminated worker leaves the
    last complete write readable. The timeline maps source sample offsets to
    capture timestamps; the two files are NOT a synchronized stereo mix.
    """
    def __init__(self, directory, max_bytes=1024 * 1024 * 1024):
        self.directory = recording_directory(directory)
        if self.directory is None:
            raise RecordingAudioError('A recording directory is required.')
        self.limit = max_bytes
        self.bytes = 0
        self.timeline_bytes = 0
        self.files = {}
        self.handles = {}
        self.offsets = {}
        self.lock = threading.RLock()
        self.closed = False
        self.failed = False
        self.timeline = None

    def _open(self, name):
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        return os.fdopen(fd, 'wb')

    def write(self, speaker, samples, timestamp):
        import numpy as np
        with self.lock:
            try:
                if self.closed or self.failed:
                    raise ValueError('archive is closed or failed')
                name = {'you': 'microphone', 'remote': 'system'}[speaker]
                values = np.asarray(samples, dtype=np.float32)
                if values.ndim != 1 or not np.isfinite(values).all() or not math.isfinite(float(timestamp)):
                    raise ValueError('invalid PCM or timestamp')
                pcm = (np.clip(values, -1, 1) * 32767).astype('<i2').tobytes()
                if self.bytes + len(pcm) > self.limit:
                    raise ValueError('recording storage limit reached')
                if not pcm:
                    return
                if self.timeline is None:
                    self.timeline = self._open('audio-timeline.jsonl')
                if name not in self.files:
                    handle = self._open(name + '.wav')
                    self.handles[name] = handle
                    writer = wave.open(handle, 'wb')
                    writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    self.files[name] = writer
                    self.offsets[name] = 0
                line = {'source': name, 'timestamp': float(timestamp),
                        'sample_offset': self.offsets[name], 'samples': len(values), 'sample_rate': 16000}
                encoded = (json.dumps(line, allow_nan=False) + '\n').encode('utf-8')
                if self.timeline_bytes + len(encoded) > 64 * 1024 * 1024:
                    raise ValueError('recording timeline limit reached')
                self.files[name].writeframes(pcm)
                self.handles[name].flush()
                self.timeline.write(encoded)
                self.timeline_bytes += len(encoded)
                self.timeline.flush()
                self.offsets[name] += len(values)
                self.bytes += len(pcm)
            except Exception as exc:
                self.failed = True
                raise RecordingAudioError('Audio recording could not be saved. Check free disk space and recording limits; partial files may remain.') from exc

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            error = None
            for writer in self.files.values():
                try:
                    writer.close()
                except Exception as exc:
                    error = exc
            for handle in [*self.handles.values(), self.timeline]:
                if handle:
                    try:
                        handle.close()
                    except Exception as exc:
                        error = exc
            if error:
                self.failed = True
                raise RecordingAudioError('Audio recording finalization failed; partial files may remain.') from error
