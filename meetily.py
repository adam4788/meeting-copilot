"""Original read-only adapter for Meetily v0.4.1 JSON snapshots."""
import json
import math
import os
from pathlib import Path
import stat
import threading


class MeetilyBridge:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, root=None, poll_interval=.5):
        self.root = Path(root) if root is not None else self.default_root()
        self.poll_interval = max(.01, float(poll_interval))
        self._state = {'status': 'idle', 'backend': 'meetily', 'message': 'Not attached', 'error': None}

    @classmethod
    def default_root(cls):
        if os.name == 'nt' and os.environ.get('APPDATA'):
            try:
                path = Path(os.environ['APPDATA']) / 'com.meetily.ai' / 'recording_preferences.json'
                with path.open('rb') as source:
                    data = json.loads(source.read(cls.MAX_BYTES + 1))
                value = data.get('save_folder')
                if isinstance(value, str) and value:
                    return Path(value)
            except (OSError, ValueError, AttributeError):
                pass
        return Path.home() / 'Music' / 'meetily-recordings'

    @staticmethod
    def _plain(path):
        info = path.lstat()
        return not (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400)

    def _folder(self, recording_id):
        if not isinstance(recording_id, str) or not recording_id or recording_id in ('.', '..') or any(c in recording_id for c in '/\\:'):
            raise ValueError('Invalid recording ID')
        root = self.root.resolve(strict=True)
        folder = root / recording_id
        if not self._plain(folder) or folder.resolve(strict=True).parent != root or not folder.is_dir():
            raise ValueError('Recording must be a direct non-linked child directory')
        return folder

    def _read(self, folder, name):
        folder = self._folder(folder.name)
        path = folder / name
        if not self._plain(path):
            raise ValueError('Linked recording files are not supported')
        with path.open('rb') as source:
            raw = source.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError('Recording JSON exceeds 8 MiB limit')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Expected JSON object')
        return data

    def recordings(self):
        rows = []
        try:
            children = self.root.iterdir()
            for child in children:
                try:
                    folder = self._folder(child.name)
                    meta = self._read(folder, 'metadata.json')
                    rows.append({'id': child.name, 'title': meta.get('meeting_name') or child.name,
                                 'status': meta.get('status', 'unknown'),
                                 'updated_at': meta.get('completed_at') or meta.get('created_at')})
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return rows

    def status(self):
        return dict(self._state)

    def attach(self, recording_id, on_segment, on_status):
        """Explicitly attach an active recording; callbacks run on a daemon thread."""
        folder = self._folder(recording_id)
        if self._read(folder, 'metadata.json').get('status') != 'recording':
            raise ValueError('Only active recording status can be attached')
        if not callable(on_segment) or not callable(on_status):
            raise ValueError('Callbacks must be callable')
        self.stop()
        event = threading.Event()
        self._event = event
        self._state = {'status': 'listening', 'backend': 'meetily', 'message': 'Listening to selected recording', 'error': None}
        self._thread = threading.Thread(target=self._poll, args=(folder, event, on_segment, on_status), daemon=True, name='meetily-reader')
        self._thread.start()

    def _notify(self, event, callback, value):
        if not event.is_set():
            try:
                callback(value)
            except Exception:
                if not event.is_set():
                    self._state = {'status': 'error', 'backend': 'meetily', 'message': 'Meetily callback failed', 'error': 'callback_failed'}
                    event.set()

    def _set_status(self, event, callback, status, message, error=None):
        if event.is_set():
            return
        self._state = {'status': status, 'backend': 'meetily', 'message': message, 'error': error}
        self._notify(event, callback, dict(self._state))

    def _poll(self, folder, event, on_segment, on_status):
        seen = {}
        initial = True
        self._set_status(event, on_status, 'listening', 'Listening to selected recording')
        while not event.is_set():
            try:
                meta = self._read(folder, 'metadata.json')
                snapshot = self._read(folder, 'transcripts.json')
                if snapshot.get('version') != '1.0':
                    self._set_status(event, on_status, 'error', 'Unsupported Meetily transcript schema version', 'unsupported_schema')
                    return
                segments = snapshot.get('segments')
                if not isinstance(segments, list):
                    raise ValueError('Invalid segments')
                current = {}
                for item in segments[-120:]:
                    seq = item['sequence_id']
                    timestamp = item['audio_start_time']
                    if (type(seq) is not int or seq < 0 or not isinstance(item['text'], str)
                            or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                            or timestamp < 0):
                        raise ValueError('Invalid segment fields')
                    current[seq] = {'id': 'meetily:' + folder.name + ':' + str(seq), 'speaker': 'meeting',
                                    'text': item['text'], 'timestamp': timestamp, 'sequence_id': seq}
                selected = list(current.items())[-40:] if initial else current.items()
                for seq, value in selected:
                    if seen.get(seq) != value:
                        self._notify(event, on_segment, dict(value))
                seen = current
                initial = False
                state = meta.get('status')
                if state in ('completed', 'error'):
                    self._set_status(event, on_status, state, 'Recording finished' if state == 'completed' else 'Meetily recording failed', None if state == 'completed' else 'recording_error')
                    return
                self._set_status(event, on_status, 'listening', 'Listening to selected recording')
            except (OSError, ValueError, KeyError, TypeError):
                self._set_status(event, on_status, 'listening', 'Waiting for a valid JSON snapshot', 'snapshot_unavailable')
            except Exception:
                self._set_status(event, on_status, 'error', 'Meetily callback failed', 'callback_failed')
                return
            event.wait(self.poll_interval)

    def stop(self):
        """Cancel delivery and wait at most one second for the worker."""
        event = getattr(self, '_event', None)
        if event is not None:
            event.set()
        worker = getattr(self, '_thread', None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.)
        self._state = {'status': 'idle', 'backend': 'meetily', 'message': 'Not attached', 'error': None}
