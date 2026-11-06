"""Display bridge between the macOS overlay and the existing backend.

Read-only display: forwards bounded transcript and question snapshots to the
native helper. Answers are requested from the authenticated Desktop pane.
The overlay retains no provider credential context and never admits answers.
"""
import argparse
import contextvars as _contextvars
from contextlib import nullcontext
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time

_POPEN_TYPE = subprocess.Popen


def _helper_environment():
    """Give the display helper only ordinary GUI/locale environment values."""
    allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL',
               'LC_CTYPE', 'LC_MESSAGES', 'LC_NUMERIC', 'LC_TIME', 'LC_COLLATE',
               'LC_MONETARY', 'LC_PAPER', 'LC_NAME', 'LC_ADDRESS', 'LC_TELEPHONE',
               'LC_MEASUREMENT', 'LC_IDENTIFICATION',
               'DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR',
               '__CF_USER_TEXT_ENCODING'}
    return {key: value for key, value in os.environ.items()
            if key in allowed}


def _fresh_child_observation(process):
    """Worker-only, nonblocking OS veto of an earlier healthy poll sample.

    Popen.poll() may return None when another waiter owns its waitpid lock;
    returncode=None and signal-zero also cannot distinguish a zombie. Prefer
    non-reaping waitid; on older POSIX Pythons use owned waitpid(WNOHANG) under
    Popen's nonblocking reaper lock. Missing primitives/errors mean unknown.
    Neither observation proves group/pipe cleanup or future physical liveness.
    """
    if isinstance(getattr(process, 'returncode', None), int):
        return None  # contradicts the original healthy sample, not cleanup
    if not isinstance(process, _POPEN_TYPE):
        # Injected display adapters retain their poll-result contract. Actual
        # launched helpers are always Popen and must supply OS evidence.
        return True
    try:
        if all(getattr(os, name, None) is not None
               for name in ('waitid', 'P_PID', 'WEXITED', 'WNOWAIT', 'WNOHANG')):
            result = os.waitid(os.P_PID, process.pid,
                               os.WEXITED | os.WNOWAIT | os.WNOHANG)
            return True if result is None or result.si_pid == 0 else None
        # No guessed libc structures/flags and no PID signals. The fallback
        # may reap, so serialize with Popen and record status on that handle.
        lock = getattr(process, '_waitpid_lock', None)
        if (os.name != 'posix' or not callable(getattr(os, 'waitpid', None))
                or not hasattr(os, 'WNOHANG') or lock is None
                or not callable(getattr(process, '_handle_exitstatus', None))
                or not lock.acquire(False)):
            return None
        try:
            if process.returncode is not None:
                return None
            pid, status = os.waitpid(process.pid, os.WNOHANG)
            if pid == 0:
                return True
            if pid == process.pid:
                getattr(process, '_handle_exitstatus')(status)
            return None
        finally:
            lock.release()
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        return None


# Marker bound to an explicit pane overlay action (show/scope). The
# transport only runs backend answers when the context it captured carries
# the token minted on that action's thread — an off-path construction (or
# an empty copied context) fails closed instead of running provider work
# under an uninherited default.
_PANE_ACTION = _contextvars.ContextVar('meeting_copilot_pane_action', default=None)

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
        explicit = str(segment_id or '').strip()[:200]
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


def _prioritize_warnings(warnings, limit=3):
    """Order overlay warnings so critical ones survive the display cap.

    Interruption/partial, truncation and citation warnings decide whether an
    answer can be trusted, so they always precede routine disclosures. Any
    overflow is disclosed rather than silently dropped.
    """
    seen = []
    for warning in warnings or []:
        if isinstance(warning, str) and warning not in seen:
            seen.append(warning)
    def rank(text):
        lowered = text.lower()
        if 'interrupt' in lowered or 'partial' in lowered:
            return 0
        if 'shorten' in lowered or 'truncat' in lowered or '12,000' in lowered:
            return 1
        if 'citation' in lowered:
            return 2
        return 3
    ordered = sorted(seen, key=rank)
    if len(ordered) > limit:
        return ordered[:limit - 1] + [f'+{len(ordered) - (limit - 1)} more warning(s) in the pane.']
    return ordered


def _admission_failure_message(transport, error):
    """Map only known in-process backend rejections to fixed, safe text."""
    if isinstance(transport, BackendTransport) and error is not None:
        import importlib
        module = importlib.import_module(f'{__package__}.server' if __package__ else 'server')
        if isinstance(error, module._AnswerRejected):
            detail = error.detail
            if error.status_code == 409 and detail in ('Meeting context changed; refresh and retry the question.',
                          'Capture context changed; refresh and retry the question.',
                          'Question changed; refresh and tap the current row.'):
                return 'Meeting or question changed; wait for the current row, then tap to retry.'
            if error.status_code == 409 and isinstance(detail, str) and (detail == 'Too many pending answer requests.' or
                    (detail.startswith('Request ') and detail.endswith(' is already being prepared.'))):
                return 'Another answer is being prepared; wait for it to finish, then tap to retry.'
            if error.status_code == 503 and detail == 'Too many answer workers in flight; wait for one to finish and retry.':
                return 'Answer capacity is busy; wait for an answer to finish, then tap to retry.'
    return 'Answer request failed; tap this row to retry or check the Meeting Copilot pane.'


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
        raw_warnings = [str(w)[:200] for w in (entry.get('warnings') or []) if isinstance(w, str)]
        if entry.get('admission_error'):
            warnings = _prioritize_warnings(raw_warnings, limit=2)
            # Polling may already have summarized warnings; avoid counting
            # that summary as a single warning when reducing the display.
            if len(set(raw_warnings)) > 2:
                warnings[-1] = 'More warnings in the pane.'
            warnings = [entry['admission_error']] + warnings
        else:
            warnings = _prioritize_warnings(raw_warnings)
        sources = [str(s)[:80] for s in (entry.get('source_titles') or []) if isinstance(s, str)][:3]
        questions.append({
            'id': row['id'],
            'question': row['question'],
            'answer': str(answer or '')[:12000] if isinstance(answer, str) else None,
            'busy': bool(entry.get('busy')),
            'streaming': bool(entry.get('streaming')),
            'completed_only': bool(entry.get('completed_only', True)),
            'warnings': warnings,
            'sources': sources,
        })
    snapshot = {'type': 'snapshot', 'transcript': transcript, 'questions': questions,
                'busy': bool(status.get('busy')), 'listening': bool(status.get('listening'))}
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
    """Read-only access to the owning backend; overlay answers use the pane."""

    def __init__(self, state, poll=None, pane_token=None):
        self._state = state
        self._poll = poll
        self._pane_token = pane_token
        self._compat_admission_lock = threading.RLock()

    def pane_context_matches(self):
        # No request context is retained, so identity cannot be compared.
        # Conservatively retire the display generation on every pane update.
        return False

    def refresh_pane_token(self, token):
        self._pane_token = token

    def refresh_credentials(self):
        # The read-only overlay never consumes or retains provider credentials.
        return None

    def admission_guard(self):
        lock = getattr(self._state, 'overlay_lock', None)
        if lock is not None and hasattr(type(lock), '__enter__'):
            return lock
        return self._compat_admission_lock

    def assist(self, question, scope, row_id, expected_generation=None, expected_capture_generation=None):
        raise RuntimeError('Overlay answers are unavailable. Ask in the Meeting Copilot pane.')

    def _assist_locked(self, question, scope, row_id, expected_generation, expected_capture_generation):
        raise RuntimeError('Overlay answers are unavailable. Ask in the Meeting Copilot pane.')

    def assist_request(self, request_id):
        return self._state.assist_request(request_id)

    def cancel(self, request_id):
        try:
            self._state.cancel_assist(request_id)
        except Exception:
            pass

    def poll_status(self):
        if self._poll is not None:
            return self._poll()
        try:
            return self._state.status(touch=False)
        except TypeError:
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
        self.scope_seq = 0
        if isinstance(scope, dict) and isinstance(scope.get('seq'), int):
            self.scope_seq = scope['seq']
        self.scope.pop('seq', None)
        self.poll_interval = poll_interval
        self.assist_timeout = assist_timeout
        self.process = None
        self.lock = threading.RLock()
        self.segments = []
        self.message = None
        self._sticky_error = None
        self.questions = {}
        self.answers = {}
        self.order = []
        self.message = None
        self.request_by_row = {}
        # Generation tag for each True reservation: a stale ask proves it owns
        # the reservation only when the tag matches its click generation.
        # Entries are advisory (bounded by live rows, cleared on scope
        # changes); a tag is only read while its row holds a True mapping,
        # and every new reservation overwrites its row's tag first.
        self.reserved_generation = {}
        self.row_by_request = {}
        self.cancelled = set()
        self.listening = False  # last status truth: rows are never clickable while paused
        self.paused_generation = 0
        self._instance_id = secrets.token_hex(16)
        self._listening = False
        self._paused = False
        self._last_generation = None
        self._last_capture_generation = None
        self._last_status_seq = None
        self._poller = None
        self._poller_lock = threading.Lock()
        self._poller_seq = 0
        self._seq = 0
        self._latest_seq = 0
        self._emitted_seq = 0
        self.pushed = []  # headless record (helper=None); used by tests/dry-run
        self.threads = []
        self.stopping = False
        self._lifecycle_epoch = 0
        self._startup_cancelled = False
        self._admission_lock = threading.RLock()
        import queue as _queue
        self._push_queue = _queue.Queue(maxsize=8)
        self._click_queue = _queue.Queue(maxsize=32)
        self._stdin_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._cleanup_job = None
        self._signal_jobs = {}
        # Only launch-created sessions are eligible for group confirmation.
        # Never infer ownership from getpgid() of an arbitrary injected handle.
        self._process_groups = {}
        self.cleanup_error = None
        self._reader = None
        self._stdout_eof = {}
        self._stdout_pipes = {}
        self._stdout_readers = {}
        self._stdout_busy = set()
        self._reader_failed = set()
        self._probe_lock = threading.Lock()
        self._probe_jobs = {}
        self._show_probe_threads = set()

    # -- backend truth in ------------------------------------------------
    def retire_authorization(self):
        """Even identical settings may carry a different pane principal."""
        with self.lock:
            self.paused_generation += 1
            for request_id in list(self.row_by_request):
                self._cancel_locked(request_id)
            self.request_by_row.clear()
            self.reserved_generation.clear()
            self.answers.clear()

    def set_scope(self, scope):
        """Pane pushes current project/session scope; clears stale meeting data.

        Scope carries a monotonic `seq` from the pane. Older deliveries that
        arrive after newer ones are ignored so a stale scope can never
        overwrite current settings.
        """
        with self.lock:
            scope = dict(scope or {})
            incoming = scope.get('seq')
            scope.pop('seq', None)
            if isinstance(incoming, int):
                if incoming < self.scope_seq:
                    return {'scope': dict(self.scope), 'seq': self.scope_seq, 'ignored_stale': True}
                self.scope_seq = incoming
            if scope != self.scope:
                self.scope = scope
                if self.message in ('Answer not accepted: transport lacks expected_generation safety support; check the pane.',
                                    'Too many pending clicks; wait before retrying.'):
                    self.message = getattr(self, '_sticky_error', None) or default_message(
                        {'listening': self._listening})
                self.questions = {}
                self.answers = {}
                self.order = []
                self.paused_generation += 1
                for request_id in list(self.row_by_request):
                    self._cancel_locked(request_id)
                self.request_by_row = {}
                self.reserved_generation = {}
                # Retired rows leave orphan request ids behind; prune the cancelled
                # set down to ids still referenced by a live row so it stays
                # bounded. (Ids with no live row are discarded in _cancel_locked.)
                live_ids = set(self.row_by_request)
                self.cancelled = {request_id for request_id in self.cancelled if request_id in live_ids}
        return self.push()

    def update(self, status, poller_seq=None):
        if not isinstance(status, dict):
            with self.lock:
                if self.stopping:
                    return False
                if poller_seq is not None and poller_seq != self._poller_seq:
                    return False
                self.message = getattr(self, '_sticky_error', None) or default_message(status)
                self._listening = False
                self._paused = False
            return self.push()
        raw = status.get('segments') if isinstance(status.get('segments'), list) else []
        rows = suggest_questions(raw)
        with self.lock:
            if self.stopping or (poller_seq is not None and poller_seq != self._poller_seq):
                return False
            incoming_seq = status.get('status_seq')
            if self._last_status_seq is not None:
                if type(incoming_seq) is not int or incoming_seq <= self._last_status_seq:
                    return False
            if type(incoming_seq) is int:
                self._last_status_seq = incoming_seq
            else:
                # Legacy injected status sources have no within-epoch order.
                # Never permit even those sources to rewind known epochs.
                for incoming, previous in ((status.get('generation'), self._last_generation),
                                           (status.get('capture_generation'), self._last_capture_generation)):
                    if type(incoming) is int and type(previous) is int and incoming < previous:
                        return False
            # Completion is published before worker-map cleanup. Mapping
            # presence alone must not turn a completed answer back into pending.
            pending_rows = {key for key in self.request_by_row
                            if not self.answers.get(key, {}).get('completed')}
            self.segments = list(raw)
            capture_changed = status.get('capture_generation') != self._last_capture_generation
            self._last_capture_generation = status.get('capture_generation')
            listening = bool(status.get('listening'))
            generation = status.get('generation')
            if generation is None:
                generation = status.get('capture_generation')
            paused_now = not listening
            if paused_now and not self._paused:
                # First sight of a pause/switch/idle revision: retire rows so no
                # stale request can launch after this revision.
                self.paused_generation += 1
                for answer in self.answers.values():
                    answer.pop('admission_error', None)
                for request_id in list(self.row_by_request):
                    self._cancel_locked(request_id)
                self.request_by_row = {k: v for k, v in self.request_by_row.items() if v is True}
            elif not paused_now and self._paused:
                # Resume retires both the stale placeholder and its busy state.
                # The old worker no longer owns this row and cannot reset it.
                for stale in [k for k, v in self.request_by_row.items() if v is True]:
                    self.request_by_row.pop(stale, None)
                    self.reserved_generation.pop(stale, None)
                    if stale in self.answers:
                        self.answers[stale] = {'answer': None, 'busy': False,
                                               'streaming': False, 'completed_only': True}
            self._listening = listening
            self._paused = paused_now
            self.listening = listening
            meeting_changed = generation != self._last_generation
            if meeting_changed or capture_changed:
                # New meeting/session/clear revision retires in-flight rows so
                # late events from older work cannot resurrect stale rows.
                self._last_generation = generation
                self.paused_generation += 1
                for request_id in list(self.row_by_request):
                    self._cancel_locked(request_id)
                # Completed and cancelled/partial answers may no longer have a
                # request mapping, so retire all answer data on this boundary.
                if meeting_changed:
                    self.answers.clear()
                else:
                    # Same-meeting capture pause/resume retires pending work,
                    # not completed answers. Partial/queued text is not complete.
                    for key, answer in list(self.answers.items()):
                        if key in pending_rows or answer.get('busy') or not answer.get('completed'):
                            self.answers.pop(key, None)
                self.request_by_row.clear()
                self.reserved_generation.clear()
            next_questions = {row['id']: row['question'] for row in rows}
            revised = {key for key, text in self.questions.items() if next_questions.get(key) != text}
            if revised:
                # A row id is not question identity: transcript corrections
                # can reuse it. Advance the reservation epoch (including ABA)
                # and release pending taps; keep unrelated admitted answers.
                self.paused_generation += 1
                for key in list(self.request_by_row):
                    request_id = self.request_by_row[key]
                    if request_id is True:
                        self.request_by_row.pop(key, None)
                        self.reserved_generation.pop(key, None)
                        if key in self.answers:
                            self.answers[key]['busy'] = False
                    elif key in revised:
                        self._cancel_locked(request_id)
                for key in revised:
                    self.answers.pop(key, None)
            self.questions = next_questions
            live = set(self.questions)
            for row_id in [key for key in self.answers if key not in live and self.answers[key].get('busy')]:
                request_id = self.request_by_row.get(row_id)
                if request_id:
                    self._cancel_locked(request_id)
            for row_id in [key for key in self.answers if key not in live and not self.answers[key].get('busy')]:
                self.answers.pop(row_id, None)
                self.request_by_row.pop(row_id, None)
                self.reserved_generation.pop(row_id, None)
            while len(self.answers) > 40:
                oldest = next(iter(self.answers))
                request_id = self.request_by_row.pop(oldest, None)
                self.reserved_generation.pop(oldest, None)
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
            self.message = getattr(self, '_sticky_error', None) or default_message(status)
            if not raw and not status.get('busy'):
                for row_id in [key for key in self.answers if not self.answers[key].get('busy')]:
                    self.answers.pop(row_id, None)
                    self.request_by_row.pop(row_id, None)
                    self.reserved_generation.pop(row_id, None)
        return self.push()

    def _prune_threads_locked(self):
        self.threads = [t for t in self.threads if t.is_alive()]

    def _prune_threads(self):
        with self.lock:
            self._prune_threads_locked()

    # -- clicks out -------------------------------------------------------
    @property
    def snapshot_id(self):
        return f'{self._instance_id}:{self.paused_generation}'

    def on_click_line(self, line):
        """The single path that may request an answer: an explicit overlay click."""
        row_id = parse_click(line)
        if row_id is None:
            return False
        # Reject even an old or modified helper's click. No pending provider
        # work is created and no retained pane credentials are consulted.
        if isinstance(self.transport, BackendTransport):
            return False
        queued = False
        with self.lock:
            if json.loads(line).get('snapshot_id') != self.snapshot_id:
                return False
            if self.stopping or not self._listening:
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
            state.pop('admission_error', None)
            state.pop('warnings', None)
            state.pop('source_titles', None)
            state.pop('completed', None)
            # Reserve synchronously: a second immediate click sees busy even
            # before the worker thread drains the queue.
            self.request_by_row[row_id] = True
            self.reserved_generation[row_id] = click_generation
            try:
                self._click_queue.put_nowait((row_id, question, click_generation))
                queued = True
            except Exception:
                self.request_by_row.pop(row_id, None)
                self.reserved_generation.pop(row_id, None)
                state['busy'] = False
                state['admission_error'] = 'Too many pending clicks; wait, then tap this row to retry.'
                self.message = 'Too many pending clicks; wait before retrying.'
        # push -> _emit takes stdin then bridge; never call it under bridge.
        self.push()
        if not queued:
            return False
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
        # Serialize with local scope/pause updates, then bind admission to the
        # backend generation that produced the displayed question. The backend
        # can advance before our next poll; only its own lock can close that
        # race. Lock order: backend overlay publication -> bridge -> state.
        # Never acquire the publication guard while already holding bridge.lock.
        guard = transport.admission_guard() if isinstance(transport, BackendTransport) else nullcontext()
        with guard, self.lock:
            if (self.stopping or not self._listening or self.request_by_row.get(row_id) is not True
                    or click_generation != self.paused_generation):
                owner = self.request_by_row.get(row_id)
                own_reservation = (owner is True and self.reserved_generation.get(row_id, click_generation) == click_generation)
                if own_reservation or (owner is None and click_generation == self.paused_generation):
                    # Our own reservation (or none): release it and reset the
                    # row. A live mapping owned by another request — or a
                    # newer tap's reservation — is never touched: a stale tap
                    # must not erase newer state.
                    self.request_by_row.pop(row_id, None)
                    self.reserved_generation.pop(row_id, None)
                    if row_id in self.answers:
                        self.answers[row_id] = {'answer': None, 'busy': False,
                                                'streaming': False, 'completed_only': True}
                    self.message = 'Meeting context changed before the tap was sent; tap again to retry.'
                request_id = None
            else:
                admission_error = None
                try:
                    if isinstance(transport, BackendTransport):
                        if self._last_generation is None or self._last_capture_generation is None:
                            raise RuntimeError('backend snapshot lacks admission generations')
                        # guard is already held, including for plain Lock injections.
                        request_id = transport._assist_locked(
                            question, dict(self.scope), row_id,
                            self._last_generation, self._last_capture_generation)
                    else:
                        request_id = transport.assist(question, dict(self.scope), row_id,
                                                      expected_generation=self._last_generation)
                except AssertionError:
                    raise  # Programming/test failures are not admission rejections.
                except Exception as exc:
                    request_id = None
                    admission_error = exc
                if not isinstance(request_id, str) or not request_id:
                    # An injected transport can re-enter and retire this tap.
                    # Never replace a newer reservation, answer, or revision.
                    if (self.request_by_row.get(row_id) is True
                            and self.reserved_generation.get(row_id, click_generation) == click_generation
                            and click_generation == self.paused_generation
                            and self.questions.get(row_id) == question and row_id in self.answers):
                        self.request_by_row.pop(row_id, None)
                        self.reserved_generation.pop(row_id, None)
                        self.answers[row_id] = {'answer': None, 'busy': False,
                                                'streaming': False, 'completed_only': True,
                                                'admission_error': _admission_failure_message(transport, admission_error)}
                        if isinstance(admission_error, TypeError) and not isinstance(transport, BackendTransport):
                            # Compatibility status for an injected legacy transport
                            # that cannot accept the safety precondition. Never retry it.
                            import inspect
                            try:
                                inspect.signature(transport.assist).bind(
                                    question, dict(self.scope), row_id,
                                    expected_generation=self._last_generation)
                            except TypeError:
                                self.message = 'Answer not accepted: transport lacks expected_generation safety support; check the pane.'
                            except (ValueError, AttributeError):
                                pass
                    request_id = None
                elif (self.request_by_row.get(row_id) is not True
                      or click_generation != self.paused_generation):
                    # Injected transports may re-enter state mutation. Never
                    # replace a newer owner with their returned orphan slot.
                    try:
                        transport.cancel(request_id)
                    except AssertionError:
                        raise
                    except Exception:
                        pass
                    request_id = None
                else:
                    # Publish ownership BEFORE leaving the admission guard.
                    # Hide can now always cancel the admitted backend slot.
                    self.request_by_row[row_id] = request_id
                    self.reserved_generation.pop(row_id, None)
                    self.row_by_request[request_id] = row_id
                    # Preserve injected single-slot reuse hygiene.
                    for other, rid in list(self.request_by_row.items()):
                        if other != row_id and rid == request_id:
                            self.request_by_row.pop(other, None)
                            self.reserved_generation.pop(other, None)
        if request_id is None:
            self.push()
            return
        deadline = time.monotonic() + self.assist_timeout
        seen = ''
        streaming = False
        try:
            while time.monotonic() < deadline and not self.stopping:
                with self.lock:
                    if (request_id in self.cancelled or row_id not in self.answers
                            or self.request_by_row.get(row_id) != request_id):
                        return
                try:
                    entry = transport.assist_request(request_id)
                except AssertionError:
                    raise
                except Exception:
                    time.sleep(self.poll_interval)
                    continue
                if not isinstance(entry, dict):
                    time.sleep(self.poll_interval)
                    continue
                if entry.get('row_id') and entry['row_id'] != row_id:
                    with self.lock:
                        # A poll can return after retirement and a newer answer
                        # has completed (leaving no owner). Only the current
                        # owner may clear the row or publish a mismatch error.
                        if self.request_by_row.get(row_id) != request_id:
                            return
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
                        if self.request_by_row.get(row_id) == request_id and row_id in self.answers:
                            self.answers[row_id] = {'answer': seen or None, 'busy': False,
                                                    'streaming': False, 'completed_only': False,
                                                    'completed': False,
                                                    'warnings': ['Answer cancelled; the text above is partial.'] if seen else []}
                    self.push()
                    return
                if answer != seen:
                    seen = answer
                    streaming = bool(entry.get('streaming')) or streaming
                    with self.lock:
                        if self.request_by_row.get(row_id) == request_id and row_id in self.answers:
                            self.answers[row_id] = {'answer': seen or None, 'busy': True,
                                                    'streaming': streaming,
                                                    'completed_only': bool(entry.get('completed_only', not streaming))}
                    self.push()
                if entry.get('status') in ('done', 'error'):
                    with self.lock:
                        if self.request_by_row.get(row_id) == request_id and row_id in self.answers:
                            warnings = _prioritize_warnings([str(w)[:200] for w in (entry.get('warnings') or []) if isinstance(w, str)])
                            titles = []
                            for source in (entry.get('sources') or [])[:3]:
                                if isinstance(source, dict) and source.get('title'):
                                    titles.append(str(source['title'])[:80])
                            self.answers[row_id] = {'answer': seen or None, 'busy': False,
                                                    'streaming': False,
                                                    'completed_only': bool(entry.get('completed_only', not streaming)),
                                                    'warnings': warnings, 'source_titles': titles,
                                                    'completed': entry.get('status') == 'done'}
                            if entry.get('status') == 'error':
                                self.answers[row_id]['admission_error'] = (
                                    'Answer failed; tap this row to retry or check the Meeting Copilot pane.'
                                    + (' The text above may be partial.' if seen else ''))
                    self.push()
                    return
                time.sleep(self.poll_interval)
            with self.lock:
                try:
                    transport.cancel(request_id)
                except Exception:
                    pass
                if self.request_by_row.get(row_id) == request_id and row_id in self.answers:
                    kept = self.answers[row_id]
                    kept = dict(kept) if isinstance(kept, dict) else {}
                    kept.update({'busy': False, 'streaming': False, 'completed_only': not streaming,
                                 'warnings': _prioritize_warnings(
                                     list(kept.get('warnings') or []) +
                                     ['Answer timed out or was cleared; the text above may be partial.'])})
                    self.answers[row_id] = kept
                self.message = 'Answer timed out or was cleared; try again from the pane.'
            self.push()
        finally:
            with self.lock:
                # Conditional cleanup: a newer request may have taken over
                # this row while this worker exited — never remove its
                # ownership or reservation.
                if self.request_by_row.get(row_id) == request_id:
                    self.request_by_row.pop(row_id, None)
                if self.row_by_request.get(request_id) == row_id:
                    self.row_by_request.pop(request_id, None)
                # Worker exited: its id can never arrive late, so drop it from
                # the cancelled set and keep that set bounded to live rows.
                self.cancelled.discard(request_id)

    def _cancel_locked(self, request_id):
        self.cancelled.add(request_id)
        row_id = self.row_by_request.pop(request_id, None)
        if row_id:
            self.request_by_row.pop(row_id, None)
            self.reserved_generation.pop(row_id, None)
            state = self.answers.get(row_id)
            if state is not None and state.get('busy'):
                state['busy'] = False
        else:
            # No live row holds this id (scope/pause retirement orphan): drop it
            # immediately so the cancelled set never grows without bound.
            self.cancelled.discard(request_id)
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
        if getattr(self, '_protocol_error', None):
            return {'available': False, 'error': self._protocol_error}
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

    def _check_helper_protocol(self):
        # A bounded, display-free probe: never send meeting data to an old
        # helper that cannot echo presentation identity on native clicks.
        self._protocol_error = None
        if not isinstance(self.helper_path, str):
            return False
        target = None
        valid = False
        try:
            # Never use run/Popen's context manager here: its __exit__ waits
            # indefinitely if timeout-triggered kill fails. Own the probe just
            # like a display until the bounded reaper proves termination.
            target = subprocess.Popen(
                [self.helper_path] + list(getattr(self, 'helper_args', None) or []) + ['--protocol-version'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=False, start_new_session=True,
                env=_helper_environment())
            with self.lock:
                self._process_groups[target] = target.pid
                self._register_stdout_locked(target)
                self._stdout_busy.add(target)
                self.process = target
            try:
                output, _ = target.communicate(timeout=2)
                # Binary communicate finishes the actual read before decoding.
                # Text-mode communicate can close stdout then raise on invalid
                # bytes, stranding an unset EOF obligation on a closed stream.
                self._stdout_eof[target].set()
                output = output.decode('utf-8', errors='strict')
            finally:
                with self.lock:
                    self._stdout_busy.discard(target)
            valid = target.returncode == 0 and output.strip() == 'meeting-overlay/2'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        finally:
            if target is not None:
                valid = self._reap_locked(target) and valid
        if valid:
            return True
        self._protocol_error = ('Overlay helper protocol is outdated or incompatible. '
                                'Update the plugin sources and run python prepare_macos.py '
                                '--home <active-profile-home> to rebuild it, then Show overlay again. '
                                'Snapshot identity is required; '
                                'legacy clicks are never accepted.')
        return False

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
        if not self._check_helper_protocol():
            return None  # retain an unconfirmed probe for Hide / Clear
        with self.lock:
            if self.stopping:
                return None
        argv = [self.helper_path] + list(getattr(self, 'helper_args', None) or [])
        try:
            target = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                      encoding='utf-8', errors='strict', start_new_session=True,
                                      env=_helper_environment())
            with self.lock:
                self._process_groups[target] = target.pid
                self._register_stdout_locked(target)
                self.process = target
                self._start_stdout_reader(target)
        except (OSError, ValueError, TypeError):
            self.process = None
            return None
        return self.process

    def _show_process_alive(self, process):
        """Bound direct reuse inspection to one live probe across all children."""
        with self._probe_lock:
            # Completion is not thread exit. Hold the slot until the worker
            # actually exits, even if Hide has already reaped its old child.
            self._probe_jobs = {p: j for p, j in self._probe_jobs.items() if j['worker'].is_alive()}
            job = self._probe_jobs.get(process)
            if job is not None and job['done'].is_set():
                return None  # publication is not a new observation
            if job is None:
                if self._probe_jobs:
                    return None  # never apply another generation's result
                job = {'done': threading.Event(), 'alive': None, 'joined': 0}
                self._probe_jobs[process] = job

                def inspect_process():
                    alive = None
                    try:
                        alive = process.poll() is None
                        if alive:
                            for _ in range(8):
                                with self._probe_lock:
                                    joined = job['joined']
                                alive = _fresh_child_observation(process)
                                with self._probe_lock:
                                    if alive is not True or joined == job['joined']:
                                        job['alive'] = alive
                                        job['done'].set()
                                        return
                            alive = None
                    except Exception:
                        alive = None
                    with self._probe_lock:
                        job['alive'] = alive
                        job['done'].set()

                worker = threading.Thread(target=inspect_process, name='overlay-show-probe', daemon=True)
                job['worker'] = worker
                # Only prune our own finished probes. Other registered threads
                # may not have started yet; is_alive() would discard those.
                with self.lock:
                    finished = {t for t in self._show_probe_threads if not t.is_alive()}
                    self.threads = [t for t in self.threads if t not in finished]
                    self._show_probe_threads.difference_update(finished)
                    self._show_probe_threads.add(worker)
                    self.threads.append(worker)
                    worker.start()
            job['joined'] += 1
        if not job['done'].wait(.05):
            return None
        if job['alive'] is True and isinstance(getattr(process, 'returncode', None), int):
            return None
        return job['alive']

    def show_overlay(self):
        """Explicit pane action: open the overlay window, start forwarding."""
        if self.process in self._reader_failed:
            return {'shown': False, 'error': 'Overlay reader failed; retry Hide before Show.'}
        if self.stopping and self.process is not None:
            return {'shown': None, 'stop_confirmed': False,
                    'error': self.cleanup_error or 'Overlay shutdown is not confirmed; retry Hide.'}
        if self.process is not None:
            process = self.process
            alive = self._show_process_alive(process)
            with self.lock:
                # EOF/retirement can publish while the bounded probe runs.
                # Commit reuse only against the same healthy lifecycle.
                if process in self._reader_failed:
                    return {'shown': False, 'error': 'Overlay reader failed; retry Hide before Show.'}
                if self.stopping or self.process is not process or alive is None:
                    return {'shown': None, 'stop_confirmed': False,
                            'error': 'Overlay health is uncertain; shutdown is not confirmed. Retry Hide.'}
                if alive is True and not isinstance(getattr(process, 'returncode', None), int):
                    return {'shown': True, 'reused': True}
            # Reaped or exited helper: fall through to a clean relaunch.
            if not self._reap_locked(None):
                return {'shown': None, 'stop_confirmed': False, 'error': self.cleanup_error}
        with self.lock:
            if getattr(self, '_startup_cancelled', False):
                return {'shown': False, 'error': 'Overlay startup was retired.'}
            self.stopping = False
        process = self.start_helper()
        with self.lock:
            if self.stopping:
                return {'shown': False, 'error': 'Overlay startup was retired.'}
            # Fresh helper lifecycle: never suppress the first snapshot.
            self._last_emit_digest = None
        if process is None:
            status = self.helper_status()
            with self.lock:
                self.message = status.get('error')
            return {'shown': False, 'available': False,
                    'error': status.get('error')}
        if process.stdout is not None:
            self._start_stdout_reader(process)
        self._start_pusher_locked()
        self._start_poller_locked()
        self.push()
        # Cached Popen evidence only: no potentially blocking poll on caller.
        if self.stopping or isinstance(getattr(process, 'returncode', None), int):
            return {'shown': False, 'error': 'Overlay helper exited or was retired during startup.'}
        if process in self._reader_failed:
            return {'shown': False, 'error': 'Overlay reader failed during startup; retry Hide.'}
        return {'shown': True, 'reused': False}

    def hide_overlay(self):
        """Explicit pane action: close the window, cancel rows, reap the group."""
        confirmed = self.stop()
        return {'shown': False if confirmed else None, 'stop_confirmed': confirmed,
                'error': self.cleanup_error}

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
            pane_context = _contextvars.Context()  # never inherit pane credentials
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
                    self.update(status, poller_seq=seq)
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
            self._emit(snapshot, seq)

    def _emit(self, snapshot, seq=None):
        if not isinstance(snapshot, dict):
            return False
        # Single ordered writer: a snapshot whose sequence has been
        # superseded by a newer push must never reach the helper, whether
        # it arrives via the pump queue or a direct call.
        if seq is not None:
            with self.lock:
                if seq != getattr(self, '_latest_seq', seq):
                    return True
        truncated = False
        try:
            data = json.dumps(snapshot, ensure_ascii=False) + '\n'
        except (TypeError, ValueError):
            return False
        if len(data.encode('utf-8')) > self._SNAPSHOT_MAX_BYTES:
            snapshot = dict(snapshot)
            snapshot['transcript'] = []
            snapshot['truncated'] = True
            try:
                questions = snapshot.get('questions')
                if isinstance(questions, list):
                    bounded = []
                    for row in questions[:20]:
                        if not isinstance(row, dict):
                            continue
                        row = dict(row)
                        answer = row.get('answer')
                        if isinstance(answer, str) and len(answer) > 2000:
                            row['answer'] = answer[:2000] + '… (shortened for overlay; open the pane for full text)'
                        bounded.append(row)
                    snapshot['questions'] = bounded
            except Exception:
                pass
            base_message = snapshot.get('message') or ''
            notice = ('Long answers shortened for the overlay; open the pane for full text.'
                      if any(isinstance(row, dict) and isinstance(row.get('answer'), str) and len(row['answer']) > 2000
                             for row in (snapshot.get('questions') or [])[:20])
                      else 'Snapshot shortened for the overlay; open the pane for full text.')
            snapshot['message'] = f'{base_message} {notice}'.strip() if base_message else notice
            try:
                data = json.dumps(snapshot, ensure_ascii=False) + '\n'
            except (TypeError, ValueError):
                return False
            if len(data.encode('utf-8')) > self._SNAPSHOT_MAX_BYTES:
                return False
            truncated = True
        # Suppress identical consecutive snapshots so idle polls don't rewrite.
        # The digest commits only after successful delivery so a failed write
        # or helper restart never suppresses a snapshot the helper never got.
        try:
            digest = hashlib.sha256(data.encode('utf-8')).hexdigest()
        except Exception:
            digest = None
        # One serialization lock across validation and delivery: sequence and
        # digest checks, the write (or headless append), and bookkeeping all
        # happen atomically, so a newer snapshot can never be followed by an
        # older one on either path. The bounded fd write nests bridge.lock;
        # bridge.lock is never held while acquiring the stdin lock.
        failed_process = None
        with self._stdin_lock, self.lock:
            if self.stopping:
                return False
            if snapshot.get('snapshot_id', self.snapshot_id) != self.snapshot_id:
                return False
            if digest is not None and digest == getattr(self, '_last_emit_digest', None):
                return True
            if seq is not None and seq != self._latest_seq:
                return True
            process = self.process
            try:
                if process is None or process.stdin is None:
                    self.pushed.append(snapshot)
                else:
                    self._write_frame(process.stdin, data.encode('utf-8'))
            except (OSError, ValueError, TypeError, AttributeError):
                # Partial frames are not reusable: stop admission immediately,
                # then reap outside bridge/stdin locks. Never flush buffered IO.
                self._retire_lifecycle_locked()
                failed_process = process
            else:
                if seq is not None:
                    self._emitted_seq = max(self._emitted_seq, seq)
                self._last_emit_digest = digest
                return True
        self._broken_pipe_shutdown(failed_process)
        return False

    @staticmethod
    def _write_frame(stream, data):
        """Bound every real pipe write; arbitrary file-like writers unsupported.

        Never call TextIO.write/flush: either can block forever on a stalled
        helper. Holding bridge.lock across <=100ms of nonblocking fd IO makes
        invalidation a publication fence without holding it across teardown.
        """
        import os
        import select
        fd = stream.fileno()
        if type(fd) is not int:
            raise TypeError('overlay stdin requires a real pipe descriptor')
        os.set_blocking(fd, False)
        deadline = time.monotonic() + 0.1
        remaining = memoryview(data)
        while remaining:
            if time.monotonic() >= deadline:
                raise TimeoutError('overlay helper stopped reading')
            try:
                written = os.write(fd, remaining)
                if not written:
                    raise BrokenPipeError('overlay pipe closed')
                remaining = remaining[written:]
            except BlockingIOError:
                select.select([], [fd], [], max(0, deadline - time.monotonic()))

    def _broken_pipe_shutdown(self, failed_process):
        # Broken stdin means the helper exited: stop pumps/readers and reap so
        # no later push resurrects writes to a dead pipe, without taking
        # _stdin_lock inside the reader path (the old stop()-under-lock hang).
        current = self.process
        if current is not None and failed_process is not None and current is not failed_process:
            return
        with self.lock:
            self._retire_lifecycle_locked()
        with self._poller_lock:
            self._poller_seq += 1
            self._poller = None
        try:
            self._reap_locked(failed_process if failed_process is not None else current)
        except OSError:
            pass

    def push(self):
        """Render current state; headless (helper=None) records for tests.

        State capture, snapshot build, and sequence assignment happen under
        one lock: an older in-flight capture can never be sequenced newer
        than a state it did not observe.
        """
        with self.lock:
            rows = [{'id': row_id, 'question': self.questions[row_id]} for row_id in self.order if row_id in self.questions]
            segments = list(self.segments)
            answers = {row_id: dict(state) for row_id, state in self.answers.items()}
            message = self.message
            if isinstance(self.transport, BackendTransport):
                notice = 'Read-only overlay. Ask questions in the Meeting Copilot pane.'
                message = f'{message} · {notice}' if message else notice
            elif isinstance(self.scope.get('web_query'), str) and self.scope['web_query'].strip():
                notice = 'Public web query active for row requests; edit or clear it in the pane.'
                message = f'{message} · {notice}' if message else notice
            busy = any(state.get('busy') for state in answers.values())
            listening = bool(self._listening)
            snapshot = build_snapshot({'segments': segments, 'busy': busy, 'listening': listening},
                                      answers, message)
            if isinstance(self.transport, BackendTransport):
                snapshot['answer_actions'] = False
            snapshot['snapshot_id'] = self.snapshot_id
            # Preserve pinned row order so streaming rows do not jump around.
            order_index = {row_id: index for index, row_id in enumerate(self.order)}
            snapshot['questions'] = sorted(snapshot.get('questions', []),
                                           key=lambda row: order_index.get(row.get('id'), 10 ** 6))
            # Single serialized writer: assign a monotonic sequence, keep only the
            # newest snapshot in the bounded queue, and emit in order. Late or
            # duplicate older snapshots can never overwrite newer answers.
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
        # Helper IO belongs only to the writer pump, never to pane publication
        # (which may hold overlay_lock). Headless tests remain synchronous.
        if self.process is not None and self.process.stdin is not None:
            return True
        return self._emit(snapshot, seq)

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
            self._start_stdout_reader(process)
        self._start_pusher_locked()
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

    def _register_stdout_locked(self, process):
        # Call under bridge.lock BEFORE publishing a newly born process. Even
        # cancelled startup owns an unset EOF obligation for its original pipe.
        if process.stdout is not None and process not in self._stdout_eof:
            self._stdout_eof[process] = threading.Event()
            self._stdout_pipes[process] = process.stdout
            self._stdout_readers[process] = None

    def _start_stdout_reader(self, process):
        with self.lock:
            self._register_stdout_locked(process)  # injected test launchers too
            if (self.stopping or process is not self.process or process.stdout is None
                    or self._stdout_readers.get(process) is not None):
                return
            reader = threading.Thread(target=self._read_clicks, args=(process,),
                                      name='overlay-click-reader', daemon=True)
            self._reader = reader
            self._stdout_readers[process] = reader
            self.threads.append(reader)
            reader.start()  # registration/start atomic to cleanup's snapshot

    def _read_clicks(self, process=None):
        process = self.process if process is None else process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                # A retired reader never publishes errors or clicks, including
                # a line delivered only after a failed shutdown is retried.
                if self.stopping or process is not self.process:
                    continue  # drain without publishing; only actual EOF is evidence
                try:
                    import json as _json
                    event = _json.loads(line) if isinstance(line, str) else None
                    if isinstance(event, dict) and event.get('type') == 'error':
                        code = str(event.get('code') or 'overlay')
                        with self.lock:
                            if self.stopping or process is not self.process:
                                continue
                            if code == 'hotkey':
                                self._sticky_error = 'Overlay shortcut unavailable — overlay stays click-through. Use the pane Show/Hide actions instead.'
                            else:
                                self._sticky_error = f'Overlay helper reported an error ({code[:60]}).'
                            self.message = self._sticky_error
                        self.push()
                        continue
                except Exception:
                    pass
                self.on_click_line(line)
            else:
                # Only normal iterator exhaustion proves EOF, never an early
                # reader return/break or IO exception.
                with self.lock:
                    # EOF proves pipe retirement, but an active display can no
                    # longer deliver clicks even if its process is still alive.
                    # Retired readers must not taint a replacement lifecycle.
                    if not self.stopping and process is self.process:
                        self._fail_reader_locked(process)
                    eof = self._stdout_eof.get(process)
                    if eof is not None:
                        eof.set()
        except (OSError, ValueError):
            with self.lock:
                self._fail_reader_locked(process)

    def _fail_reader_locked(self, process):
        # Caller owns bridge.lock; use the same small fence as retirement.
        # Publishing failure and invalidating tickets is atomic with every
        # capture guard, without taking controller/state locks from a reader.
        with self._admission_lock:
            self._reader_failed.add(process)
            if not self.stopping and process is self.process:
                self._lifecycle_epoch += 1

    def _recover_stdout_eof(self, target, reader):
        """Cleanup-only raw drain after the original reader has exited.

        The serialized reaper owns this drain; no replacement blocking reader,
        no decoding, no publication, and no EOF inferred from errors/timeouts.
        Retain the original pipe object so a newer helper's fd is never used.
        """
        import os
        import stat
        eof = self._stdout_eof.get(target)
        stream = self._stdout_pipes.get(target)
        if (eof is None or eof.is_set() or stream is None
                or stream is not getattr(target, 'stdout', None)
                or (reader is not None and reader.is_alive())
                or sys.platform not in ('darwin', 'linux')):
            return
        try:
            fd = stream.fileno()
            if stream.closed or not stat.S_ISFIFO(os.fstat(fd).st_mode):
                return
            os.set_blocking(fd, False)
            deadline = time.monotonic() + .05
            while time.monotonic() < deadline:
                if os.read(fd, 65536) == b'':
                    eof.set()
                    return
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # includes EAGAIN: a live holder is not EOF

    def _signal_bounded(self, target, name):
        # OS signaling normally returns immediately. Faulty wrappers / OS calls
        # need not. Keep at most one in-flight call per signal and process;
        # a hung TERM must not prevent an independent KILL or leak retry threads.
        key = (target, name)
        with self._cleanup_lock:
            worker = self._signal_jobs.get(key)
            if worker is None or not worker.is_alive():
                def signal_child():
                    try:
                        getattr(target, name)()
                    except Exception:
                        pass  # poll, not signal delivery, proves termination
                worker = threading.Thread(target=signal_child, name='overlay-signal', daemon=True)
                self._signal_jobs[key] = worker
                worker.start()
        worker.join(.05)

    def _group_gone(self, target):
        """Observe only our launch-created group; never signal a stale PGID.

        Popen.poll/communicate may already have reaped the session leader on
        another thread. A check-then-killpg would race PID/group reuse, so only
        the owned Popen receives TERM/KILL. Surviving descendants deliberately
        leave shutdown unconfirmed. A reused group also fails closed (it is
        never signaled). ESRCH proves absence only of this process group, not
        daemonized descendants that escaped it; helpers must not daemonize.
        """
        import os
        pgid = self._process_groups.get(target)
        if pgid is None:
            return True  # injected non-session handle: parent + reader only
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def _reap_locked(self, process):
        """Bounded caller wait; no buffered IO or process methods on caller.

        Despite the historic name, never hold bridge.lock across this call.
        Only the caller publishes the outcome. A late worker cannot clear a
        newer process or resurrect an invalidated bridge.
        """
        with self.lock:
            target = process if process is not None else self.process
            reader = self._stdout_readers.get(target, self._reader)
        if target is None:
            self.cleanup_error = None
            return True
        with self._cleanup_lock:
            job = self._cleanup_job
            if job is None or job['target'] is not target or (job['done'].is_set() and not job['confirmed']):
                job = {'target': target, 'done': threading.Event(), 'confirmed': False}
                self._cleanup_job = job
                def reap():
                    try:
                        for name in ('terminate', 'kill'):
                            if target.poll() is not None:
                                break
                            self._signal_bounded(target, name)
                            deadline = time.monotonic() + .15
                            while target.poll() is None and time.monotonic() < deadline:
                                time.sleep(.01)
                        parent_gone = target.poll() is not None
                        group_gone = self._group_gone(target)
                        # An escaped/inherited pipe can outlive even the group.
                        # Never publish success before the reader has finished,
                        # and never strand a reaper in an unbounded reader join.
                        if parent_gone and group_gone and reader is not None and reader is not threading.current_thread():
                            reader.join(.05)
                        reader_gone = (target not in self._stdout_busy
                                       and (reader is None or not reader.is_alive()))
                        if parent_gone and reader_gone:
                            self._recover_stdout_eof(target, reader)
                        eof = self._stdout_eof.get(target)
                        pipe_eof = (getattr(target, 'stdout', None) is None
                                    or (eof is not None and eof.is_set()))
                        job['confirmed'] = parent_gone and group_gone and reader_gone and pipe_eof
                        if job['confirmed']:
                            for stream in (getattr(target, 'stdout', None), getattr(target, 'stdin', None)):
                                if stream is not None:
                                    stream.close()
                    except Exception:
                        job['confirmed'] = False
                    finally:
                        job['done'].set()
                job['worker'] = threading.Thread(target=reap, name='overlay-reaper', daemon=True)
                job['worker'].start()
        job['done'].wait(.5)
        confirmed = job['done'].is_set() and job['confirmed'] and job['target'] is target
        if confirmed:
            if self.process is target:
                self.process = None
            self._process_groups.pop(target, None)
            self._stdout_eof.pop(target, None)
            self._stdout_pipes.pop(target, None)
            self._stdout_readers.pop(target, None)
            self._reader_failed.discard(target)
            self.cleanup_error = None
        else:
            self.cleanup_error = ('Overlay shutdown is not confirmed; the helper, its process group, '
                                  'or pipe reader may still be alive and meeting content may remain visible. '
                                  'Retry Hide / Clear. If it persists, quit the overlay helper and its descendants.')
        return confirmed

    def _retire_lifecycle_locked(self):
        """Invalidate queued taps as well as admitted slots on every exit."""
        with self._admission_lock:
            self.stopping = True
            self._lifecycle_epoch += 1
        self._listening = self.listening = False
        self.paused_generation += 1
        for request_id in list(self.row_by_request):
            self._cancel_locked(request_id)
        self.request_by_row.clear()
        self.reserved_generation.clear()
        self.answers.clear()
        self.questions.clear()
        self.order.clear()
        self.segments.clear()
        while True:
            try:
                self._click_queue.get_nowait()
            except Exception:
                break

    def invalidate(self):
        """Close admission before the backend detaches us; no pipe IO/join."""
        with self.lock:
            self._retire_lifecycle_locked()
            self._sticky_error = None

    def stop(self):
        self.invalidate()
        with self._poller_lock:
            self._poller_seq += 1
            self._poller = None
        # Drain the coalescing queue so late snapshots cannot leak after exit.
        try:
            while True:
                self._push_queue.get_nowait()
        except Exception:
            pass
        return self._reap_locked(self.process)


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
