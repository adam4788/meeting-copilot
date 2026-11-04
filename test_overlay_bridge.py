"""Overlay bridge tests; stdlib only, no GUI, network, devices, or permissions."""
import json
import unittest
from thread_test_support import CheckedThreads
from contextlib import contextmanager

import overlay_bridge as bridge


class ActiveWebNoticeTests(CheckedThreads):
    def test_native_snapshot_discloses_active_query_and_clears_it(self):
        overlay = bridge.OverlayBridge(scope={'web_query': 'public docs'})
        overlay.push()
        self.assertIn('Public web query active', overlay.pushed[-1]['message'])
        self.assertIn('pane', overlay.pushed[-1]['message'])
        overlay.set_scope({})
        overlay.push()
        self.assertNotIn('Public web query active', overlay.pushed[-1].get('message') or '')


class _SlotProcess:
    """Fake helper process: no real spawn, works on every platform.

    poll() None means alive; terminate()/kill() mark it exited. stdin/stdout
    are None so snapshots record to `pushed` and no reader thread starts.
    """

    def __init__(self, exit_code=None):
        self.stdin = None
        self.stdout = None
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code

    def terminate(self):
        self._exit_code = 0

    def kill(self):
        self._exit_code = 0

    def wait(self, timeout=None):
        return self._exit_code if self._exit_code is not None else 0


@contextmanager
def _fake_overlay_spawn():
    """Fake a live overlay helper: no real subprocess on any platform."""
    from unittest.mock import patch as _patch

    def fake_start(self):
        self.process = _SlotProcess()
        return self.process

    with _patch.object(bridge.OverlayBridge, '_helper_available', lambda self: True), \
            _patch.object(bridge.OverlayBridge, 'start_helper', fake_start):
        yield


class SnapshotTests(CheckedThreads):
    def test_question_id_is_stable_as_transcript_window_rolls(self):
        question = {'speaker': 'remote', 'text': 'Who approves?'}
        before = bridge.suggest_questions([{'text': 'earlier'}, question])[0]['id']
        after = bridge.suggest_questions([question])[0]['id']
        self.assertEqual(before, after)

    def test_question_rows_come_from_transcript_questions_only(self):
        segments = [
            {'speaker': 'remote', 'text': 'Just a status update, no action.'},
            {'speaker': 'remote', 'text': 'What approval does the rollout need?'},
            {'speaker': 'remote', 'text': 'WHAT APPROVAL DOES THE ROLLOUT NEED?  '},
            {'id': 'fixed', 'speaker': 'remote', 'text': 'When is the review?'},
        ]
        rows = bridge.suggest_questions(segments)
        self.assertEqual([r['question'] for r in rows],
                         ['What approval does the rollout need?', 'When is the review?'])
        self.assertEqual(rows[-1]['id'], 'fixed')

    def test_snapshot_bounds_transcript_and_questions(self):
        status = {'segments': [
            {'speaker': 's', 'text': f'line {i}'} for i in range(10)] + [
            {'speaker': 's', 'text': f' item {i}?'} for i in range(25)]}
        snapshot = bridge.build_snapshot(status)
        self.assertEqual(len(snapshot['transcript']), bridge.TRANSCRIPT_LINES)
        self.assertEqual(len(snapshot['questions']), bridge.QUESTION_ROWS)
        self.assertTrue(all(set(q) == {'id', 'question', 'answer', 'busy', 'streaming', 'completed_only', 'warnings', 'sources'} for q in snapshot['questions']))
        json.dumps(snapshot)  # Swift decodes exactly this shape

    def test_paused_backend_produces_honest_message(self):
        status = {'segments': [], 'listening': False}
        snapshot = bridge.build_snapshot(status, message=bridge.default_message(status))
        message = str(snapshot.get('message') or '')
        self.assertIn('Paused', message)
        self.assertNotIn('stream', message.lower())


class ClickTests(CheckedThreads):
    def test_click_line_parsing_ignores_garbage(self):
        self.assertIsNone(bridge.parse_click('not json'))
        self.assertIsNone(bridge.parse_click('{"type":"snapshot"}'))
        self.assertIsNone(bridge.parse_click('{"type":"click","id":""}'))
        self.assertEqual(bridge.parse_click('{"type":"click","id":"q-1"}'), 'q-1')

    def _slot_transport(self, calls, answers_by_row=None, delay=0.0):
        import threading
        import time
        answers_by_row = answers_by_row or {}
        lock = threading.Lock()
        slots = {}
        seq = [0]
        delay = 0.05

        class SlotTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                with lock:
                    calls.append({'question': question, 'scope': dict(scope), 'row_id': row_id})
                    seq[0] += 1
                    request_id = f'r-{seq[0]}'
                    chunks = list(answers_by_row.get(row_id, ['Two reviewers.']))
                    slots[request_id] = {'question': question, 'row_id': row_id, 'chunks': chunks,
                                         'index': 0, 'status': 'streaming'}
                    return request_id

            def assist_request(self, request_id):
                time.sleep(delay)
                with lock:
                    slot = slots.get(request_id)
                    if slot is None:
                        return {'request_id': request_id, 'status': 'cancelled', 'cancelled': True}
                    if slot['index'] < len(slot['chunks']):
                        slot['index'] += 1
                    answer = ''.join(slot['chunks'][:slot['index']])
                    done = slot['index'] >= len(slot['chunks'])
                    return {'request_id': request_id, 'row_id': slot['row_id'],
                            'status': 'done' if done else 'streaming',
                            'answer': answer, 'streaming': not done or len(slot['chunks']) > 1,
                            'completed_only': len(slot['chunks']) <= 1 and done}

            def cancel(self, request_id):
                with lock:
                    slot = slots.get(request_id)
                    if slot is not None:
                        slot['status'] = 'cancelled'

            def poll_status(self):
                raise AssertionError('row clicks poll their own slot, never a global status')

        return SlotTransport()

    def test_click_is_the_only_path_to_assist_and_never_duplicates_busy(self):
        calls = []
        overlay = bridge.OverlayBridge(transport=self._slot_transport(calls), scope={'project': 'demo'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))  # busy
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': 'unknown', 'snapshot_id': overlay.snapshot_id})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['question'], 'Who approves?')
        self.assertEqual(calls[0]['scope']['project'], 'demo')
        self.assertEqual(calls[0]['row_id'], row_id)
        self.assertIn('Two reviewers.', overlay.answers[row_id]['answer'])
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertTrue(overlay.pushed)  # snapshot rendered after completion

    def test_concurrent_rows_keep_their_own_answers(self):
        import threading
        import time
        lock = threading.Lock()
        slots, seq, calls = {}, [0], []

        class SlotTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                with lock:
                    seq[0] += 1
                    request_id = f'r-{seq[0]}'
                    text = 'alpha-one' if 'alpha' in question else 'beta-two'
                    slots[request_id] = {'row_id': row_id, 'text': text, 'step': 0}
                    calls.append({'row_id': row_id, 'request_id': request_id})
                    return request_id
            def assist_request(self, request_id):
                time.sleep(0.02)
                with lock:
                    slot = slots[request_id]
                    slot['step'] += 1
                    shown = slot['text'][:4 * slot['step']]
                    done = shown == slot['text']
                    return {'request_id': request_id, 'row_id': slot['row_id'],
                            'status': 'done' if done else 'streaming', 'answer': shown,
                            'streaming': True, 'completed_only': False}
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('row clicks poll their own slot, never a global status')

        overlay = bridge.OverlayBridge(transport=SlotTransport(), poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves alpha?'},
                                      {'speaker': 'm', 'text': 'Who approves beta?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        rows = list(overlay.questions)
        self.assertEqual(len(rows), 2)
        for row_id in rows:
            self.assertTrue(overlay.on_click_line(__import__('json').dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        got = sorted(overlay.answers[row_id]['answer'] for row_id in rows)
        self.assertEqual(got, ['alpha-one', 'beta-two'])
        self.assertEqual({c['row_id'] for c in calls}, set(rows))

    def test_wrong_slot_row_id_cancels_without_misattribution(self):
        calls = []
        overlay = bridge.OverlayBridge(transport=self._slot_transport(calls), scope={})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))

        class CrossWired:
            def assist(self, question, scope, rid, expected_generation=None):
                return 'r-x'
            def assist_request(self, request_id):
                return {'request_id': request_id, 'row_id': 'some-other-row',
                        'status': 'done', 'answer': 'WRONG', 'streaming': False, 'completed_only': True}
            def cancel(self, request_id):
                calls.append({'cancelled': request_id})

        overlay.transport = CrossWired()
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertNotIn('WRONG', str(overlay.answers[row_id].get('answer')))
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertTrue(any(entry.get('cancelled') == 'r-x' for entry in calls))

    def test_assist_rejection_marks_row_idle_without_raise(self):
        class FailingTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                raise RuntimeError('down')
            def assist_request(self, request_id):
                raise AssertionError('must not poll after rejection')
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('no clicks means no slot polling')

        overlay = bridge.OverlayBridge(transport=FailingTransport())
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Ready?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertIn('failed', (overlay.message or '').lower())

    def test_scope_push_uses_current_pane_settings_and_cancels_stale_rows(self):
        calls = []
        cancellations = []
        entered, release = __import__('threading').Event(), __import__('threading').Event()
        import threading as _threading
        import time as _time

        checked_wait = self.checked_wait
        class HangingTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                calls.append(dict(scope))
                entered.set()
                checked_wait(release, 2, 'scope admission gate')
                return 'r-hang'
            def assist_request(self, request_id):
                _time.sleep(0.01)
                return {'request_id': request_id, 'row_id': 'row', 'status': 'streaming',
                        'answer': '', 'streaming': True, 'completed_only': False}
            def cancel(self, request_id):
                cancellations.append(request_id)
            def poll_status(self):
                raise AssertionError('no global polling')

        transport = HangingTransport()
        for method in ('assist', 'assist_request', 'cancel', 'poll_status'):
            setattr(transport, method, self.checked_callback(getattr(transport, method)))
        overlay = bridge.OverlayBridge(transport=transport, scope={'project': 'old'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        self.assertTrue(entered.wait(5))
        attempted, synced = _threading.Event(), _threading.Event()
        def sync():
            attempted.set()
            overlay.set_scope({'project': 'new', 'answer_provider': 'meta', 'answer_model': 'muse-spark-1.3'})
            synced.set()
        updater = _threading.Thread(target=sync, daemon=True)
        updater.start()
        try:
            self.assertTrue(attempted.wait(2))
        finally:
            # Admission holds bridge.lock. Release it before waiting for scope;
            # the old synchronous test advanced only when its gate timed out.
            release.set()
            updater.join(2)
            for worker in list(overlay.threads):
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertFalse(updater.is_alive())
        self.assertTrue(synced.is_set())
        self.assertEqual(overlay.scope['project'], 'new')
        self.assertFalse(overlay.answers.get(row_id, {}).get('busy'))
        self.assertEqual(calls[0].get('project'), 'old')
        self.assertEqual(cancellations, ['r-hang'])
        self.assertEqual(self.worker_errors, [], 'even caught callback errors must fail on the test thread')

    def test_cancel_row_stops_its_worker_and_keeps_other_rows(self):
        import threading
        import time
        started = threading.Event()

        class SlowTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                started.set()
                return 'r-slow'
            def assist_request(self, request_id):
                time.sleep(0.05)
                return {'request_id': request_id, 'row_id': 'row', 'status': 'streaming',
                        'answer': 'partial', 'streaming': True, 'completed_only': False}
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('no global polling')

        overlay = bridge.OverlayBridge(transport=SlowTransport(), poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        self.assertTrue(started.wait(5))
        self.assertTrue(overlay.cancel_row(row_id))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertFalse(overlay.cancel_row(row_id))

    def test_cancelled_ids_are_pruned_after_workers_terminate(self):
        import json as _json
        cancelled_seen = []

        class CancellingTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                return 'r-prune'
            def assist_request(self, request_id):
                cancelled_seen.append(request_id)
                return {'request_id': request_id, 'row_id': 'row',
                        'status': 'cancelled', 'cancelled': True}
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('row clicks poll their own slot')

        overlay = bridge.OverlayBridge(transport=CancellingTransport(), poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(_json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        # Worker exited on the cancelled slot: no id may be retained.
        self.assertEqual(overlay.cancelled, set())
        # Orphan ids injected by a stale revision are pruned on scope push.
        overlay.cancelled.update({'orphan-1', 'orphan-2'})
        overlay.set_scope({'project': 'new'})
        self.assertEqual(overlay.cancelled, set())
        overlay.stop()

    def test_headless_run_without_poll_capability_returns(self):
        class ClickOnly:
            def assist(self, question, scope, row_id, expected_generation=None):
                raise AssertionError('no clicks in this test')
            def assist_request(self, request_id):
                raise AssertionError('no slots in this test')
            def cancel(self, request_id):
                pass

        overlay = bridge.OverlayBridge(transport=ClickOnly(), poll_interval=0.01)
        overlay.stopping = True  # single-pass intent: run() exits without polling
        overlay.run()

    def test_broken_pipe_stops_bridge_and_bounded_queues_hold(self):
        import io
        overlay = bridge.OverlayBridge(transport=self._slot_transport([]))
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})

        class BrokenStdin(io.StringIO):
            def write(self, data):
                raise BrokenPipeError('helper exited')
        class DeadHelper:
            stdin = BrokenStdin()
            stdout = None
            def poll(self):
                return 1
        overlay.process = DeadHelper()
        self.assertFalse(overlay._emit({'type': 'snapshot', 'transcript': [], 'questions': []}))
        self.assertTrue(overlay.stopping)
        for _ in range(40):
            overlay.on_click_line('{"type":"click","id":"missing"}')
        self.assertLessEqual(overlay._click_queue.qsize(), 32)
        self.assertLessEqual(overlay._push_queue.qsize(), 8)

    def test_helper_exit_is_reaped_without_leaked_process(self):
        import subprocess
        overlay = bridge.OverlayBridge(transport=self._slot_transport([]))
        import sys as _sys
        overlay.helper_path = _sys.executable
        overlay.helper_args = ['-c', 'import sys; print("meeting-overlay/2") if "--protocol-version" in sys.argv else None']
        self.assertTrue(overlay.show_overlay()['shown'])
        deadline = __import__('time').monotonic() + 10
        while overlay.process is not None and overlay.process.poll() is None and __import__('time').monotonic() < deadline:
            __import__('time').sleep(0.01)
        overlay.stop()
        overlay.stopping = True
        self.assertIsNone(overlay.process)


class BridgeLifecycleTests(ClickTests):
    """Status pump, fail-closed show, single-writer ordering, cleanup."""

    def test_status_pump_keeps_streaming_through_pause_switch_and_late_events(self):
        import time
        seen = {'status': {'segments': [], 'listening': True}}
        transport = bridge.BackendTransport.__new__(bridge.BackendTransport)
        transport._state, transport._poll, transport._poll_context = None, None, None

        def poll():
            return dict(seen['status'])

        transport._poll = poll
        overlay = bridge.OverlayBridge(helper=None, transport=transport, poll_interval=0.02)
        with _fake_overlay_spawn():
            result = overlay.show_overlay()
        try:
            self.assertTrue(result.get('shown'), result)
            seen['status'] = {'segments': [{'speaker': 'm', 'text': 'Who decides?'}],
                              'listening': True, 'generation': 0}
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with overlay.lock:
                    questions = dict(overlay.questions)
                    segments = list(overlay.segments)
                if questions and any('Who decides?' in q for q in questions.values()):
                    break
                time.sleep(0.02)
            with overlay.lock:
                questions = dict(overlay.questions)
                segments = list(overlay.segments)
            self.assertTrue(questions, 'status pump never delivered new segments')
            self.assertTrue(any('Who decides?' in s.get('text', '') for s in segments))
        finally:
            overlay.hide_overlay()
            overlay.stop()
            self.assertIsNone(overlay.process)

    def test_headless_pump_reaches_overlay_without_scope_change(self):
        # Offline repro from the review: fake helper/transport, show-equivalent
        # pump running, mutate segments, later snapshot must contain them
        # without changing scope.
        import time
        seen = {'status': {'segments': [], 'listening': True, 'generation': 0}}
        transport = bridge.BackendTransport.__new__(bridge.BackendTransport)
        transport._state, transport._poll, transport._poll_context = None, None, None

        def poll():
            return dict(seen['status'])

        transport._poll = poll
        overlay = bridge.OverlayBridge(helper=None, transport=transport, poll_interval=0.02)
        overlay.update(dict(seen['status']))
        overlay._start_poller_locked()
        try:
            self.assertIsNone(overlay.process)
            self.assertIsNotNone(overlay._poller)
            seen['status'] = {'segments': [{'speaker': 'm', 'text': 'Who decides offline?'}],
                              'listening': True, 'generation': 1}
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with overlay.lock:
                    questions = dict(overlay.questions)
                if questions and any('offline' in q for q in questions.values()):
                    break
                time.sleep(0.02)
            with overlay.lock:
                questions = dict(overlay.questions)
                latest = overlay.pushed[-1] if overlay.pushed else {}
            self.assertTrue(questions, 'status pump never delivered segments')
            flat = __import__('json').dumps(latest)
            self.assertIn('offline', flat, 'later snapshot missing mutated segment')
        finally:
            overlay.stop()
            self.assertIsNone(overlay.process)

    def test_show_reports_unavailable_for_missing_executable(self):
        import time
        overlay = bridge.OverlayBridge(helper='/nonexistent/macos_overlay_for_test',
                                       transport=self._slot_transport([]))
        result = overlay.show_overlay()
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get('shown'))
        self.assertFalse(result.get('available', True))
        self.assertTrue(result.get('error'))
        self.assertIsNone(overlay.process)
        self.assertIsNone(overlay._poller)
        overlay.stop()
        self.assertIsNone(overlay.process)
        status = overlay.helper_status()
        self.assertFalse(status.get('available'))
        self.assertTrue(status.get('error'))

    def test_show_reports_unavailable_on_unsupported_platform(self):
        import sys as _sys
        real_platform = _sys.platform
        overlay = bridge.OverlayBridge(helper='/bin/true', transport=self._slot_transport([]))
        try:
            _sys.platform = 'win32'
            result = overlay.show_overlay()
        finally:
            _sys.platform = real_platform
        self.assertFalse(result.get('shown'))
        self.assertFalse(result.get('available', True))
        self.assertIsNone(overlay.process)
        self.assertIsNone(overlay._poller)
        overlay.stop()

    def test_paused_backend_rejects_clicks_and_meeting_switch_retires_rows(self):
        import time
        transport = self._slot_transport([])
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        # Pause: rows retire, clicks rejected, no request reserved.
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': False, 'generation': 0, 'capture_generation': 1})
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        self.assertEqual(overlay.request_by_row.get(row_id), None)
        # Resume on a new meeting revision: stale rows stay retired until the
        # new revision renders them.
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves still?'}],
                        'listening': True, 'generation': 2, 'capture_generation': 2})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        overlay.set_scope({'project': 'other'})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with overlay.lock:
                if not overlay.request_by_row and not any(
                        s.get('busy') for s in overlay.answers.values()):
                    break
            time.sleep(0.02)
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertFalse(any(s.get('busy') for s in overlay.answers.values()))
        overlay.stop()

    def test_stale_scope_reservation_cancels_new_request_without_orphan(self):
        import time
        cancels = []

        class SlowScope:
            def assist(self, question, scope, row_id, expected_generation=None):
                time.sleep(0.05)
                return 'r-stale'

            def assist_request(self, request_id):
                time.sleep(0.5)
                return {'request_id': request_id, 'status': 'streaming', 'answer': ''}

            def cancel(self, request_id):
                cancels.append(request_id)

        overlay = bridge.OverlayBridge(transport=SlowScope(), poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        time.sleep(0.02)  # let the worker pass transport.assist, then switch scope
        overlay.set_scope({'project': 'switched'})
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertEqual(cancels, ['r-stale'])
        self.assertFalse(overlay.answers.get(row_id, {}).get('busy'))
        overlay.stop()

    def test_late_answer_events_cannot_resurrect_retired_rows(self):
        import time
        late = {'fire': False}

        class LateTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                return 'r-late'

            def assist_request(self, request_id):
                if not late['fire']:
                    time.sleep(0.02)
                    return {'request_id': request_id, 'status': 'streaming',
                            'answer': '', 'streaming': True, 'completed_only': False}
                return {'request_id': request_id, 'row_id': 'gone-row', 'status': 'done',
                        'answer': 'STALE', 'streaming': False, 'completed_only': True}

            def cancel(self, request_id):
                pass

        overlay = bridge.OverlayBridge(transport=LateTransport(), poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        overlay.set_scope({'project': 'next-meeting'})
        late['fire'] = True
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertNotIn('STALE', __import__('json').dumps(overlay.answers))
        self.assertFalse(any(s.get('busy') for s in overlay.answers.values()))
        overlay.stop()

    def test_contextvars_propagate_from_pane_to_answer_worker_without_leaks(self):
        import contextvars
        sentinel = contextvars.ContextVar('meeting-test-auth2')
        token = sentinel.set('pane-secret-value')
        try:
            seen = {}

            class LeakCheckState:
                overlay_scope_value = {}

                def assist(self, *args, **kwargs):
                    seen['ctx'] = sentinel.get()
                    seen['kwargs'] = dict(kwargs)
                    return {'request_id': 'r-ctx'}

                def status(self):
                    return {'segments': [], 'listening': True}

            pane_token = 'pane-action-token'
            bridge._PANE_ACTION.set(pane_token)
            transport = bridge.BackendTransport(LeakCheckState(), pane_token=pane_token)
            self.assertNotIn('pane-secret-value', repr(vars(transport)))
            overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.01)
            overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                            'listening': True, 'generation': 0, 'capture_generation': 0})
            row_id = next(iter(overlay.questions))
            self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
            for thread in list(overlay.threads):
                thread.join(timeout=10)
            self.assertEqual(seen.get('ctx'), 'pane-secret-value')
            self.assertEqual(seen.get('kwargs', {}).get('row_id'), row_id)
            self.assertNotIn('pane-secret-value', repr(vars(overlay)))
            # Impossible environments (no copy_context) fail closed, documented.
            overlay.stop()
        finally:
            sentinel.reset(token)

    def _slot_transport(self, *args, **kwargs):
        return super()._slot_transport(*args, **kwargs)

    def test_fake_provider_streams_distinct_chunks_in_order(self):
        import threading
        import time
        lock = threading.Lock()
        slots, seq = {}, [0]

        class FakeStreamingTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                with lock:
                    seq[0] += 1
                    request_id = f'r-{seq[0]}'
                    slots[request_id] = {'row_id': row_id, 'step': 0,
                                         'chunks': ['Two ', 'reviewers ', 'must approve.']}
                    return request_id
            def assist_request(self, request_id):
                time.sleep(0.01)
                with lock:
                    slot = slots[request_id]
                    slot['step'] += 1
                    shown = ''.join(slot['chunks'][:slot['step']])
                    done = shown == ''.join(slot['chunks'])
                    return {'request_id': request_id, 'row_id': slot['row_id'],
                            'status': 'done' if done else 'streaming', 'answer': shown,
                            'streaming': True, 'completed_only': False}
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('streaming rows poll their own slot')

        overlay = bridge.OverlayBridge(transport=FakeStreamingTransport(), poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        seen_streaming = []

        original_push = overlay.push
        def spy_push():
            state = overlay.answers.get(row_id, {})
            if state.get('streaming') or (state.get('answer') and state.get('busy')):
                seen_streaming.append(state.get('answer'))
            return original_push()
        overlay.push = spy_push
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        overlay.push = original_push
        self.assertEqual(overlay.answers[row_id]['answer'], 'Two reviewers must approve.')
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertFalse(overlay.answers[row_id]['completed_only'])
        # Incremental snapshots grew monotonically with distinct prefixes.
        prefixes = [text for text in seen_streaming if text]
        self.assertTrue(len(prefixes) >= 2)
        for earlier, later in zip(prefixes, prefixes[1:]):
            self.assertTrue(later.startswith(earlier) and len(later) > len(earlier))

    def test_completed_only_provider_reports_honestly_without_chunked_fakes(self):
        real_row = [None]

        class CompletedOnly:
            def assist(self, question, scope, row_id, expected_generation=None):
                real_row[0] = row_id
                return 'r-1'
            def assist_request(self, request_id):
                return {'request_id': request_id, 'row_id': real_row[0], 'status': 'done',
                        'answer': 'Final answer.', 'streaming': False, 'completed_only': True}
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('no global polling')

        overlay = bridge.OverlayBridge(transport=CompletedOnly(), poll_interval=0.01)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertEqual(overlay.answers[row_id]['answer'], 'Final answer.')
        self.assertTrue(overlay.answers[row_id]['completed_only'])

    def test_end_to_end_mocked_backend_pane_native_protocol(self):
        """Clicks reach the real request-specific row with current pane scope.

        Uses the real MeetingState + BackendTransport (not an HTTP fake): the
        pane scope flows through /overlay/show, the click creates a request
        slot, and streamed chunks land on the clicked row only.

        Portable: the helper spawn is faked with a slot process (no real
        subprocess), so the full pane/show/click/status lifecycle runs on
        every platform, including Windows CI.
        """
        import time
        import server as real_server
        deltas = ['Two ', 'reviewers.']

        class FakeEngine:
            def answer_stream(self, question, **kwargs):
                captured.append({'question': question, 'kwargs': kwargs})
                header = {'model_route': 'fake/test', 'sources': [], 'warnings': [],
                          'web_search': {'used': False}}
                return header, iter(list(deltas))
            def answer(self, question, **kwargs):
                raise AssertionError('streaming engine must use answer_stream')

        captured = []
        state = real_server.MeetingState(engine_factory=lambda: FakeEngine())
        state.add_segment({'speaker': 'remote', 'text': 'Who approves?'})
        state.listening = True

        shown = None
        real_helper_path = state._overlay_helper_path
        state._overlay_helper_path = lambda: '/fake/macos_overlay_for_test'
        try:
            with _fake_overlay_spawn():
                shown = state.overlay_show({'project': 'demo', 'answer_provider': 'meta',
                                            'answer_model': 'muse-spark-1.3',
                                            'web_fallback': True})
        finally:
            state._overlay_helper_path = real_helper_path
        self.assertTrue(shown['shown'])
        transport = bridge.BackendTransport(state)
        overlay = state.overlay
        self.assertIsNotNone(overlay)
        assert overlay is not None
        overlay.update(state.status())
        row_id = next(iter(overlay.questions))
        current_scope = {'project': 'demo', 'answer_provider': 'meta',
                         'answer_model': 'muse-spark-1.3', 'web_fallback': True}
        state.overlay_scope(dict(current_scope, owner=state.overlay_owner))
        row_deadline = time.monotonic() + 10
        clicked = False
        while time.monotonic() < row_deadline:
            try:
                clicked = overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id}))
            except AssertionError:
                raise
            except Exception:
                clicked = False
            if clicked:
                break
            time.sleep(0.05)
        self.assertTrue(clicked)
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertEqual(overlay.answers[row_id]['answer'], 'Two reviewers.')
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertEqual(captured[0]['question'], 'Who approves?')
        self.assertEqual(captured[0]['kwargs'].get('project'), 'demo')
        self.assertEqual(captured[0]['kwargs'].get('answer_provider'), 'meta')
        self.assertEqual(captured[0]['kwargs'].get('answer_model'), 'muse-spark-1.3')
        state.overlay_hide()
        self.assertIsNone(state.overlay)

    def test_first_partial_row_answer_visible_before_provider_finishes(self):
        """End-to-end barrier: the row shows its first partial before release.

        Uses the real MeetingState + BackendTransport with a gated fake
        provider stream (no models, no network): the engine's first chunk
        arrives and the row answer becomes visible while the provider is
        still blocked on an event; only then is the provider released and
        the stream completes. Cancelling never revives a stale row.
        """
        import threading
        import time
        import server as real_server
        release = threading.Event()

        class FakeGatedEngine:
            def answer_stream(self, question, **kwargs):
                header = {'model_route': 'fake/test', 'sources': [], 'warnings': [],
                          'web_search': {'used': False}}
                def gated():
                    yield 'Two '
                    release.wait(10)  # stay unfinished until row proves arrival
                    yield 'reviewers.'
                return header, gated()
            def answer(self, question, **kwargs):
                raise AssertionError('streaming engine must use answer_stream')

        state = real_server.MeetingState(engine_factory=lambda: FakeGatedEngine())
        state.add_segment({'speaker': 'remote', 'text': 'Who approves?'})
        state.listening = True
        real_helper_path = state._overlay_helper_path
        state._overlay_helper_path = lambda: '/fake/macos_overlay_for_test'
        try:
            with _fake_overlay_spawn():
                shown = state.overlay_show({'project': 'demo'})
        finally:
            state._overlay_helper_path = real_helper_path
        self.assertTrue(shown['shown'])
        transport = bridge.BackendTransport(state)
        overlay = state.overlay
        self.assertIsNotNone(overlay)
        assert overlay is not None
        overlay.update(state.status())
        row_id = next(iter(overlay.questions))
        state.overlay_scope({'owner': state.overlay_owner, 'project': 'demo'})
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        # First partial row answer must be visible before release.
        visible = ''
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with state.lock:
                with overlay.lock:
                    visible = overlay.answers.get(row_id, {}).get('answer', '')
            if visible == 'Two ':
                break
            time.sleep(0.01)
        self.assertEqual(visible, 'Two ')
        self.assertFalse(release.is_set(), 'provider must still be gated')
        release.set()
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        with overlay.lock:
            final = overlay.answers[row_id]['answer']
            completed = overlay.answers[row_id]['completed_only']
        self.assertEqual(final, 'Two reviewers.')
        self.assertFalse(completed)
        state.overlay_hide()
        self.assertIsNone(state.overlay)

    def test_timeout_cancels_slot_and_reports_retry(self):
        import time

        class Never:
            def assist(self, question, scope, row_id, expected_generation=None):
                return 'r-1'
            def assist_request(self, request_id):
                time.sleep(0.02)
                return {'request_id': request_id, 'row_id': real_row[0], 'status': 'streaming',
                        'answer': '', 'streaming': True, 'completed_only': False}
            def cancel(self, request_id):
                cancelled.append(request_id)
            def poll_status(self):
                raise AssertionError('no global polling')

        cancelled = []
        real_row = [None]
        overlay = bridge.OverlayBridge(transport=Never(), poll_interval=0.01, assist_timeout=0.05)
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        real_row[0] = row_id
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertEqual(cancelled, ['r-1'])
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertIn('timed out', (overlay.message or '').lower())
        row_warnings = overlay.answers[row_id].get('warnings') or []
        self.assertTrue(any('partial' in w.lower() for w in row_warnings),
                        'timeout must persist a row-level partial warning, not just a transient message')


class GenerationIsolationTests(CheckedThreads):
    def test_late_mismatched_row_cannot_erase_completed_new_answer(self):
        import threading
        from unittest.mock import Mock
        entered = threading.Event()
        release = threading.Event()
        transport = Mock()
        transport.assist.side_effect = ['old', 'new']
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.001)
        status = {'segments': [{'text': 'Who approves?'}], 'listening': True, 'generation': 1}
        overlay.update(status)
        row_id = next(iter(overlay.questions))
        click = json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})

        def poll(request_id):
            if request_id == 'old':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('late-response gate was not released')
                return {'row_id': 'different-row', 'status': 'done', 'answer': 'Old answer'}
            return {'row_id': row_id, 'status': 'done', 'answer': 'New answer',
                    'warnings': ['New warning'], 'sources': [{'title': 'New source'}]}

        transport.assist_request.side_effect = self.checked_callback(poll)
        workers = []
        try:
            self.assertTrue(overlay.on_click_line(json.dumps(dict(json.loads(click), snapshot_id=overlay.snapshot_id))))
            workers.extend(overlay.threads)
            self.assertTrue(entered.wait(5))
            overlay.update(dict(status, generation=2))
            self.assertTrue(overlay.on_click_line(json.dumps(dict(json.loads(click), snapshot_id=overlay.snapshot_id))))
            workers.extend(t for t in overlay.threads if t not in workers)
            workers[1].join(5)
            self.assertFalse(workers[1].is_alive())
            self.assertNotIn(row_id, overlay.request_by_row)
            expected = dict(overlay.answers[row_id])
            expected_message = overlay.message
            self.assertEqual(expected['answer'], 'New answer')
            release.set()
            workers[0].join(5)
            self.assertFalse(workers[0].is_alive())
            self.assertEqual(overlay.answers[row_id], expected)
            self.assertEqual(overlay.message, expected_message)
            self.assertEqual(overlay.pushed[-1]['questions'][0]['answer'], 'New answer')
            self.assertEqual(overlay.row_by_request, {})
        finally:
            release.set()
            for worker in workers:
                worker.join(5)

    def test_resume_retires_pending_click_and_allows_retry_without_old_worker_reset(self):
        import threading
        from unittest.mock import Mock, patch
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        transport = Mock()
        transport.assist.return_value = 'retry'
        overlay = bridge.OverlayBridge(transport=transport)
        status = {'segments': [{'text': 'Who approves?'}], 'listening': True, 'generation': 1}
        overlay.update(status)
        row_id = next(iter(overlay.questions))
        transport.assist_request.return_value = {'row_id': row_id, 'status': 'done', 'answer': 'Retry answer'}
        click = json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})
        real_ask = overlay.ask
        old_generation = overlay.paused_generation

        def gated_ask(row_id, question, click_generation):
            index = 0 if click_generation == old_generation else 1
            entered[index].set()
            if not release[index].wait(5):
                raise AssertionError('click gate was not released')
            return real_ask(row_id, question, click_generation)

        workers = []
        try:
            with patch.object(overlay, 'ask', side_effect=gated_ask):
                self.assertTrue(overlay.on_click_line(json.dumps(dict(json.loads(click), snapshot_id=overlay.snapshot_id))))
                workers.extend(overlay.threads)
                self.assertTrue(entered[0].wait(5))
                self.assertTrue(overlay.answers[row_id]['busy'])
                overlay.update(dict(status, listening=False))
                overlay.update(status)  # Resume the SAME backend generation.
                self.assertNotIn(row_id, overlay.request_by_row)
                self.assertFalse(overlay.answers[row_id]['busy'])
                self.assertTrue(overlay.on_click_line(json.dumps(dict(json.loads(click), snapshot_id=overlay.snapshot_id))))
                workers.extend(t for t in overlay.threads if t not in workers)
                self.assertTrue(entered[1].wait(5))
                release[0].set()
                workers[0].join(5)
                self.assertFalse(workers[0].is_alive())
                transport.assist.assert_not_called()
                self.assertIs(overlay.request_by_row[row_id], True)
                self.assertTrue(overlay.answers[row_id]['busy'])
                release[1].set()
                workers[1].join(5)
                self.assertFalse(workers[1].is_alive())
                self.assertEqual(overlay.answers[row_id]['answer'], 'Retry answer')
                self.assertFalse(overlay.answers[row_id]['busy'])
                self.assertNotIn(row_id, overlay.request_by_row)
                transport.assist.assert_called_once()
        finally:
            for event in release:
                event.set()
            for worker in workers:
                worker.join(5)

    def test_completed_answer_is_cleared_for_identical_question_in_new_generation(self):
        class Transport:
            def assist(self, question, scope, row_id, expected_generation=None):
                self.row_id = row_id
                return 'request'

            def assist_request(self, request_id):
                return {'row_id': self.row_id, 'status': 'done', 'answer': 'Old answer',
                        'warnings': ['Old warning'], 'sources': [{'title': 'Old source'}]}

            def cancel(self, request_id):
                pass

        overlay = bridge.OverlayBridge(transport=Transport())
        status = {'segments': [{'text': 'Who approves?'}], 'listening': True, 'generation': 1}
        overlay.update(status)
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(overlay.answers[row_id]['answer'], 'Old answer')
        self.assertNotIn(row_id, overlay.request_by_row)
        overlay.update(dict(status, generation=2))
        row = overlay.pushed[-1]['questions'][0]
        self.assertEqual(row['id'], row_id)
        self.assertIsNone(row['answer'])
        self.assertFalse(row['busy'])
        self.assertEqual(row['warnings'], [])
        self.assertEqual(row['sources'], [])

    def test_running_partial_is_cancelled_and_late_result_cannot_overwrite_new_answer(self):
        import threading
        import time
        waiting = threading.Event()
        late_poll_entered = threading.Event()
        release = threading.Event()
        cancelled = []
        checked_wait = self.checked_wait

        class Transport:
            def __init__(self):
                self.count = 0
                self.row_id = None

            def assist(self, question, scope, row_id, expected_generation=None):
                self.count += 1
                self.row_id = row_id
                return f'request-{self.count}'

            def assist_request(self, request_id):
                if request_id == 'request-1':
                    if not waiting.is_set():
                        waiting.set()
                        return {'row_id': self.row_id, 'status': 'streaming',
                                'answer': 'Old partial', 'streaming': True}
                    late_poll_entered.set()
                    checked_wait(release, 5, 'late poll gate')
                    return {'row_id': self.row_id, 'status': 'done', 'answer': 'Old late'}
                return {'row_id': self.row_id, 'status': 'done', 'answer': 'New answer'}

            def cancel(self, request_id):
                cancelled.append(request_id)

        overlay = bridge.OverlayBridge(transport=Transport(), poll_interval=0.001)
        status = {'segments': [{'text': 'Who approves?'}], 'listening': True, 'generation': 1}
        overlay.update(status)
        row_id = next(iter(overlay.questions))
        try:
            self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with overlay.lock:
                    if overlay.answers[row_id]['answer'] == 'Old partial':
                        break
                time.sleep(0.001)
            self.assertEqual(overlay.answers[row_id]['answer'], 'Old partial')
            self.assertTrue(late_poll_entered.wait(5))
            overlay.update(dict(status, generation=2))
            row = overlay.pushed[-1]['questions'][0]
            self.assertIsNone(row['answer'])
            self.assertFalse(row['busy'])
            self.assertIn('request-1', cancelled)
            self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with overlay.lock:
                    if overlay.answers[row_id]['answer'] == 'New answer':
                        break
                time.sleep(0.001)
            self.assertEqual(overlay.answers[row_id]['answer'], 'New answer')
            release.set()
            for thread in list(overlay.threads):
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(overlay.answers[row_id]['answer'], 'New answer')
        finally:
            release.set()
            for thread in list(overlay.threads):
                thread.join(timeout=5)

    def test_stale_queued_click_cannot_clear_new_completed_answer(self):
        from unittest.mock import Mock
        transport = Mock()
        overlay = bridge.OverlayBridge(transport=transport)
        status = {'segments': [{'text': 'Who approves?'}], 'listening': True, 'generation': 1}
        overlay.update(status)
        row_id = next(iter(overlay.questions))
        old_generation = overlay.paused_generation
        overlay.update(dict(status, generation=2))
        overlay.answers[row_id] = {'answer': 'New answer', 'busy': False,
                                   'streaming': False, 'completed_only': True}
        overlay.ask(row_id, 'Who approves?', click_generation=old_generation)
        transport.assist.assert_not_called()
        self.assertEqual(overlay.answers[row_id]['answer'], 'New answer')

    def test_old_queued_click_cannot_claim_new_generation_reservation(self):
        from unittest.mock import Mock
        transport = Mock()
        overlay = bridge.OverlayBridge(transport=transport)
        status = {'segments': [{'text': 'Who approves?'}], 'listening': True, 'generation': 1}
        overlay.update(status)
        row_id = next(iter(overlay.questions))
        old_generation = overlay.paused_generation
        overlay.request_by_row[row_id] = True
        overlay.reserved_generation[row_id] = old_generation
        overlay.update(dict(status, generation=2))
        overlay.request_by_row[row_id] = True
        overlay.reserved_generation[row_id] = overlay.paused_generation
        overlay.answers[row_id]['busy'] = True
        overlay.ask(row_id, 'Who approves?', click_generation=old_generation)
        transport.assist.assert_not_called()
        self.assertIs(overlay.request_by_row[row_id], True)
        self.assertTrue(overlay.answers[row_id]['busy'])


class BackendGenerationAdmissionTests(CheckedThreads):
    def test_stale_snapshot_click_is_rejected_before_next_overlay_poll(self):
        self._exercise_admission('before_click')

    def test_generation_is_checked_inside_backend_admission_lock(self):
        self._exercise_admission('at_lock')

    def test_matching_generation_is_admitted_and_completed(self):
        self._exercise_admission(None)

    def _exercise_admission(self, change_when):
        # Test the backend boundary directly: a bridge tap holds overlay_lock,
        # so waiting for Clear in that tap's admission gate deadlocks the test.
        from fastapi import HTTPException
        from unittest.mock import Mock
        import server
        import threading
        import time
        entered, release = threading.Event(), threading.Event()
        state = server.MeetingState(engine_factory=lambda: engine)
        engine = Mock()
        engine.supports_streaming.return_value = False
        engine.answer.return_value = {'answer': 'Current answer'}
        state.listening = True
        state.add_segment({'id': 'row', 'speaker': 'remote', 'text': 'Who approves?'})
        generation, capture = state.generation, state.capture_generation
        lock = state.lock
        caller = None
        errors, results = [], []
        class Gate:
            def __enter__(self):
                if threading.current_thread() is caller and not entered.is_set():
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('admission gate timed out')
                return lock.__enter__()
            def __exit__(self, *args):
                return lock.__exit__(*args)
        state.lock = Gate()
        clear = next(r.endpoint for r in server.make_router(state).routes if r.path == '/clear')
        def submit():
            try:
                results.append(state.assist('Who approves?', row_id='row',
                                            expected_generation=generation,
                                            expected_capture_generation=capture))
            except BaseException as exc:
                errors.append(exc)
        if change_when == 'before_click':
            clear()
        caller = threading.Thread(target=submit, daemon=True)
        caller.start()
        try:
            self.assertTrue(entered.wait(2))
            if change_when == 'at_lock':
                clear()
        finally:
            release.set()
            caller.join(2)
        self.assertFalse(caller.is_alive())
        if change_when:
            self.assertEqual(results, [])
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], HTTPException)
            self.assertEqual(errors[0].status_code, 409)
            self.assertEqual(state.requests, {})
            self.assertEqual(state.request_seq, 0)
            engine.answer.assert_not_called()
        else:
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            deadline = time.monotonic() + 2
            while state.live_workers and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(state.live_workers, 0)
            self.assertEqual(state.assist_request(results[0]['request_id'])['answer'], 'Current answer')
            engine.answer.assert_called_once()


class SnapshotTruthTests(CheckedThreads):
    def test_snapshot_carries_listening_and_share_warning(self):
        status = {'segments': [], 'listening': True}
        snapshot = bridge.build_snapshot(status)
        self.assertTrue(snapshot['listening'])
        self.assertIn('screen share', str(snapshot.get('share_warning', '')).lower())
        paused = bridge.build_snapshot({'segments': [], 'listening': False})
        self.assertFalse(paused['listening'])

    def test_oversize_snapshot_truncates_with_notice(self):
        import json as _json
        snap = {'type': 'snapshot', 'transcript': [{'id': 'l1', 'speaker': 's', 'text': 'hi'}],
                'questions': [{'id': 'r1', 'question': 'Q?', 'answer': 'A' * 5000, 'busy': False,
                               'streaming': False, 'completed_only': True}],
                'busy': False, 'listening': True}
        b = bridge.OverlayBridge(transport=None)
        b._SNAPSHOT_MAX_BYTES = 4000  # type: ignore[attr-defined]
        self.assertTrue(b._emit(snap))
        emitted = _json.loads(b.pushed[-1] if isinstance(b.pushed[-1], str) else _json.dumps(b.pushed[-1]))
        # pushed stores dict snapshots (headless record)
        stored = b.pushed[-1]
        stored = stored if isinstance(stored, dict) else _json.loads(stored)
        self.assertEqual(stored.get('transcript'), [])
        self.assertTrue(stored.get('truncated'))
        self.assertIn('shortened', str(stored.get('message', '')).lower())

    def test_identical_snapshots_suppressed(self):
        status = {'segments': [], 'listening': True}
        b = bridge.OverlayBridge(transport=None)
        snap = bridge.build_snapshot(status)
        self.assertTrue(b._emit(dict(snap)))
        n = len(b.pushed)
        self.assertTrue(b._emit(dict(snap)))
        self.assertEqual(len(b.pushed), n)

    def test_helper_hotkey_error_surfaces_message(self):
        import io, json as _json
        from unittest.mock import Mock
        b = bridge.OverlayBridge(transport=None)
        proc = Mock()
        proc.stdout = iter([_json.dumps({'type': 'error', 'code': 'hotkey'}) + '\n'])
        proc.stdin = None
        b.process = proc
        b._read_clicks()
        self.assertIn('shortcut', (b.message or '').lower())


class ScopeOrderingTests(CheckedThreads):
    def test_older_scope_sequence_is_ignored(self):
        b = bridge.OverlayBridge(transport=None)
        b.set_scope({'project': 'beta', 'seq': 5})
        self.assertEqual(b.scope.get('project'), 'beta')
        result = b.set_scope({'project': 'alpha', 'seq': 3})
        self.assertTrue(result.get('ignored_stale'))
        self.assertEqual(b.scope.get('project'), 'beta')
        b.set_scope({'project': 'gamma', 'seq': 6})
        self.assertEqual(b.scope.get('project'), 'gamma')

    def test_snapshot_carries_warnings_and_sources(self):
        status = {'segments': [{'speaker': 's', 'text': 'When is the review?'}], 'listening': True}
        # Build from a real suggested row instead of a fabricated id.
        rows = bridge.suggest_questions(status['segments'])
        self.assertTrue(rows)
        row_id = rows[0]['id']
        snap = bridge.build_snapshot(status, answers={row_id: {'answer': 'Friday [1]', 'busy': False,
            'streaming': False, 'completed_only': False,
            'warnings': ['Stream interrupted; showing a partial answer.'],
            'source_titles': ['01_quote.txt']}})
        rendered = [q for q in snap['questions'] if q['id'] == row_id][0]
        self.assertEqual(rendered['warnings'], ['Stream interrupted; showing a partial answer.'])
        self.assertEqual(rendered['sources'], ['01_quote.txt'])


class WarningPriorityTests(CheckedThreads):
    def test_interruption_warning_survives_cap(self):
        warnings = ['Selected snippets may be sent.', 'All-project scope searches up to 100 folders.',
                    'Single-project scope still includes prior sessions.',
                    'Stream interrupted; showing a partial answer.']
        prioritized = bridge._prioritize_warnings(warnings)
        self.assertIn('Stream interrupted; showing a partial answer.', prioritized)
        self.assertLessEqual(len(prioritized), 3)

    def test_overflow_is_disclosed_not_dropped(self):
        prioritized = bridge._prioritize_warnings(['a', 'b', 'c', 'd', 'e'])
        self.assertTrue(any('more warning' in w for w in prioritized))


class ConcurrentAssistTests(CheckedThreads):
    def test_overlapping_assists_do_not_share_context(self):
        import threading
        import time
        import contextvars
        from unittest.mock import Mock
        gate = threading.Event()
        entered = []
        lock = threading.Lock()
        def fake_assist(*args, **kwargs):
            time.sleep(0.05)
            row_id = kwargs.get('row_id', args[-1] if args else None)
            with lock:
                entered.append(row_id)
            return {'accepted': True, 'request_id': 'req-' + str(row_id)}
        fake_state = Mock()
        fake_state.overlay_scope_value = {}
        fake_state.assist.side_effect = fake_assist
        pane_token = 'pane-action-token'
        bridge._PANE_ACTION.set(pane_token)
        transport = bridge.BackendTransport(fake_state, pane_token=pane_token)
        errors = []
        def call(row):
            try:
                transport.assist('q', {}, row)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=call, args=(f'row-{i}',)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(entered), ['row-0', 'row-1', 'row-2', 'row-3'])

    def test_stale_click_does_not_submit(self):
        from unittest.mock import Mock
        transport = Mock()
        overlay = bridge.OverlayBridge(transport=transport)
        overlay.update({'segments': [{'speaker': 's', 'text': 'When is the review?'}], 'listening': True})
        row_id = next(iter(overlay.questions))
        overlay.request_by_row[row_id] = True
        overlay.answers[row_id] = {'answer': None, 'busy': False, 'streaming': False, 'completed_only': True}
        # Simulate a pause landing between the click and the worker run.
        overlay.paused_generation += 1
        overlay.ask(row_id, 'When is the review?', click_generation=0)
        transport.assist.assert_not_called()
        self.assertIn('tap again', (overlay.message or '').lower())


class OrderedDeliveryTests(CheckedThreads):
    def test_stale_sequence_never_reaches_helper(self):
        b = bridge.OverlayBridge(transport=None)
        with b.lock:
            b._latest_seq = 5
        stale = {'type': 'snapshot', 'transcript': [], 'questions': []}
        self.assertTrue(b._emit(dict(stale), seq=3))
        self.assertEqual(b.pushed, [])
        fresh = {'type': 'snapshot', 'transcript': [], 'questions': []}
        self.assertTrue(b._emit(dict(fresh), seq=5))
        self.assertEqual(len(b.pushed), 1)

    def test_reordered_concurrent_pushes_keep_newest(self):
        import threading
        b = bridge.OverlayBridge(transport=None)
        threads = [threading.Thread(target=b.push) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        # A late duplicate of the very first snapshot must not overwrite the newest.
        before = len(b.pushed)
        b._emit({'type': 'snapshot', 'transcript': [], 'questions': []}, seq=1)
        self.assertEqual(len(b.pushed), before)


class PushAtomicityTests(CheckedThreads):
    def test_concurrent_pushes_serialize_with_exact_sequences(self):
        import threading
        b = bridge.OverlayBridge(transport=None)
        threads = [threading.Thread(target=b.push) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(b._seq, 16)
        self.assertTrue(b.pushed)
        # An older in-flight capture completing late can never overwrite the newest.
        before = len(b.pushed)
        b._emit({'type': 'snapshot', 'transcript': [], 'questions': []}, seq=1)
        self.assertEqual(len(b.pushed), before)
        self.assertEqual(b._latest_seq, 16)


class StaleWriteRaceTests(CheckedThreads):
    def test_older_write_loses_after_newer_observed(self):
        # An older emission validated before a newer push must not overwrite
        # the newer snapshot: validation and delivery are atomic.
        b = bridge.OverlayBridge(transport=None)
        with b.lock:
            b._latest_seq = 2
        new = {'type': 'snapshot', 'transcript': [], 'questions': [], 'marker': 'new'}
        old = {'type': 'snapshot', 'transcript': [], 'questions': [], 'marker': 'old'}
        self.assertTrue(b._emit(dict(new), seq=2))
        before = len(b.pushed)
        self.assertTrue(b._emit(dict(old), seq=1))
        self.assertEqual(len(b.pushed), before)
        self.assertEqual(b.pushed[-1].get('marker'), 'new')

    def test_concurrent_emissions_deliver_newest_only(self):
        import threading
        b = bridge.OverlayBridge(transport=None)
        with b.lock:
            b._latest_seq = 32
        errors = []
        def send(i):
            try:
                b._emit({'type': 'snapshot', 'transcript': [], 'questions': [],
                         'marker': f'seq-{i}'}, seq=i)
            except Exception as exc:  # pragma: no cover - must never happen
                errors.append(exc)
        threads = [threading.Thread(target=send, args=(i,)) for i in range(1, 33)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(b.pushed), 1)
        self.assertEqual(b.pushed[0].get('marker'), 'seq-32')


class TakeoverTests(CheckedThreads):
    def test_late_worker_never_writes_into_takeover_row(self):
        import threading
        import time
        import overlay_bridge as bridge_mod
        release_old = threading.Event()
        lock = threading.Lock()
        slots, seq = {}, [0]

        class GateTransport:
            def assist(self, question, scope, row_id, expected_generation=None):
                with lock:
                    seq[0] += 1
                    request_id = f'r-{seq[0]}'
                    slots[request_id] = {'row_id': row_id,
                                         'text': 'OLD-ANSWER' if 'first' in question else 'NEW-ANSWER'}
                    return request_id

            def assist_request(self, request_id):
                if request_id == 'r-1':
                    # Hold the old worker past its ownership pre-check so the
                    # scope retirement + re-ask below interleave mid-poll.
                    release_old.wait(10)
                    with lock:
                        slot = slots[request_id]
                    return {'request_id': request_id, 'row_id': slot['row_id'],
                            'status': 'done', 'answer': slot['text'],
                            'streaming': False, 'completed_only': False,
                            'warnings': [], 'sources': []}
                with lock:
                    slot = slots[request_id]
                return {'request_id': request_id, 'row_id': slot['row_id'],
                        'status': 'done', 'answer': slot['text'],
                        'streaming': False, 'completed_only': False,
                        'warnings': [], 'sources': []}

            def cancel(self, request_id):
                pass

            def poll_status(self):
                raise AssertionError('unused')

        overlay = bridge_mod.OverlayBridge(transport=GateTransport(), scope={'project': 'demo'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'first?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(__import__('json').dumps({'type': 'click', 'id': row_id, 'snapshot_id': overlay.snapshot_id})))
        # Let the old worker pass its pre-poll ownership check, then retire
        # the scope and re-ask the same row before it writes.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with overlay.lock:
                owned = overlay.request_by_row.get(row_id) == 'r-1'
            if owned:
                break
            time.sleep(0.01)
        time.sleep(0.2)  # old worker is now blocked inside assist_request
        overlay.set_scope({'project': 'demo2'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'second?'}],
                        'listening': True, 'generation': 1, 'capture_generation': 0})
        row_id2 = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(__import__('json').dumps({'type': 'click', 'id': row_id2, 'snapshot_id': overlay.snapshot_id})))
        release_old.set()
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        final = overlay.answers.get(row_id2, {})
        self.assertIn('NEW-ANSWER', final.get('answer') or '')
        self.assertNotIn('OLD-ANSWER', final.get('answer') or '')
        # The old worker's exit must not have resurrected its own stale
        # ownership after the takeover completed.
        with overlay.lock:
            self.assertNotIn('r-1', overlay.row_by_request)


class PaneTokenGateTests(CheckedThreads):
    def test_off_path_transport_construction_fails_closed(self):
        import contextvars
        from unittest.mock import Mock
        state = Mock()
        state.overlay_scope_value = {}
        # No pane token minted (constructed off the pane action thread):
        # an empty copied context must not authorize provider work.
        transport = bridge.BackendTransport(state)
        with self.assertRaisesRegex(RuntimeError, 'not from the pane action'):
            transport.assist('q', {}, 'row-1')
        state.assist.assert_not_called()

    def test_refresh_rebinds_to_the_latest_pane_action(self):
        import contextvars
        from unittest.mock import Mock
        state = Mock()
        state.overlay_scope_value = {}
        state.assist.return_value = {'request_id': 'r-1'}
        bridge._PANE_ACTION.set('first-token')
        transport = bridge.BackendTransport(state, pane_token='first-token')
        bridge._PANE_ACTION.set('second-token')
        transport.refresh_pane_token('second-token')
        self.assertEqual(transport.assist('q', {}, 'row-1'), 'r-1')
        state.assist.assert_called_once()


class StaleAskTests(CheckedThreads):
    def test_stale_tap_never_evicts_newer_row_reservation(self):
        from unittest.mock import Mock
        state = Mock()
        state.overlay_scope_value = {}
        transport = Mock()
        transport.assist.return_value = 'r-ignored'
        overlay = bridge.OverlayBridge(transport=transport, scope={'project': 'demo'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        # A newer tap owns the row and is streaming an answer.
        overlay.request_by_row[row_id] = 'r-new'
        overlay.row_by_request['r-new'] = row_id
        overlay.answers[row_id] = {'answer': 'partial new', 'busy': True,
                                   'streaming': True, 'completed_only': False}
        stale_generation = overlay.paused_generation
        overlay.paused_generation += 1  # context moved on after the stale tap
        overlay.ask(row_id, 'stale question', click_generation=stale_generation)
        # The newer reservation, ownership, and partial answer survive.
        self.assertEqual(overlay.request_by_row.get(row_id), 'r-new')
        self.assertEqual(overlay.row_by_request.get('r-new'), row_id)
        self.assertEqual(overlay.answers[row_id]['answer'], 'partial new')
        self.assertTrue(overlay.answers[row_id]['busy'])
        transport.assist.assert_not_called()


class StaleAskReservationTests(CheckedThreads):
    def test_stale_tap_never_evicts_newer_pending_reservation(self):
        from unittest.mock import Mock
        state = Mock()
        state.overlay_scope_value = {}
        transport = Mock()
        transport.assist.return_value = 'r-ignored'
        overlay = bridge.OverlayBridge(transport=transport, scope={'project': 'demo'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        # A newer tap reserved the row but its worker has not sent yet.
        overlay.paused_generation += 1
        newer_generation = overlay.paused_generation
        overlay.request_by_row[row_id] = True
        overlay.reserved_generation[row_id] = newer_generation
        overlay.answers[row_id] = {'answer': None, 'busy': True,
                                   'streaming': False, 'completed_only': True}
        overlay.ask(row_id, 'stale question', click_generation=newer_generation - 1)
        self.assertTrue(overlay.request_by_row.get(row_id) is True)
        self.assertEqual(overlay.reserved_generation.get(row_id), newer_generation)
        self.assertTrue(overlay.answers[row_id]['busy'])
        transport.assist.assert_not_called()

    def test_own_stale_reservation_still_releases_for_retry(self):
        from unittest.mock import Mock
        state = Mock()
        state.overlay_scope_value = {}
        transport = Mock()
        transport.assist.return_value = 'r-ignored'
        overlay = bridge.OverlayBridge(transport=transport, scope={'project': 'demo'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        stale_generation = overlay.paused_generation
        overlay.request_by_row[row_id] = True
        overlay.reserved_generation[row_id] = stale_generation
        overlay.answers[row_id] = {'answer': None, 'busy': True,
                                   'streaming': False, 'completed_only': True}
        overlay.paused_generation += 1  # context moved on after our own tap
        overlay.ask(row_id, 'own stale question', click_generation=stale_generation)
        self.assertNotIn(row_id, overlay.request_by_row)
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertIn('tap again', overlay.message)
        transport.assist.assert_not_called()


class StaleOrphanTests(CheckedThreads):
    def test_stale_orphan_never_resets_newer_answer_state(self):
        from unittest.mock import Mock
        state = Mock()
        state.overlay_scope_value = {}
        transport = Mock()
        overlay = bridge.OverlayBridge(transport=transport, scope={'project': 'demo'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                        'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        own_generation = overlay.paused_generation
        overlay.request_by_row[row_id] = True
        overlay.reserved_generation[row_id] = own_generation
        def assist_with_takeover(question, scope, rid, expected_generation=None):
            # Another thread's scope change + newer tap lands while our
            # assist call is in flight; the post-assist re-check observes it.
            overlay.paused_generation += 1
            overlay.request_by_row[row_id] = 'r-new'
            overlay.row_by_request['r-new'] = row_id
            overlay.answers[row_id] = {'answer': 'new partial', 'busy': True,
                                       'streaming': True, 'completed_only': False}
            return 'r-old'
        transport.assist.side_effect = assist_with_takeover
        overlay.ask(row_id, 'own question', click_generation=own_generation)
        self.assertEqual(overlay.request_by_row.get(row_id), 'r-new')
        self.assertEqual(overlay.row_by_request.get('r-new'), row_id)
        self.assertEqual(overlay.answers[row_id]['answer'], 'new partial')
        self.assertTrue(overlay.answers[row_id]['busy'])
        self.assertNotIn('r-old', overlay.row_by_request)
        transport.cancel.assert_called_once_with('r-old')


class ReservationTagHygieneTests(CheckedThreads):
    def test_generation_cleanup_drops_tags_with_reservations(self):
        from unittest.mock import Mock
        state = Mock()
        state.overlay_scope_value = {}
        transport = Mock()
        overlay = bridge.OverlayBridge(transport=transport, scope={'project': 'demo'})
        for cycle in range(20):
            overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                            'listening': True, 'generation': 0, 'capture_generation': 0})
            row_id = next(iter(overlay.questions))
            overlay.request_by_row[row_id] = True
            overlay.reserved_generation[row_id] = overlay.paused_generation
            # A new meeting/session/clear revision retires all rows.
            overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                            'listening': True, 'generation': cycle + 1, 'capture_generation': 0})
        self.assertEqual(dict(overlay.request_by_row), {})
        self.assertEqual(dict(overlay.reserved_generation), {})
