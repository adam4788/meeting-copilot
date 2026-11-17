"""Explicit, local-only recording bundles. Never creates files on import/listen."""
import json
import os
from pathlib import Path
import secrets
import threading
import time

MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024


class RecordingSession:
    def __init__(self, *, backend, source, provider, root=None):
        if root is None:
            if __package__:
                from .native_runtime import effective_home
            else:
                from native_runtime import effective_home
            root = effective_home() / 'meeting-copilot-recordings'
        root = Path(root)
        if root.is_symlink():
            raise ValueError('Recording storage must not be a symbolic link')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + secrets.token_hex(8)
        self.directory = root / self.id
        self.directory.mkdir(mode=0o700)
        self.lock = threading.RLock()
        self.active = True
        self.error = None
        self.bytes_written = 0
        self.manifest = {'format': 'meeting-copilot-recording-v1', 'id': self.id,
                         'state': 'recording', 'started_at': time.time(),
                         'backend': backend, 'audio_source': source,
                         'transcription_provider': provider,
                         'transcript': 'transcript.jsonl',
                         'notice': 'Local durable files; Clear meeting does not delete this bundle. '
                                   'Transcript events with the same id replace earlier revisions. '
                                   'Source labels are not verified speaker identities.'}
        self.stream = None
        try:
            fd = os.open(self.directory / 'transcript.jsonl', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            self.stream = os.fdopen(fd, 'wb')
            self._write_manifest()
        except Exception:
            if self.stream:
                self.stream.close()
            self.active = False
            raise

    def _write_manifest(self):
        temp = self.directory / ('manifest-' + secrets.token_hex(4) + '.tmp')
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(self.manifest, out, ensure_ascii=False, allow_nan=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.directory / 'manifest.json')
        finally:
            temp.unlink(missing_ok=True)

    def append(self, segment):
        with self.lock:
            if not self.active:
                return
            row = {'speaker': str(segment.get('speaker', 'remote'))[:30],
                   'text': str(segment.get('text', '')).strip(),
                   'timestamp': segment.get('timestamp', time.time())}
            if not row['text']:
                return
            if segment.get('id') is not None:
                row['id'] = str(segment['id'])[:600]
            if segment.get('partial_tail') is True:
                row['partial_tail'] = True
            try:
                if len(row['text']) > 16000:
                    raise ValueError('transcript segment size limit')
                data = (json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
                if self.bytes_written + len(data) > MAX_TRANSCRIPT_BYTES:
                    raise ValueError('transcript size limit')
                self.stream.write(data)
                self.stream.flush()
                self.bytes_written += len(data)
            except Exception:
                self.error = 'Transcript could not be saved (storage unavailable or size limit). Recording is incomplete.'
                raise RuntimeError(self.error) from None

    def finish(self, capture=None):
        with self.lock:
            if not self.active:
                return
            capture = capture or {}
            incomplete = bool(self.error or capture.get('error') or capture.get('warning') or
                              any(capture.get(k) for k in ('dropped_chunks', 'native_dropped_chunks',
                                  'worker_dropped_chunks', 'sub_second_dropped_chunks', 'reader_incomplete')))
            self.manifest.update(state='partial' if incomplete else 'saved', stopped_at=time.time(),
                                 transcript_bytes=self.bytes_written,
                                 error=self.error or ('Capture reported missing audio/transcript or an error.' if incomplete else None))
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
                self.stream.close()
                # Audio adapter has stopped and closed its WAV files before this call.
                self.manifest['audio_files'] = [p.name for p in self.directory.glob('*.wav')]
                if not self.manifest['audio_files']:
                    self.manifest.update(state='partial', error='No audio file was saved. Check capture permissions and setup.')
                self._write_manifest()
            except Exception:
                self.error = 'Recording finalization failed. Some local files may be incomplete.'
                self.manifest.update(state='partial', error=self.error)
            finally:
                self.active = False
                try:
                    self.stream.close()
                except Exception:
                    pass  # A close failure must not undo confirmed audio shutdown.

    def status(self):
        with self.lock:
            return {'id': self.id, 'state': self.manifest['state'],
                    'directory': str(self.directory), 'error': self.error or self.manifest.get('error')}
