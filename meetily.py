"""Original read-only adapter for Meetily v0.4.1 JSON snapshots."""
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading


class MeetilyBridge:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, root=None, poll_interval=.5):
        self.root = Path(root) if root is not None else self.default_root()
        self.poll_interval = max(.01, float(poll_interval))
        self._state = {'status': 'idle', 'backend': 'meetily', 'message': 'Not attached', 'error': None}
        # Per-segment delivery memory, keyed by folder and sequence with the
        # latest delivered content: corrections (same sequence, new content)
        # and genuinely new segments always emit; already-delivered content
        # never repeats. Bounded to recent entries.
        self._emitted_seq = {}
        self._attached_folders = set()
        # Clear boundaries are recording-scoped and independent of the
        # evictable delivery cache. Cleared sequences stay suppressed; new
        # sequences remain eligible even if they arrive before resume.
        self._pending_clear = set()
        self._clear_high_water = {}
        # Clear epoch per recording: note_cleared bumps it, and every scan
        # stamps the epoch when the iteration starts. A scan that was already
        # in flight when Clear landed retires silently — it writes neither
        # delivery records nor withheld entries — instead of repopulating the
        # caches with cleared text after the purge.
        self._clear_epoch = {}
        self._folder_order = []
        # Segments deliberately withheld by the first-attachment 40-segment
        # window, keyed per folder and sequence: ordinary polls and resume
        # never backfill them, while corrections and genuinely new speech
        # stay eligible. The delivery record stays as the exactly-once
        # memory for invoked callbacks; the high-water mark stays as the
        # non-evictable Clear boundary.
        self._withheld = {}
        self._cursor_lock = threading.RLock()
        self._recording_id = None

    def note_cleared(self, recording_id=None):
        """Forget this recording's snapshot as it exists *now*.

        Do not defer a readable boundary until resume: speech recorded after
        Clear must remain eligible, and selecting another recording must not
        apply the old recording's boundary to it. If the snapshot is currently
        unreadable, fail closed on that recording's next valid snapshot.
        """
        recording_id = recording_id if recording_id is not None else self._recording_id
        if recording_id is None:
            return
        # The whole boundary is one atomic section: two concurrent clears
        # cannot interleave a stale snapshot read with a newer unreadable
        # result and leave a stale cutoff with pending discarded. (The
        # server Clear route serializes calls as well; this makes the
        # adapter contract atomic on its own.) File I/O under the lock is
        # brief and clears are rare; scans never block on it for long.
        with self._cursor_lock:
            # Retire in-flight scans BEFORE purging and reading: any scan
            # that already started stamps the old epoch and must not write
            # back cleared text after the purge. Marking pending first also
            # covers callbacks that return mid-clear; a later valid scan
            # converts pending into the cutoff.
            self._clear_epoch[recording_id] = self._clear_epoch.get(recording_id, 0) + 1
            self._pending_clear.add(recording_id)
            for store in (self._withheld, self._emitted_seq):
                for key in [key for key in store if key[0] == recording_id]:
                    store.pop(key, None)
            try:
                snapshot = self._read(self._folder(recording_id), 'transcripts.json')
                segments = snapshot['segments']
                if snapshot.get('version') != '1.0' or not isinstance(segments, list):
                    raise ValueError('Invalid clear snapshot')
                sequences = [item['sequence_id'] for item in segments]
                if any(type(seq) is not int or seq < 0 for seq in sequences):
                    raise ValueError('Invalid sequence')
                cutoff = max(sequences, default=-1)
            except (OSError, ValueError, KeyError, TypeError):
                return
            self._clear_high_water[recording_id] = max(
                cutoff, self._clear_high_water.get(recording_id, -1))
            self._pending_clear.discard(recording_id)
            # Explicit Clear must release cached content, not just delivery
            # fingerprints: drop this recording's withheld window and
            # correction cache so no cleared transcript text is retained in
            # memory. The non-evictable cutoff enforces suppression on all
            # future scans.
            for store in (self._withheld, self._emitted_seq):
                for key in [key for key in store if key[0] == recording_id]:
                    store.pop(key, None)

    @classmethod
    def default_root(cls):
        if sys.platform == 'darwin':
            # Meetily v0.4.1's snapshot saver uses its platform default directly,
            # rather than the optional recording export preference.
            return Path.home() / 'Movies' / 'meetily-recordings'
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

    @staticmethod
    def _open_snapshot(path):
        """Read without preventing Meetily's atomic replacement on Windows."""
        if os.name != 'nt':
            return path.open('rb')
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        # GENERIC_READ; SHARE_READ|SHARE_WRITE|SHARE_DELETE; OPEN_EXISTING;
        # OPEN_REPARSE_POINT avoids following a link swapped in after lstat.
        handle = create(str(path), 0x80000000, 0x7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        except Exception:
            close(handle)
            raise
        try:
            if getattr(os.fstat(fd), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked recording files are not supported')
            return os.fdopen(fd, 'rb')  # stream owns and closes the handle
        except Exception:
            os.close(fd)
            raise

    def _read(self, folder, name):
        folder = self._folder(folder.name)
        path = folder / name
        if not self._plain(path):
            raise ValueError('Linked recording files are not supported')
        with self._open_snapshot(path) as source:
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
        worker = getattr(self, '_thread', None)
        running = bool(worker and worker.is_alive())
        state = dict(self._state)
        if state['status'] == 'stopping' and not running:
            state.update(status='idle', message='Not attached')
        return {**state, 'running': running, 'stop_confirmed': not running}

    def attach(self, recording_id, on_segment, on_status):
        """Explicitly attach an active recording; callbacks run on a daemon thread."""
        folder = self._folder(recording_id)
        if self._read(folder, 'metadata.json').get('status') != 'recording':
            raise ValueError('Only active recording status can be attached')
        if not callable(on_segment) or not callable(on_status):
            raise ValueError('Callbacks must be callable')
        self.stop()
        if not self.status()['stop_confirmed']:
            raise RuntimeError('Meetily reader is still stopping')
        self._recording_id = recording_id
        with self._cursor_lock:
            # Bound per-folder bulk memory. The Clear high-water mark and
            # pending flags are small (one entry per recording) and are
            # never evicted: dropping them could resurrect cleared content
            # on a much-later re-attach. Evicting delivery/withheld history
            # or first-attachment state only affects what that re-attach
            # treats as new, and consumers upsert by ID.
            if recording_id in self._folder_order:
                self._folder_order.remove(recording_id)
            self._folder_order.append(recording_id)
            while len(self._folder_order) > 32:
                oldest = self._folder_order.pop(0)
                self._attached_folders.discard(oldest)
                for store in (self._withheld, self._emitted_seq):
                    for key in [key for key in store if key[0] == oldest]:
                        store.pop(key, None)
        event = threading.Event()
        self._event = event
        self._state = {'status': 'listening', 'backend': 'meetily', 'message': 'Listening to selected recording', 'error': None}
        self._thread = threading.Thread(target=self._poll, args=(folder, event, on_segment, on_status), daemon=True, name='meetily-reader')
        self._thread.start()

    def _notify(self, event, callback, value, scan_epoch=None, folder_name=None):
        """Acknowledge only an invoked, successfully completed callback.

        A callback may itself stop the bridge. That callback was delivered;
        subsequent callbacks suppressed by cancellation were not. When the
        delivery site passes a scan epoch, a Clear that retired the scan
        after the eligibility check still suppresses the invocation: the
        epoch is re-checked here, immediately before user code runs. A residual
        check-to-call gap remains: the lock is released before the callback
        runs, so Clear can theoretically land in between. No duration is
        claimed for that window, and it is not deterministically reproducible
        by ordinary scheduling — every deterministic demonstration of this gap
        uses execution control between the check and the call (a hook pausing
        the worker there, or debugger-level thread control).
        """
        if event.is_set():
            return False
        if scan_epoch is not None and folder_name is not None:
            with self._cursor_lock:
                if scan_epoch != self._clear_epoch.get(folder_name, 0):
                    return False
        try:
            callback(value)
            return True
        except Exception:
            if not event.is_set():
                self._state = {'status': 'error', 'backend': 'meetily', 'message': 'Meetily callback failed', 'error': 'callback_failed'}
                event.set()
            return False

    def _set_status(self, event, callback, status, message, error=None):
        if event.is_set():
            return
        self._state = {'status': status, 'backend': 'meetily', 'message': message, 'error': error}
        self._notify(event, callback, self.status())

    def _poll(self, folder, event, on_segment, on_status):
        initial = True
        self._set_status(event, on_status, 'listening', 'Listening to selected recording')
        while not event.is_set():
            with self._cursor_lock:
                scan_epoch = self._clear_epoch.get(folder.name, 0)
            try:
                meta = self._read(folder, 'metadata.json')
            except (OSError, ValueError, KeyError, TypeError):
                meta = None
            try:
                snapshot = self._read(folder, 'transcripts.json')
                if event.is_set():
                    return  # a blocked read cannot advance a retired scan
                if snapshot.get('version') != '1.0':
                    self._set_status(event, on_status, 'error', 'Unsupported Meetily transcript schema version', 'unsupported_schema')
                    return
                segments = snapshot.get('segments')
                if not isinstance(segments, list):
                    raise ValueError('Invalid segments')
                current = {}
                for item in segments:
                    seq = item['sequence_id']
                    timestamp = item['audio_start_time']
                    if (type(seq) is not int or seq < 0 or not isinstance(item['text'], str)
                            or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                            or timestamp < 0):
                        raise ValueError('Invalid segment fields')
                    current[seq] = {'id': 'meetily:' + folder.name + ':' + str(seq), 'speaker': 'meeting',
                                    'text': item['text'], 'timestamp': timestamp, 'sequence_id': seq}
                with self._cursor_lock:
                    if folder.name in self._pending_clear and scan_epoch == self._clear_epoch.get(folder.name, 0):
                        # Only a scan that started after the Clear converts
                        # pending into the cutoff. An in-flight scan holding
                        # a pre-clear snapshot must not consume the boundary:
                        # its maximum is stale, and using it would deliver
                        # speech that arrived at or after Clear time.
                        self._pending_clear.discard(folder.name)
                        self._clear_high_water[folder.name] = max(
                            max(current, default=-1), self._clear_high_water.get(folder.name, -1))
                if initial:
                    # Only first attachment limits initial display to 40.
                    # Normal resume recovers undelivered speech; Clear uses
                    # a separate recording-scoped, non-evictable cutoff.
                    first = folder.name not in self._attached_folders
                    self._attached_folders.add(folder.name)
                    selected = list(current.items())[-40:] if first else list(current.items())
                else:
                    first = False
                    selected = current.items()
                for seq, value in selected:
                    delivered = (value.get('id'), value.get('text'), value.get('timestamp'))
                    fseq = (folder.name, seq)
                    with self._cursor_lock:
                        # A Clear that lands mid-scan retires this iteration:
                        # already-read segments must not invoke callbacks
                        # after the boundary, even though the pane/server
                        # layer also rejects retired generations. Epochs are
                        # monotonic per recording, so a scan that started
                        # before the bump can never become current again.
                        if scan_epoch != self._clear_epoch.get(folder.name, 0):
                            eligible = False
                        # The delivery record suppresses content already
                        # received; the withheld window suppresses unchanged
                        # history the initial 40-segment limit never showed;
                        # a flip-flop back to earlier text still emits
                        # because it differs from the last delivery.
                        elif seq <= self._clear_high_water.get(folder.name, -1):
                            eligible = False
                        elif fseq in self._emitted_seq:
                            # The delivery record decides first: already
                            # received content never repeats, while a
                            # flip-flop back to earlier text still emits
                            # because it differs from the last delivery —
                            # even when it matches a withheld original.
                            eligible = self._emitted_seq[fseq] != delivered
                        elif (fseq in self._withheld
                                and self._withheld[fseq] == delivered):
                            # Deliberately withheld by the initial 40-segment
                            # window and unchanged since: old context the pane
                            # never showed. A correction changes the key and
                            # still emits.
                            eligible = False
                        else:
                            eligible = True
                    if eligible:
                        if self._notify(event, on_segment, dict(value),
                                        scan_epoch, folder.name):
                            with self._cursor_lock:
                                clear_pending = folder.name in self._pending_clear
                                current_event = getattr(self, '_event', None) is event
                                retired = scan_epoch != self._clear_epoch.get(folder.name, 0)
                                # A Clear that lands mid-scan retires this
                                # iteration: it must not re-record cleared
                                # text. The next valid scan sets the
                                # high-water mark, which suppresses it.
                                if not clear_pending and not retired and current_event and seq > self._clear_high_water.get(folder.name, -1):
                                    self._emitted_seq[fseq] = delivered
                                    # The delivery record is the exactly-once
                                    # memory: it must comfortably exceed any
                                    # snapshot the 8 MiB read cap admits, so
                                    # eviction can only duplicate the oldest
                                    # history on pathological reattachment
                                    # (consumers upsert by ID), never lose
                                    # an invoked callback.
                                    while len(self._emitted_seq) > 100000:
                                        self._emitted_seq.pop(next(iter(self._emitted_seq)))
                        if event.is_set():
                            break
                if initial and first and not event.is_set():
                    # First impression only, and only for a completed scan:
                    # remember what the 40-segment window deliberately
                    # withheld so ordinary polls and resume do not backfill
                    # it. An interrupted scan withholds nothing — everything
                    # undelivered stays eligible on resume. Anything
                    # genuinely new or corrected stays eligible. A Clear that
                    # lands mid-scan must not repopulate the withheld cache
                    # with cleared text: while the boundary is pending, the
                    # next valid scan sets the high-water mark instead.
                    with self._cursor_lock:
                        clear_pending = folder.name in self._pending_clear
                        retired = scan_epoch != self._clear_epoch.get(folder.name, 0)
                        if not clear_pending and not retired:
                            shown = set(seq for seq, _ in selected)
                            for seq, value in current.items():
                                if seq not in shown:
                                    self._withheld[(folder.name, seq)] = (
                                        value.get('id'), value.get('text'), value.get('timestamp'))
                initial = False
                state = meta.get('status') if isinstance(meta, dict) else None
                if state in ('completed', 'error'):
                    self._set_status(event, on_status, state, 'Recording finished' if state == 'completed' else 'Meetily recording failed', None if state == 'completed' else 'recording_error')
                    return
                if meta is None:
                    self._set_status(event, on_status, 'listening', 'Waiting for a valid JSON snapshot', 'snapshot_unavailable')
                else:
                    self._set_status(event, on_status, 'listening', 'Listening to selected recording')
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    meta_probe = self._read(folder, 'metadata.json')
                except Exception:
                    meta_probe = None
                state_probe = meta_probe.get('status') if isinstance(meta_probe, dict) else None
                if state_probe in ('completed', 'error'):
                    self._set_status(event, on_status, state_probe, 'Recording finished' if state_probe == 'completed' else 'Meetily recording failed', None if state_probe == 'completed' else 'recording_error')
                    return
                self._set_status(event, on_status, 'listening', 'Waiting for a valid JSON snapshot', 'snapshot_unavailable')
            except Exception:
                self._set_status(event, on_status, 'error', 'Meetily callback failed', 'callback_failed')
                return
            event.wait(self.poll_interval)

    def stop(self, timeout=1.):
        """Cancel delivery and wait at most one second for the worker."""
        event = getattr(self, '_event', None)
        if event is not None:
            event.set()
        worker = getattr(self, '_thread', None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0., min(float(timeout), 1.)))
        running = bool(worker and worker.is_alive())
        self._state = {'status': 'stopping' if running else 'idle', 'backend': 'meetily',
                       'message': 'Reader stop is not confirmed' if running else 'Not attached', 'error': None}
