"""Merge-blocker regressions: deterministic, offline lifecycle boundaries."""
import json
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import overlay_bridge as bridge
import server
from thread_test_support import CheckedThreads


def click(overlay, row='row'):
    return json.dumps({'type': 'click', 'id': row,
                       'snapshot_id': overlay.pushed[-1].get('snapshot_id')})


class BlockerTests(CheckedThreads):
    def setUp(self):
        super().setUp()

    def backend(self):
        engine = Mock()
        engine.supports_streaming.return_value = False
        engine.answer.return_value = {'answer': 'Safe answer'}
        state = server.MeetingState(engine_factory=lambda: engine)
        state.listening = True
        state.add_segment({'id': 'row', 'speaker': 'remote', 'text': 'What is confidential?'})
        token = bridge._PANE_ACTION.set('blocker-test')
        try:
            transport = bridge.BackendTransport(state, pane_token='blocker-test')
        finally:
            bridge._PANE_ACTION.reset(token)
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.001)
        state.overlay = overlay
        state.overlay_owner = 'test-owner'
        overlay.update(state.status(touch=False))
        self.addCleanup(overlay.stop)
        return state, overlay, engine

    def join(self, overlay):
        for worker in list(overlay.threads):
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'worker did not exit')

    def test_buffered_native_click_cannot_rebind_identical_question(self):
        state, overlay, engine = self.backend()
        old_click = click(overlay)
        state.generation += 1
        overlay.update(state.status(touch=False))
        self.assertFalse(overlay.on_click_line(old_click))
        self.assertFalse(overlay.on_click_line(json.dumps({'type': 'click', 'id': 'row'})))
        self.assertTrue(overlay.on_click_line(click(overlay)))
        self.join(overlay)
        engine.answer.assert_called_once()
        self.assertEqual(overlay.answers['row']['answer'], 'Safe answer')

    def test_same_scope_authorization_refresh_retires_pending_tap(self):
        state, overlay, engine = self.backend()
        with patch.object(threading.Thread, 'start'):
            self.assertTrue(overlay.on_click_line(click(overlay)))
        queued = overlay._click_queue.get_nowait()
        import contextvars
        principal = contextvars.ContextVar('changed-principal')
        token = principal.set('new-principal')
        try:
            state.overlay_scope({'owner': state.overlay_owner, 'seq': 1})
        finally:
            principal.reset(token)
        overlay.ask(*queued)
        engine.answer.assert_not_called()

    def test_previous_owner_scope_is_rejected_even_with_higher_sequence(self):
        state, overlay, engine = self.backend()
        state.overlay_owner = 'new-lifecycle'
        result = state.overlay_scope({'owner': 'old-lifecycle', 'seq': 100,
                                      'all_projects': True, 'web_fallback': True})
        self.assertFalse(result['accepted'])
        self.assertEqual(state.overlay_scope_value, {})

    def test_reused_show_rotates_owner_and_resets_sequence(self):
        state, overlay, engine = self.backend()
        overlay.process = Mock(stdin=None, stdout=None)
        overlay.process.poll.return_value = None
        with patch.object(state, '_start_overlay_lease_locked'):
            old = state.overlay_show({'all_projects': True, 'seq': 90})
            new = state.overlay_show({'project': 'narrow', 'seq': 0})
        self.assertNotEqual(old['owner'], new['owner'])
        self.assertTrue(new['reused'])
        self.assertFalse(state.overlay_scope({'owner': old['owner'], 'seq': 999,
                                             'all_projects': True})['accepted'])
        self.assertTrue(state.overlay_scope({'owner': new['owner'], 'seq': 1,
                                            'project': 'narrow'})['accepted'])
        self.assertEqual(state.overlay_scope_value['project'], 'narrow')

    def test_clear_retires_overlay_without_waiting_for_poll(self):
        state, overlay, engine = self.backend()
        overlay.answers['row']['answer'] = 'PRIVATE'
        clear = next(r.endpoint for r in server.make_router(state).routes if r.path == '/clear')
        clear()
        before = len(overlay.pushed)
        overlay.push()
        self.assertEqual(len(overlay.pushed), before)
        self.assertEqual(overlay.segments, [])
        self.assertEqual(overlay.answers, {})

    def test_pause_preserves_completed_but_retires_pending(self):
        state, overlay, engine = self.backend()
        state.add_segment({'id': 'pending', 'speaker': 'remote', 'text': 'When is launch?'})
        overlay.update(state.status(touch=False))
        engine.answer.return_value = {'answer': 'Completed'}
        self.assertTrue(overlay.on_click_line(click(overlay)))
        self.join(overlay)
        self.assertTrue(overlay.answers['row']['completed'])
        overlay.answers['pending'].update(answer='Partial', busy=True)
        overlay.request_by_row['pending'] = 'pending-request'
        overlay.row_by_request['pending-request'] = 'pending'
        old_id = overlay.snapshot_id
        state.pause()
        overlay.update(state.status(touch=False))
        self.assertEqual(overlay.answers['row']['answer'], 'Completed')
        self.assertIsNone(overlay.answers['pending']['answer'])
        self.assertFalse(overlay.answers['pending']['busy'])
        self.assertEqual(overlay.request_by_row, {})
        self.assertNotEqual(overlay.snapshot_id, old_id)
        # Resume the same meeting without touching real capture hardware.
        state.listening = True
        state.capture_generation += 1
        overlay.update(state.status(touch=False))
        self.assertEqual(overlay.answers['row']['answer'], 'Completed')

    def test_stale_non_dict_poller_result_does_not_change_state(self):
        state, overlay, engine = self.backend()
        before = dict(overlay.pushed[-1])
        overlay._poller_seq = 2
        self.assertFalse(overlay.update(None, poller_seq=1))
        self.assertEqual(overlay.pushed[-1], before)
        self.assertTrue(overlay._listening)

    def test_long_explicit_ids_are_deduplicated_after_bounding(self):
        rows = bridge.suggest_questions([
            {'id': 'x' * 200 + 'a', 'text': 'Who approves?'},
            {'id': 'x' * 200 + 'b', 'text': 'When is launch?'}])
        self.assertEqual(len({r['id'] for r in rows}), 2)

    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'POSIX pipe/select required')
    def test_blocked_real_pipe_cannot_stall_scope_and_hide(self):
        import os
        import subprocess
        import sys
        state, overlay, engine = self.backend()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        overlay.process = child
        fd = child.stdin.fileno()
        os.set_blocking(fd, False)
        try:
            while True:
                os.write(fd, b'x' * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(fd, True)
        scope_done, hide_done = threading.Event(), threading.Event()
        def scope():
            state.overlay_scope({'owner': 'test-owner', 'project': 'narrow', 'seq': 1})
            scope_done.set()
        def hide():
            state.overlay_hide()
            hide_done.set()
        updater = threading.Thread(target=scope, daemon=True)
        hider = threading.Thread(target=hide, daemon=True)
        updater.start()
        try:
            scope_completed = scope_done.wait(0.5)
            hider.start()
            hide_completed = hide_done.wait(1)
        finally:
            child.terminate()
            child.wait(timeout=2)
            updater.join(2)
            hider.join(2)
        self.assertTrue(scope_completed, 'scope blocked on helper IO under publication lock')
        self.assertTrue(hide_completed, 'hide could not retire blocked writer')

    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'POSIX pipe/select required')
    def test_real_fd_writer_roundtrip_and_full_pipe_deadline(self):
        import os
        import time
        reader, writer = os.pipe()
        stream = os.fdopen(writer, 'wb', buffering=0)
        try:
            bridge.OverlayBridge._write_frame(stream, b'{"snapshot_id":"current"}\n')
            self.assertEqual(os.read(reader, 4096), b'{"snapshot_id":"current"}\n')
            try:
                while True:
                    os.write(writer, b'x' * 4096)
            except BlockingIOError:
                pass
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                bridge.OverlayBridge._write_frame(stream, b'blocked\n')
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            stream.close()
            os.close(reader)

    def test_hide_during_provider_preflight_prevents_provider_call(self):
        state, overlay, engine = self.backend()
        entered, release, passed = (threading.Event() for _ in range(3))
        def supports():
            entered.set()
            if not release.wait(2):
                raise AssertionError('preflight gate timed out')
            passed.set()
            return False
        engine.supports_streaming.side_effect = self.checked_callback(supports)
        self.assertTrue(overlay.on_click_line(click(overlay)))
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(passed.is_set())
            state.overlay_hide()
            self.assertFalse(passed.is_set(), 'Hide must return before releasing preflight')
        finally:
            release.set()
        self.join(overlay)
        import time
        deadline = time.monotonic() + 2
        while state.live_workers and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(state.live_workers, 0)
        self.assertEqual(self.worker_errors, [], 'provider callback exceptions must not be swallowed')
        self.assertTrue(passed.is_set(), 'preflight must finish only after Hide and explicit release')
        engine.supports_streaming.assert_called_once()
        engine.answer.assert_not_called()

    def test_admission_assertions_are_not_swallowed_as_rejection(self):
        transport = Mock()
        transport.assist.side_effect = AssertionError('worker barrier timeout')
        overlay = bridge.OverlayBridge(transport=transport)
        overlay.update({'listening': True, 'generation': 1,
                        'segments': [{'id': 'row', 'text': 'Who approves?'}]})
        overlay.request_by_row['row'] = True
        overlay.reserved_generation['row'] = overlay.paused_generation
        with self.assertRaisesRegex(AssertionError, 'worker barrier timeout'):
            overlay.ask('row', overlay.questions['row'], overlay.paused_generation)

    def test_legacy_transport_gets_clear_error_without_unsafe_retry(self):
        class Legacy:
            calls = 0
            def assist(self, question, scope, row_id):
                self.calls += 1
                return 'unsafe'
        transport = Legacy()
        overlay = bridge.OverlayBridge(transport=transport)
        overlay.update({'listening': True, 'generation': 1,
                        'segments': [{'id': 'row', 'text': 'Who approves?'}]})
        self.assertTrue(overlay.on_click_line(click(overlay)))
        self.join(overlay)
        self.assertEqual(transport.calls, 0)
        self.assertIn('expected_generation', overlay.message)
        self.assertFalse(overlay.answers['row']['busy'])

    def test_hide_at_admission_exit_cancels_before_provider_start(self):
        state, overlay, engine = self.backend()
        pending_workers = []
        mapped = []
        real_lock = state.overlay_lock
        class HideOnExit:
            def __enter__(self):
                return real_lock.__enter__()
            def __exit__(guard, *args):
                mapped.append(bool(overlay.row_by_request))
                real_lock.__exit__(*args)
                state.overlay_lock = real_lock
                state.overlay_hide()
                # Execute the real backend worker at the exact former gap:
                # admitted slot exists; bridge hasn't resumed after admission.
                for worker in pending_workers:
                    worker.run()
        state.overlay_lock = HideOnExit()
        overlay.request_by_row['row'] = True
        overlay.reserved_generation['row'] = overlay.paused_generation
        overlay.answers['row']['busy'] = True
        original_start = threading.Thread.start
        with patch.object(threading.Thread, 'start', lambda worker: original_start(worker)
                          if worker.name == 'meeting-overlay-cleanup' else pending_workers.append(worker)):
            overlay.ask('row', overlay.questions['row'], overlay.paused_generation)
        self.assertEqual(mapped, [True])
        self.assertEqual(len(pending_workers), 1)
        engine.answer.assert_not_called()
        self.assertEqual(next(iter(state.requests.values()))['status'], 'cancelled')

    def test_inflight_emission_is_fenced_by_clear(self):
        state, overlay, engine = self.backend()
        old = dict(overlay.pushed[-1])
        arrived, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        writer = None
        class Gate:
            def __enter__(guard):
                lock.acquire()
                if threading.current_thread() is writer:
                    arrived.set()
                    if not release.wait(2):
                        lock.release()
                        raise AssertionError('emission gate timed out')
                return guard
            def __exit__(guard, *args):
                lock.release()
        overlay._stdin_lock = Gate()
        writer = threading.Thread(target=lambda: overlay._emit(old), daemon=True)
        writer.start()
        try:
            self.assertTrue(arrived.wait(2))
            clear = next(r.endpoint for r in server.make_router(state).routes if r.path == '/clear')
            clear()
            count = len(overlay.pushed)
        finally:
            release.set()
            writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(len(overlay.pushed), count)

    def test_clear_serializes_with_delivery_already_past_validation(self):
        state, overlay, engine = self.backend()
        entered, release, attempted, cleared = (threading.Event() for _ in range(4))
        class Sink(list):
            def append(sink, frame):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('delivery gate timed out')
                super().append(frame)
        overlay.pushed = Sink()
        frame = {'type': 'snapshot', 'snapshot_id': overlay.snapshot_id,
                 'transcript': [{'text': 'PRIVATE'}], 'questions': []}
        writer = threading.Thread(target=lambda: overlay._emit(frame), daemon=True)
        endpoint = next(r.endpoint for r in server.make_router(state).routes if r.path == '/clear')
        def clear():
            attempted.set()
            endpoint()
            cleared.set()
        clearer = threading.Thread(target=clear, daemon=True)
        writer.start()
        try:
            self.assertTrue(entered.wait(2))
            clearer.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(cleared.wait(0.1), 'Clear returned while old frame could still publish')
        finally:
            release.set()
            writer.join(2)
            clearer.join(2)
        self.assertTrue(cleared.is_set())
        self.assertFalse(writer.is_alive())
        self.assertFalse(clearer.is_alive())
        count = len(overlay.pushed)
        self.assertFalse(overlay._emit(frame))
        self.assertEqual(len(overlay.pushed), count)

    def test_nonreentrant_publication_lock_is_supported(self):
        state, overlay, engine = self.backend()
        state.overlay_lock = threading.Lock()
        self.assertTrue(overlay.on_click_line(click(overlay)))
        worker = overlay.threads[-1]
        worker.join(1)
        deadlocked = worker.is_alive()
        if deadlocked:
            state.overlay_lock.release()  # Rescue the buggy nested acquisition.
            worker.join(1)
        self.assertFalse(deadlocked, 'nested acquisition of a plain publication Lock')
        engine.answer.assert_called_once()
