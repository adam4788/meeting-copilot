"""Overlay bridge tests; stdlib only, no GUI, network, devices, or permissions."""
import json
import unittest

import overlay_bridge as bridge


class SnapshotTests(unittest.TestCase):
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
        self.assertTrue(all(set(q) == {'id', 'question', 'answer', 'busy', 'streaming', 'completed_only'} for q in snapshot['questions']))
        json.dumps(snapshot)  # Swift decodes exactly this shape

    def test_paused_backend_produces_honest_message(self):
        status = {'segments': [], 'listening': False}
        snapshot = bridge.build_snapshot(status, message=bridge.default_message(status))
        message = str(snapshot.get('message') or '')
        self.assertIn('Paused', message)
        self.assertNotIn('stream', message.lower())


class ClickTests(unittest.TestCase):
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
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))  # busy
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': 'unknown'})))
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
            def assist(self, question, scope, row_id):
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
            self.assertTrue(overlay.on_click_line(__import__('json').dumps({'type': 'click', 'id': row_id})))
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
            def assist(self, question, scope, rid):
                return 'r-x'
            def assist_request(self, request_id):
                return {'request_id': request_id, 'row_id': 'some-other-row',
                        'status': 'done', 'answer': 'WRONG', 'streaming': False, 'completed_only': True}
            def cancel(self, request_id):
                calls.append({'cancelled': request_id})

        overlay.transport = CrossWired()
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertNotIn('WRONG', str(overlay.answers[row_id].get('answer')))
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertTrue(any(entry.get('cancelled') == 'r-x' for entry in calls))

    def test_assist_rejection_marks_row_idle_without_raise(self):
        class FailingTransport:
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertIn('failed', (overlay.message or '').lower())

    def test_scope_push_uses_current_pane_settings_and_cancels_stale_rows(self):
        calls = []
        entered, release = __import__('threading').Event(), __import__('threading').Event()
        import threading as _threading
        import time as _time

        class HangingTransport:
            def assist(self, question, scope, row_id):
                calls.append(dict(scope))
                entered.set()
                release.wait(5)
                return 'r-hang'
            def assist_request(self, request_id):
                _time.sleep(0.01)
                return {'request_id': request_id, 'row_id': 'row', 'status': 'streaming',
                        'answer': '', 'streaming': True, 'completed_only': False}
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('no global polling')

        overlay = bridge.OverlayBridge(transport=HangingTransport(), scope={'project': 'old'})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True, 'generation': 0, 'capture_generation': 0})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        self.assertTrue(entered.wait(5))
        overlay.set_scope({'project': 'new', 'answer_provider': 'meta', 'answer_model': 'muse-spark-1.3'})
        release.set()
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertEqual(overlay.scope['project'], 'new')
        self.assertFalse(overlay.answers.get(row_id, {}).get('busy'))
        self.assertEqual(calls[0].get('project'), 'old')

    def test_cancel_row_stops_its_worker_and_keeps_other_rows(self):
        import threading
        import time
        started = threading.Event()

        class SlowTransport:
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        self.assertTrue(started.wait(5))
        self.assertTrue(overlay.cancel_row(row_id))
        for _thread in list(overlay.threads):
            _thread.join(timeout=10)
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertFalse(overlay.cancel_row(row_id))

    def test_headless_run_without_poll_capability_returns(self):
        class ClickOnly:
            def assist(self, question, scope, row_id):
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
        overlay.helper_args = ['-c', 'pass']
        overlay.show_overlay()
        deadline = __import__('time').monotonic() + 10
        while overlay.process is not None and overlay.process.poll() is None and __import__('time').monotonic() < deadline:
            __import__('time').sleep(0.01)
        overlay.stop()
        overlay.stopping = True
        self.assertIsNone(overlay.process)


class BridgeLifecycleTests(ClickTests):
    """Status pump, fail-closed show, single-writer ordering, cleanup."""

    def test_status_pump_keeps_streaming_through_pause_switch_and_late_events(self):
        import sys as _sys
        import time
        seen = {'status': {'segments': [], 'listening': True}}
        transport = bridge.BackendTransport.__new__(bridge.BackendTransport)
        transport._state, transport._poll, transport._poll_context = None, None, None

        def poll():
            return dict(seen['status'])

        transport._poll = poll
        overlay = bridge.OverlayBridge(helper=_sys.executable, transport=transport, poll_interval=0.02)
        overlay.helper_args = ['-c', 'import sys\nfor line in sys.stdin: pass']
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
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        self.assertEqual(overlay.request_by_row.get(row_id), None)
        # Resume on a new meeting revision: stale rows stay retired until the
        # new revision renders them.
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves still?'}],
                        'listening': True, 'generation': 2, 'capture_generation': 2})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
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
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
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
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
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

            transport = bridge.BackendTransport(LeakCheckState())
            self.assertNotIn('pane-secret-value', repr(vars(transport)))
            overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.01)
            overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}],
                            'listening': True, 'generation': 0, 'capture_generation': 0})
            row_id = next(iter(overlay.questions))
            self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
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
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
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
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertEqual(overlay.answers[row_id]['answer'], 'Final answer.')
        self.assertTrue(overlay.answers[row_id]['completed_only'])

    def test_end_to_end_mocked_backend_pane_native_protocol(self):
        """Clicks reach the real request-specific row with current pane scope.

        Uses the real MeetingState + BackendTransport (not an HTTP fake): the
        pane scope flows through /overlay/show, the click creates a request
        slot, and streamed chunks land on the clicked row only.
        """
        import sys
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
        import sys as _sys2
        real_helper_path = state._overlay_helper_path
        state._overlay_helper_path = lambda: _sys2.executable
        import overlay_bridge as _bridge2
        real_bridge_init = _bridge2.OverlayBridge.__init__

        def patched_init(self, helper=None, transport=None, scope=None, poll_interval=0.25,
                         assist_timeout=90.0):
            real_bridge_init(self, helper=_sys2.executable, transport=transport, scope=scope,
                             poll_interval=0.01, assist_timeout=assist_timeout)
            self.helper_args = ['-c', 'import sys\nfor line in sys.stdin: pass']

        shown = None
        try:
            _bridge2.OverlayBridge.__init__ = patched_init
            shown = state.overlay_show({'project': 'demo', 'answer_provider': 'meta',
                                        'answer_model': 'muse-spark-1.3',
                                        'web_fallback': True})
        finally:
            _bridge2.OverlayBridge.__init__ = real_bridge_init
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
        state.overlay_scope(current_scope)
        if sys.platform == 'win32':
            self.skipTest('helper spawn is covered on macOS/Linux CI')
        row_deadline = time.monotonic() + 10
        clicked = False
        while time.monotonic() < row_deadline:
            try:
                clicked = overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id}))
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

    def test_timeout_cancels_slot_and_reports_retry(self):
        import time

        class Never:
            def assist(self, question, scope, row_id):
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
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=10)
        self.assertEqual(cancelled, ['r-1'])
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertIn('timed out', (overlay.message or '').lower())
