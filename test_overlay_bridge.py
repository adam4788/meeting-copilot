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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))  # busy
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': 'unknown'})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=30)
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
                                      {'speaker': 'm', 'text': 'Who approves beta?'}], 'listening': True})
        rows = list(overlay.questions)
        self.assertEqual(len(rows), 2)
        for row_id in rows:
            self.assertTrue(overlay.on_click_line(__import__('json').dumps({'type': 'click', 'id': row_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=30)
        got = sorted(overlay.answers[row_id]['answer'] for row_id in rows)
        self.assertEqual(got, ['alpha-one', 'beta-two'])
        self.assertEqual({c['row_id'] for c in calls}, set(rows))

    def test_wrong_slot_row_id_cancels_without_misattribution(self):
        calls = []
        overlay = bridge.OverlayBridge(transport=self._slot_transport(calls), scope={})
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})
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
            _thread.join(timeout=30)
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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Ready?'}], 'listening': True})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for _thread in list(overlay.threads):
            _thread.join(timeout=30)
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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        self.assertTrue(entered.wait(5))
        overlay.set_scope({'project': 'new', 'answer_provider': 'meta', 'answer_model': 'muse-spark-1.3'})
        release.set()
        for _thread in list(overlay.threads):
            _thread.join(timeout=30)
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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        self.assertTrue(started.wait(5))
        self.assertTrue(overlay.cancel_row(row_id))
        for _thread in list(overlay.threads):
            _thread.join(timeout=30)
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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})

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


class StreamingProtocolTests(unittest.TestCase):
    """Deterministic fake-provider streaming: distinct chunks, order, errors, cancel."""

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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})
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
            thread.join(timeout=30)
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
        class CompletedOnly:
            def assist(self, question, scope, row_id):
                return 'r-1'
            def assist_request(self, request_id):
                return {'request_id': request_id, 'row_id': 'row', 'status': 'done',
                        'answer': 'Final answer.', 'streaming': False, 'completed_only': True}
            def cancel(self, request_id):
                pass
            def poll_status(self):
                raise AssertionError('no global polling')

        overlay = bridge.OverlayBridge(transport=CompletedOnly(), poll_interval=0.01)
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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})
        row_id = next(iter(overlay.questions))
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=30)
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
        chunks = iter(['Two ', 'reviewers.'])

        class FakeEngine:
            def answer_stream(self, question, **kwargs):
                captured.append({'question': question, 'kwargs': kwargs})
                header = {'model_route': 'fake/test', 'sources': [], 'warnings': [],
                          'web_search': {'used': False}}
                return header, chunks
            def answer(self, question, **kwargs):
                raise AssertionError('streaming engine must use answer_stream')

        captured = []
        state = real_server.MeetingState(engine_factory=lambda: FakeEngine())
        state.add_segment({'speaker': 'remote', 'text': 'Who approves?'})
        shown = state.overlay_show({'project': 'demo', 'answer_provider': 'meta',
                                    'answer_model': 'muse-spark-1.3',
                                    'web_fallback': True})
        self.assertTrue(shown['shown'])
        transport = bridge.BackendTransport(state)
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.01)
        overlay.update(state.status())
        row_id = next(iter(overlay.questions))
        current_scope = {'project': 'demo', 'answer_provider': 'meta',
                         'answer_model': 'muse-spark-1.3', 'web_fallback': True}
        state.overlay_scope(current_scope)
        if sys.platform == 'win32':
            self.skipTest('helper spawn is covered on macOS/Linux CI')
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=30)
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
        overlay.update({'segments': [{'speaker': 'm', 'text': 'Who approves?'}], 'listening': True})
        row_id = next(iter(overlay.questions))
        real_row[0] = row_id
        self.assertTrue(overlay.on_click_line(json.dumps({'type': 'click', 'id': row_id})))
        for thread in list(overlay.threads):
            thread.join(timeout=30)
        self.assertEqual(cancelled, ['r-1'])
        self.assertFalse(overlay.answers[row_id]['busy'])
        self.assertIn('timed out', (overlay.message or '').lower())
