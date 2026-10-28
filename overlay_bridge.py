"""Display-and-click bridge between the macOS overlay and the existing backend.

Display + click only: polls GET /status, feeds bounded NDJSON snapshots to the
compiled overlay helper's stdin, and POSTs /assist solely for explicit row
clicks read from the helper's stdout. No auto-click, auto-send, auto-record,
or capture control. Settings (models, transcript source, past-sessions scope,
web fallback) stay in the Hermes desktop pane; this bridge only carries the
pane-equivalent scope it was started with.

Answers stream incrementally per clicked row: each click opens one
request-scoped slot (POST /assist returns request_id) and only that slot's
deltas (GET /assist/<request_id>) are appended to the clicked row. Providers
without incremental support report completed-only honestly; completed
strings are never chunked to fake streaming.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time

TRANSCRIPT_LINES = 6
QUESTION_ROWS = 20
SEGMENT_WINDOW = 40
BRIDGE_VERSION = '0.2.0'

# Question starters that read as a request even without a trailing '?'.
# Kept conservative so status updates stay out of the clickable list.
QUESTION_PREFIXES = (
    'what ', 'how ', 'why ', 'when ', 'where ', 'who ', 'which ', 'whose ',
    'should ', 'could ', 'would ', 'can ', 'will ', 'is ', 'are ', 'do ',
    'does ', 'did ', 'any ',
)


def looks_like_question(text):
    """True for '?' lines plus common unpunctuated question starters."""
    lowered = ' '.join(str(text or '').split()).strip().lower()
    if not lowered or len(lowered) < 4:
        return False
    if lowered.endswith('?'):
        return True
    return lowered.startswith(QUESTION_PREFIXES)


def stable_row_id(text):
    """Stable across transcript window shifts: pure content hash, no position."""
    digest = hashlib.sha256(text.strip().lower().encode('utf-8')).hexdigest()[:12]
    return f'q-{digest}'


def suggest_questions(segments, limit=QUESTION_ROWS):
    """Derive clickable rows only from transcript lines phrased as questions.

    Most recent last, case-insensitively deduped, bounded. IDs are content
    hashes (stable when the last-40 window slides or meeting segments are
    corrected); collisions disambiguate by first-seen order. Deterministic
    and side-effect free so the pane and overlay agree on what is clickable.
    """
    rows, seen_ids, seen_text = [], set(), set()
    window = list(segments or [])[-SEGMENT_WINDOW:]
    for index, segment in enumerate(window):
        if isinstance(segment, dict):
            raw_text, segment_id = segment.get('text', ''), segment.get('id')
        else:
            raw_text, segment_id = segment, None
        text = ' '.join(str(raw_text or '').split()).strip()[:300]
        if not looks_like_question(text):
            continue
        key = text.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        explicit = str(segment_id or '').strip()
        if explicit and explicit not in seen_ids:
            seen_ids.add(explicit)
            rows.append({'id': explicit[:200], 'question': text})
            continue
        candidate = stable_row_id(key)
        if candidate not in seen_ids:
            seen_ids.add(candidate)
            rows.append({'id': candidate, 'question': text})
            continue
        for attempt in range(1, QUESTION_ROWS):
            suffixed = f'{candidate}-{attempt}'
            if suffixed not in seen_ids:
                seen_ids.add(suffixed)
                rows.append({'id': suffixed, 'question': text})
                break
    return rows[-limit:]


def build_snapshot(status, answers=None, message=None):
    """Build the bounded snapshot the Swift overlay renders verbatim."""
    status = status if isinstance(status, dict) else {}
    segments = status.get('segments') if isinstance(status.get('segments'), list) else []
    transcript = []
    for index, segment in enumerate(segments[-TRANSCRIPT_LINES:]):
        if not isinstance(segment, dict):
            continue
        transcript.append({
            'id': str(segment.get('id') or f'line-{index}'),
            'speaker': str(segment.get('speaker', 'remote'))[:30],
            'text': str(segment.get('text', ''))[:500],
        })
    answers = answers if isinstance(answers, dict) else {}
    questions = []
    for row in suggest_questions(segments):
        entry = answers.get(row['id'], {})
        answer = entry.get('answer')
        questions.append({
            'id': row['id'],
            'question': row['question'],
            'answer': str(answer or '')[:12000] if isinstance(answer, str) else None,
            'busy': bool(entry.get('busy')),
            'streaming': bool(entry.get('streaming')),
            'completed_only': bool(entry.get('completed_only', True)),
        })
    snapshot = {'type': 'snapshot', 'transcript': transcript, 'questions': questions,
                'busy': bool(status.get('busy'))}
    if message is not None:
        snapshot['message'] = message
    snapshot['share_warning'] = 'Overlay may be visible in screen shares.'
    return snapshot


def default_message(status):
    """Honest one-line overlay state; never promises capture or streaming."""
    status = status if isinstance(status, dict) else {}
    error = status.get('error')
    if error:
        return f'Unavailable: {str(error)[:200]}'
    if not status.get('listening'):
        return 'Paused — start listening from the Meeting Copilot pane.'
    capture = status.get('capture')
    if isinstance(capture, dict):
        text = capture.get('message') or capture.get('state')
        if text:
            return str(text)[:200]
    return None


def parse_click(line):
    """Parse one stdout line from the overlay helper; None means ignore."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get('type') != 'click':
        return None
    row_id = event.get('id')
    return row_id if isinstance(row_id, str) and row_id else None


class BackendTransport:
    """Injected backend handle: no HTTP, no second server, no unauthenticated socket.

    The owning backend process constructs this around its own MeetingState, so
    overlay clicks travel through already-authenticated in-process calls and
    per-row request slots instead of a parallel unauthenticated gateway route.
    `poll()` returns the live backend status dict for snapshot rendering.
    """

    def __init__(self, state, poll=None):
        self._state = state
        self._poll = poll
        self._poll_context = None
        # Capture the authorized pane/show/scope context HERE (construction
        # happens on the pane's explicit action thread, before native click
        # and answer-worker threads exist). Click/native threads never inherit
        # it, so this is the only explicit propagation path.
        try:
            import contextvars as _contextvars
            self._poll_context = _contextvars.copy_context()
        except Exception:
            self._poll_context = None

    def assist(self, question, scope, row_id):
        # Propagate the authorized pane/show/scope context explicitly into the
        # click-driven answer worker thread. Native click handlers run on
        # overlay threads that never inherit the pane request context, so
        # run the backend call under an explicitly captured context. Never
        # store auth: re-read the captured context here, and only inside the
        # pane-context runner.
        import contextvars as _contextvars
        scope = dict(scope or {})
        authoritative = getattr(self._state, 'overlay_scope_value', None)
        if isinstance(authoritative, dict) and authoritative:
            scope = dict(authoritative)
        caller_context = None
        try:
            candidate = self._poll_context
            if candidate is not None:
                caller_context = candidate
        except Exception:
            caller_context = None
        if caller_context is None:
            # No pane context was captured (constructed off the pane thread):
            # fail closed in the caller instead of running under an inherited
            # default. Documented in docs/verification.md.
            raise RuntimeError('overlay context is not from the pane action; re-show the overlay and retry')
        # Enter the pane context in THIS thread so the snapshot below captures
        # captures the pane's values (click/native threads never inherit them).
        pane_values = {}
        for var in caller_context:
            try:
                pane_values[var] = caller_context[var]
            except Exception:
                continue
        result_holder = {}

        def _call():
            for var, value in pane_values.items():
                try:
                    var.set(value)
                except Exception:
                    continue
            result_holder['result'] = self._state.assist(
                question, scope.get('project'), scope.get('web_query'),
                bool(scope.get('all_projects', False)),
                bool(scope.get('web_fallback', True)),
                scope.get('answer_provider'), scope.get('answer_model'),
                row_id=row_id)

        caller_context.run(_call)
        result = result_holder.get('result')
        request_id = result.get('request_id') if isinstance(result, dict) else None
        if not request_id:
            raise RuntimeError('backend did not accept the question')
        return request_id

    def assist_request(self, request_id):
        return self._state.assist_request(request_id)

    def cancel(self, request_id):
        try:
            self._state.cancel_assist(request_id)
        except Exception:
            pass

    def poll_status(self):
        if self._poll is not None:
            poll = self._poll
            if self._poll_context is not None:
                return self._poll_context.run(poll)
            return poll()
        return self._state.status()


class OverlayBridge:
    """Owns one helper subprocess; backend truth in, snapshots out, clicks back.

    Lifecycle: the backend creates this bridge on the explicit pane
    Show-overlay action (`show_overlay`) and tears it down on Hide/unmount
    (`hide_overlay`/`stop`). Request-scoped `ask()` workers poll only their
    own `GET /assist/<request_id>` slot, so concurrent rows can never take
    each other's answer. Stdin writes are serialized; a broken pipe stops the
    bridge instead of interleaving frames. Process groups are reaped with
    terminate-then-kill and bounded queues.
    """

    _SNAPSHOT_MAX_BYTES = 64000
    _ANSWER_MAX_CHARS = 12000

    def __init__(self, helper=None, transport=None, scope=None, poll_interval=0.25,
                 assist_timeout=90.0):
        self.helper_path = helper
        self.helper_args = []
        self.transport = transport
        self.scope = dict(scope or {})
        self.poll_interval = poll_interval
        self.assist_timeout = assist_timeout
        self.process = None
        self.lock = threading.RLock()
        self.segments = []
        self.questions = {}
        self.answers = {}
        self.order = []
        self.message = None
        self.request_by_row = {}
        self.row_by_request = {}
        self.cancelled = set()
        self.listening = False  # last status truth: rows are never clickable while paused
        self.paused_generation = 0
        self._listening = False
        self._paused = False
        self._last_generation = None
        self._poller = None
        self._poller_lock = threading.Lock()
        self._poller_seq = 0
        self._seq = 0
        self._latest_seq = 0
        self.pushed = []  # headless record (helper=None); used by tests/dry-run
        self.threads = []
        self.stopping = False
        import queue as _queue
        self._push_queue = _queue.Queue(maxsize=8)
        self._click_queue = _queue.Queue(maxsize=32)
        self._stdin_lock = threading.Lock()

    # -- backend truth in ------------------------------------------------
    def set_scope(self, scope):
        """Pane pushes current project/session scope; clears stale meeting data."""
        with self.lock:
            scope = dict(scope or {})
            if scope != self.scope:
                self.scope = scope
                self.questions = {}
                self.answers = {}
                self.order = []
                self.paused_generation += 1
                for request_id in list(self.row_by_request):
                    self._cancel_locked(request_id)
                self.request_by_row = {}
        return self.push()

    def update(self, status):
        if not isinstance(status, dict):
            with self.lock:
                self.message = default_message(status)
                self._listening = False
                self._paused = False
            return self.push()
        raw = status.get('segments') if isinstance(status.get('segments'), list) else []
        rows = suggest_questions(raw)
        with self.lock:
            self.segments = list(raw)
            listening = bool(status.get('listening'))
            generation = status.get('generation')
            if generation is None:
                generation = status.get('capture_generation')
            paused_now = not listening
            if paused_now and not self._paused:
                # First sight of a pause/switch/idle revision: retire rows so no
                # stale request can launch after this revision.
                self.paused_generation += 1
                for request_id in list(self.row_by_request):
                    self._cancel_locked(request_id)
                self.request_by_row = {k: v for k, v in self.request_by_row.items() if v is True}
            elif not paused_now and self._paused:
                # Resume: paused reservations never survived, drop any stale placeholder.
                for stale in [k for k, v in self.request_by_row.items() if v is True]:
                    self.request_by_row.pop(stale, None)
            self._listening = listening
            self._paused = paused_now
            self.listening = listening
            if generation != self._last_generation:
                # New meeting/session/clear revision retires in-flight rows so
                # late events from older work cannot resurrect stale rows.
                self._last_generation = generation
                for request_id in list(self.row_by_request):
                    self._cancel_locked(request_id)
                for row_id in list(self.request_by_row):
                    self.request_by_row.pop(row_id, None)
                    if row_id in self.answers:
                        self.answers[row_id] = {'answer': None, 'busy': False,
                                                'streaming': False, 'completed_only': True}
            self.questions = {row['id']: row['question'] for row in rows}
            live = set(self.questions)
            for row_id in [key for key in self.answers if key not in live and self.answers[key].get('busy')]:
                request_id = self.request_by_row.get(row_id)
                if request_id:
                    self._cancel_locked(request_id)
            for row_id in [key for key in self.answers if key not in live and not self.answers[key].get('busy')]:
                self.answers.pop(row_id, None)
                self.request_by_row.pop(row_id, None)
            while len(self.answers) > 40:
                oldest = next(iter(self.answers))
                request_id = self.request_by_row.pop(oldest, None)
                if request_id:
                    self.row_by_request.pop(request_id, None)
                self.answers.pop(oldest, None)
            self.order = [row_id for row_id in self.order if row_id in live]
            for row in rows:
                if row['id'] not in self.order:
                    self.order.append(row['id'])
            self.order = self.order[-QUESTION_ROWS:]
            for row_id in self.questions:
                self.answers.setdefault(row_id, {'answer': None, 'busy': False,
                                                 'streaming': False, 'completed_only': True})
            self.message = default_message(status)
            if not raw and not status.get('busy'):
                for row_id in [key for key in self.answers if not self.answers[key].get('busy')]:
                    self.answers.pop(row_id, None)
                    self.request_by_row.pop(row_id, None)
        return self.push()

    def _prune_threads_locked(self):
        self.threads = [t for t in self.threads if t.is_alive()]

    def _prune_threads(self):
        with self.lock:
            self._prune_threads_locked()

    # -- clicks out -------------------------------------------------------
    def on_click_line(self, line):
        """The single path that may request an answer: an explicit overlay click."""
        row_id = parse_click(line)
        if row_id is None:
            return False
        with self.lock:
            if not self._listening:
                return False  # paused/idle backend: never open expensive slots
            question = self.questions.get(row_id)
            if question is None:
                return False
            state = self.answers.setdefault(row_id, {'answer': None, 'busy': False,
                                                     'streaming': False, 'completed_only': True})
            if state.get('busy'):
                return False  # explicit busy row: never duplicate a request
            if self.request_by_row.get(row_id):
                return False
            if row_id not in self.questions:
                return False
            click_generation = self.paused_generation
            state.update({'answer': None, 'busy': True, 'streaming': False, 'completed_only': True})
            # Reserve synchronously: a second immediate click sees busy even
            # before the worker thread drains the queue.
            self.request_by_row[row_id] = True
            try:
                self._click_queue.put_nowait((row_id, question, click_generation))
            except Exception:
                self.request_by_row.pop(row_id, None)
                state['busy'] = False
                self.message = 'Too many pending clicks; try one row at a time.'
                self.push()
                return False
        self.push()
        worker = threading.Thread(target=self._drain_clicks, daemon=True)
        with self.lock:
            self._prune_threads_locked()
            self.threads.append(worker)
            self.threads = self.threads[-16:]
        worker.start()
        return True

    def _drain_clicks(self):
        try:
            row_id, question, click_generation = self._click_queue.get_nowait()
        except Exception:
            return
        self.ask(row_id, question, click_generation)

    def ask(self, row_id, question, click_generation=None):
        """Open one request-scoped slot, then stream only that slot to its row."""
        transport = self.transport
        assert transport is not None, 'ask requires a transport'
        if click_generation is None:
            click_generation = self.paused_generation
        try:
            request_id = transport.assist(question, dict(self.scope), row_id)
            if not isinstance(request_id, str) or not request_id:
                raise RuntimeError('backend did not accept the question')
        except Exception:
            with self.lock:
                self.request_by_row.pop(row_id, None)
                if row_id in self.answers:
                    self.answers[row_id] = {'answer': None, 'busy': False,
                                            'streaming': False, 'completed_only': True}
                self.message = 'Answer request failed: request was not accepted.'
            self.push()
            return
        with self.lock:
            # Reservation held since click; retire the new slot instead of
            # leaking an expensive backend orphan when the row moved on.
            if self.request_by_row.get(row_id) is not True or click_generation != self.paused_generation:
                orphan = request_id
                self.request_by_row.pop(row_id, None)
                self.row_by_request.pop(orphan, None)
                if row_id in self.answers:
                    self.answers[row_id] = {'answer': None, 'busy': False,
                                            'streaming': False, 'completed_only': True}
            else:
                orphan = None
                self.request_by_row[row_id] = request_id
                self.row_by_request[request_id] = row_id
                # Drop any other row holding this id (single-slot reuse safety).
                for other, rid in [item for item in self.request_by_row.items() if item[0] != row_id and item[1] == request_id]:
                    self.request_by_row.pop(other, None)
        if orphan is not None:
            try:
                transport.cancel(orphan)
            except Exception:
                pass
            self.push()
            return
        deadline = time.monotonic() + self.assist_timeout
        seen = ''
        streaming = False
        try:
            while time.monotonic() < deadline and not self.stopping:
                with self.lock:
                    if request_id in self.cancelled or row_id not in self.answers:
                        return
                try:
                    entry = transport.assist_request(request_id)
                except Exception:
                    time.sleep(self.poll_interval)
                    continue
                if not isinstance(entry, dict):
                    time.sleep(self.poll_interval)
                    continue
                if entry.get('row_id') and entry['row_id'] != row_id:
                    with self.lock:
                        self._cancel_locked(request_id)
                        if row_id in self.answers:
                            self.answers[row_id] = {'answer': None, 'busy': False,
                                                    'streaming': False, 'completed_only': True}
                        self.message = 'Answer arrived for a different row; request cancelled.'
                    self.push()
                    return
                answer = str(entry.get('answer') or '')
                if entry.get('status') == 'cancelled' or entry.get('cancelled'):
                    with self.lock:
                        if row_id in self.answers:
                            self.answers[row_id] = {'answer': seen or None, 'busy': False,
                                                    'streaming': False, 'completed_only': True}
                    self.push()
                    return
                if answer != seen:
                    seen = answer
                    streaming = bool(entry.get('streaming')) or streaming
                    with self.lock:
                        if row_id in self.answers:
                            self.answers[row_id] = {'answer': seen or None, 'busy': True,
                                                    'streaming': streaming,
                                                    'completed_only': bool(entry.get('completed_only', not streaming))}
                    self.push()
                if entry.get('status') in ('done', 'error'):
                    with self.lock:
                        if row_id in self.answers:
                            self.answers[row_id] = {'answer': seen or None, 'busy': False,
                                                    'streaming': False,
                                                    'completed_only': bool(entry.get('completed_only', not streaming))}
                            if entry.get('status') == 'error':
                                self.message = f"Answer failed for this row: {str(entry.get('error') or 'unknown')[:160]}"
                    self.push()
                    return
                time.sleep(self.poll_interval)
            with self.lock:
                try:
                    transport.cancel(request_id)
                except Exception:
                    pass
                if row_id in self.answers:
                    self.answers[row_id] = {'answer': seen or None, 'busy': False,
                                            'streaming': False, 'completed_only': not streaming}
                self.message = 'Answer timed out or was cleared; try again from the pane.'
            self.push()
        finally:
            with self.lock:
                self.request_by_row.pop(row_id, None)
                self.row_by_request.pop(request_id, None)

    def _cancel_locked(self, request_id):
        self.cancelled.add(request_id)
        row_id = self.row_by_request.pop(request_id, None)
        if row_id:
            self.request_by_row.pop(row_id, None)
            state = self.answers.get(row_id)
            if state is not None and state.get('busy'):
                state['busy'] = False
        try:
            if self.transport is not None:
                self.transport.cancel(request_id)
        except Exception:
            pass

    def cancel_row(self, row_id):
        with self.lock:
            request_id = self.request_by_row.get(row_id)
            if not request_id or request_id is True:
                return False
            self._cancel_locked(request_id)
            return True

    # -- subprocess lifecycle ---------------------------------------------
    def _helper_available(self):
        """Fail closed: only a prepared executable helper may launch a window."""
        import os as _os
        if sys.platform not in ('darwin', 'linux'):
            return False
        helper = self.helper_path
        if not helper or not isinstance(helper, str):
            return False
        if not _os.path.isfile(helper):
            return False
        return _os.access(helper, _os.X_OK)

    def helper_status(self):
        """Pane-facing availability: reason the overlay can or cannot show."""
        if self._helper_available():
            return {'available': True, 'helper': self.helper_path}
        if not self.helper_path:
            return {'available': False,
                    'error': 'Overlay helper is not prepared on this backend; run prepare_macos.py.'}
        if sys.platform not in ('darwin', 'linux'):
            return {'available': False,
                    'error': f'Overlay helper is not supported on this platform: {self.helper_path}'}
        return {'available': False,
                'error': f'Overlay helper is not executable: {self.helper_path}'}

    def start_helper(self):
        # Fail closed on unsupported platforms or missing preparation: never
        # report started when no process exists, and never leave an orphan.
        import os as _os
        if sys.platform not in ('darwin', 'linux'):
            self.process = None
            return None
        if not self._helper_available():
            self.process = None
            return None
        argv = [self.helper_path] + list(getattr(self, 'helper_args', None) or [])
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, text=True, bufsize=1)
        if sys.platform != 'win32':
            kwargs['start_new_session'] = True
        try:
            self.process = subprocess.Popen(argv, **kwargs)
        except (OSError, ValueError, TypeError):
            self.process = None
            return None
        return self.process

    def show_overlay(self):
        """Explicit pane action: open the overlay window, start forwarding."""
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    return {'shown': True, 'reused': True}
            except Exception:
                pass
            # Reaped or exited helper: fall through to a clean relaunch.
            self._reap_locked(None)
        self.stopping = False
        self.start_helper()
        process = self.process
        if process is None:
            status = self.helper_status()
            with self.lock:
                self.message = status.get('error')
            return {'shown': False, 'available': False,
                    'error': status.get('error')}
        if process.stdout is not None:
            reader = threading.Thread(target=self._read_clicks,
                                      name='overlay-click-reader', daemon=True)
            reader.start()
            with self.lock:
                self.threads.append(reader)
        self._start_pusher_locked()
        self._start_poller_locked()
        self.push()
        return {'shown': True, 'reused': False}

    def hide_overlay(self):
        """Explicit pane action: close the window, cancel rows, reap the group."""
        self.stop()
        return {'shown': False}

    def _start_pusher_locked(self):
        with self._poller_lock:
            pusher = threading.Thread(target=self._pump_pushes, daemon=True)
            with self.lock:
                self.threads.append(pusher)
            pusher.start()

    def _start_poller_locked(self):
        # One bounded status pump per overlay lifetime; no extra launch paths.
        # The pane context is captured here (the pane's explicit action thread)
        # because the pump thread never inherits it.
        poll = getattr(self.transport, 'poll_status', None)
        if poll is None:
            return
        with self._poller_lock:
            existing = self._poller
            if existing is not None and existing.is_alive():
                return
            pane_context = None
            try:
                import contextvars as _contextvars
                pane_context = _contextvars.copy_context()
            except Exception:
                pane_context = None
            self._poller_seq += 1
            worker = threading.Thread(target=self._poll_loop,
                                      args=(pane_context, poll, self._poller_seq),
                                      name='overlay-status-pump', daemon=True)
            self._poller = worker
            with self.lock:
                self.threads.append(worker)
            worker.start()

    def _poll_loop(self, pane_context, poll, seq):
        try:
            while not self.stopping:
                with self._poller_lock:
                    if seq != self._poller_seq or self.stopping:
                        return
                process = self.process
                if process is not None:
                    try:
                        if process.poll() is not None:
                            break
                    except Exception:
                        break
                try:
                    if pane_context is not None:
                        status = pane_context.run(poll)
                    else:
                        status = poll()
                except Exception as exc:
                    with self.lock:
                        self.message = f'Backend unreachable: {type(exc).__name__}'
                    self.push()
                else:
                    self.update(status)
                if self.stopping:
                    break
                # Bounded: one poll per interval, interruptible on hide.
                deadline = time.monotonic() + max(0.05, self.poll_interval)
                while time.monotonic() < deadline:
                    if self.stopping:
                        return
                    time.sleep(0.02)
        finally:
            if seq == self._poller_seq:
                self.stop()

    def _pump_pushes(self):
        while not self.stopping:
            try:
                snapshot, seq = self._push_queue.get(timeout=self.poll_interval)
            except Exception:
                continue
            with self.lock:
                if seq < self._latest_seq:
                    continue  # stale snapshot: a newer one is already queued
            self._emit(snapshot)

    def _emit(self, snapshot):
        if not isinstance(snapshot, dict):
            return False
        try:
            data = json.dumps(snapshot, ensure_ascii=False) + '\n'
        except (TypeError, ValueError):
            return False
        if len(data.encode('utf-8')) > self._SNAPSHOT_MAX_BYTES:
            snapshot = dict(snapshot)
            snapshot['transcript'] = []
            try:
                data = json.dumps(snapshot, ensure_ascii=False) + '\n'
            except (TypeError, ValueError):
                return False
            if len(data.encode('utf-8')) > self._SNAPSHOT_MAX_BYTES:
                return False
        process = self.process
        if process is None or process.stdin is None:
            self.pushed.append(snapshot)
            return True
        try:
            with self._stdin_lock:
                process_snapshot = self.process
                if process_snapshot is None or process_snapshot.stdin is None:
                    self.pushed.append(snapshot)
                    return True
                try:
                    process_snapshot.stdin.write(data)
                except (BrokenPipeError, OSError, ValueError):
                    self._broken_pipe_shutdown(process_snapshot)
                    return False
                try:
                    process_snapshot.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    self._broken_pipe_shutdown(process_snapshot)
                    return False
                return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _broken_pipe_shutdown(self, failed_process):
        # Broken stdin means the helper exited: stop pumps/readers and reap so
        # no later push resurrects writes to a dead pipe, without taking
        # _stdin_lock inside the reader path (the old stop()-under-lock hang).
        current = self.process
        if current is not None and failed_process is not None and current is not failed_process:
            return
        self.stopping = True
        with self._poller_lock:
            self._poller_seq += 1
            self._poller = None
        try:
            self._reap_locked(failed_process if failed_process is not None else current)
        except OSError:
            pass

    def push(self):
        """Render current state; headless (helper=None) records for tests."""
        with self.lock:
            rows = [{'id': row_id, 'question': self.questions[row_id]} for row_id in self.order if row_id in self.questions]
            segments = list(self.segments)
            answers = {row_id: dict(state) for row_id, state in self.answers.items()}
            message = self.message
            busy = any(state.get('busy') for state in answers.values())
            listening = bool(self._listening)
        snapshot = build_snapshot({'segments': segments, 'busy': busy, 'listening': listening},
                                  answers, message)
        # Preserve pinned row order so streaming rows do not jump around.
        order_index = {row_id: index for index, row_id in enumerate(self.order)}
        snapshot['questions'] = sorted(snapshot.get('questions', []),
                                       key=lambda row: order_index.get(row.get('id'), 10 ** 6))
        # Single serialized writer: assign a monotonic sequence, keep only the
        # newest snapshot in the bounded queue, and emit in order. Late or
        # duplicate older snapshots can never overwrite newer answers.
        with self.lock:
            self._seq += 1
            seq = self._seq
            self._latest_seq = seq
            drained = 0
            while drained < 8:
                try:
                    self._push_queue.get_nowait()
                except Exception:
                    break
                drained += 1
            try:
                self._push_queue.put_nowait((snapshot, seq))
            except Exception:
                pass
        return self._emit(snapshot)

    def run(self):
        """Poll the backend and forward clicks until the helper exits. Blocking."""
        transport = self.transport
        assert transport is not None, 'run requires a transport'
        poll = getattr(transport, 'poll_status', None)
        if poll is None:
            return
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    pass
                else:
                    self._reap_locked(None)
            except Exception:
                self._reap_locked(None)
        if self.process is None:
            self.start_helper()
            if self.process is None:
                return  # fail closed: no helper, no loop, no success claim
        self.stopping = False
        process = self.process
        if process and process.stdout:
            reader = threading.Thread(target=self._read_clicks,
                                      name='overlay-click-reader', daemon=True)
            reader.start()
            with self.lock:
                self.threads.append(reader)
        self._start_poller_locked()
        try:
            while not self.stopping:
                process = self.process
                if process is not None:
                    try:
                        if process.poll() is not None:
                            break
                    except Exception:
                        break
                else:
                    break
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _read_clicks(self):
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                self.on_click_line(line)
                if self.stopping:
                    break
        except (OSError, ValueError):
            pass

    def _reap_locked(self, process):
        target = process if process is not None else self.process
        if target is None:
            return
        try:
            running = target.poll() is None
        except Exception:
            running = False
        if running:
            try:
                target.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                target.wait(timeout=2)
                running = False
            except subprocess.TimeoutExpired:
                try:
                    target.kill()
                    target.wait(timeout=2)
                except Exception:
                    pass
        # Close pipes only after the child is reaped: closing stdout while the
        # reader thread is blocked is safe, and closing stdin first avoids the
        # old stop-while-reader-blocked deadlock.
        for stream in (getattr(target, 'stdout', None), getattr(target, 'stdin', None)):
            if stream is not None:
                try:
                    stream.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
        if self.process is target:
            self.process = None

    def stop(self):
        self.stopping = True
        with self.lock:
            for request_id in list(self.row_by_request):
                self._cancel_locked(request_id)
        with self._poller_lock:
            self._poller_seq += 1
            self._poller = None
        # Drain the coalescing queue so late snapshots cannot leak after exit.
        try:
            while True:
                self._push_queue.get_nowait()
        except Exception:
            pass
        process = self.process
        if process is not None:
            try:
                self._reap_locked(process)
            except OSError:
                pass


def main(argv=None):
    """Refuse standalone launches: the bridge is backend-managed only.

    Scope lives solely in the desktop pane (`/overlay/show`, `/overlay/scope`);
    transport is the injected in-process BackendTransport, never an
    unauthenticated standalone HTTP client. Keeping this entrypoint as a loud
    refusal (instead of deleting it) prevents silent reintroduction of the old
    `--base-url` + scope-flag client.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', default=None, help='Compiled macos_overlay binary.')
    parser.add_argument('--version', action='store_true', help='Print bridge version and exit.')
    args, unknown = parser.parse_known_args(argv)
    if args.version and not unknown:
        print(f'overlay-bridge {BRIDGE_VERSION}')
        return 0
    parser.error('overlay_bridge is backend-managed: use the pane Show overlay action, '
                 'which supplies scope via /overlay/show. Standalone scope flags and '
                 'unauthenticated --base-url clients are removed.')


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(main())
