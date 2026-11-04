"""Deterministic overlay privacy regressions; no devices or live providers."""
import json
import threading
import unittest
from thread_test_support import CheckedThreads
from unittest.mock import Mock, patch

import overlay_bridge as bridge
import server


class PrivacyTests(CheckedThreads):
    def make_backend(self):
        engine = Mock()
        engine.supports_streaming.return_value = False
        engine.answer.return_value = {'answer': 'Safe answer'}
        state = server.MeetingState(engine_factory=lambda: engine)
        state.bridge = Mock()
        state.watchdog = Mock()
        state.start(True, recording_id='test-recording')
        state.add_segment({'id': 'row', 'speaker': 'remote', 'text': 'What is confidential?'})
        token = bridge._PANE_ACTION.set('privacy-test')
        try:
            transport = bridge.BackendTransport(state, pane_token='privacy-test')
        finally:
            bridge._PANE_ACTION.reset(token)
        overlay = bridge.OverlayBridge(transport=transport, poll_interval=0.001)
        overlay.update(state.status(touch=False))
        self.addCleanup(overlay.stop)
        self.addCleanup(state.pause)
        return state, overlay, engine

    def click(self, overlay):
        return overlay.on_click_line(json.dumps({'type': 'click', 'id': 'row', 'snapshot_id': overlay.snapshot_id}))

    def join_workers(self, overlay):
        for worker in list(overlay.threads):
            worker.join(3)
            self.assertFalse(worker.is_alive(), 'overlay worker deadlocked')

    def test_reopened_helper_accepts_new_tap_but_not_pre_stop_reservation(self):
        state, overlay, engine = self.make_backend()
        entered, release = threading.Event(), threading.Event()
        original_ask = overlay.ask
        first = True
        def held_ask(*args):
            nonlocal first
            if first:
                first = False
                entered.set()
                if not release.wait(3):
                    raise AssertionError('pre-stop tap barrier timeout')
            return original_ask(*args)
        helper = Mock(stdin=None, stdout=None)
        helper.poll.return_value = None
        def terminate():
            helper.poll.return_value = 0
        helper.terminate.side_effect = terminate
        def start_helper():
            # This tests reservation fencing, not POSIX launch. Inject at the
            # platform boundary so the same assertions run on Windows.
            overlay.process = helper
            return helper
        try:
            with patch.object(overlay, 'ask', side_effect=held_ask):
                self.assertTrue(self.click(overlay))
                self.assertTrue(entered.wait(3))
                overlay.stop()
                with patch.object(overlay, 'start_helper', side_effect=start_helper), \
                        patch.object(overlay, '_read_clicks'), \
                        patch.object(overlay, '_start_poller_locked'), \
                        patch.object(overlay, '_start_pusher_locked'):
                    self.assertTrue(overlay.show_overlay()['shown'])
                overlay.update(state.status(touch=False))
                self.assertTrue(self.click(overlay))
                newest = overlay.threads[-1]
                newest.join(3)
                self.assertFalse(newest.is_alive())
                self.assertEqual(overlay.answers['row']['answer'], 'Safe answer')
                release.set()
                self.join_workers(overlay)
                engine.answer.assert_called_once()
                self.assertEqual(overlay.answers['row']['answer'], 'Safe answer')
        finally:
            release.set()
            self.join_workers(overlay)

    def test_empty_authoritative_scope_never_falls_back_to_broader_caller(self):
        state, overlay, engine = self.make_backend()
        overlay.scope = {'all_projects': True, 'web_fallback': True}
        state.overlay_scope_value = {}
        self.assertTrue(self.click(overlay))
        self.join_workers(overlay)
        engine.answer.assert_called_once()
        self.assertFalse(engine.answer.call_args.kwargs['all_projects'])
        self.assertFalse(engine.answer.call_args.kwargs['web_fallback'])

    def test_injected_transport_compatibility_never_drops_admission_preconditions(self):
        calls = []
        class Legacy:
            def assist(self, question, scope, row_id):
                calls.append(question)
                return 'unsafe'
        overlay = bridge.OverlayBridge(transport=Legacy())
        overlay.update({'segments': [{'id': 'row', 'text': 'What is safe?'}],
                        'listening': True, 'generation': 1})
        self.assertTrue(self.click(overlay))
        self.join_workers(overlay)
        self.assertEqual(calls, [])
        self.assertFalse(overlay.answers['row']['busy'])
        self.assertIn('not accepted', overlay.message)
        # An implementation that accepts the optional generation keeps working.
        class Compatible:
            def assist(self, question, scope, row_id, expected_generation=None):
                calls.append(expected_generation)
                return 'safe'
            def assist_request(self, request_id):
                return {'row_id': 'row', 'status': 'done', 'answer': 'Safe answer'}
        overlay.transport = Compatible()
        self.assertTrue(self.click(overlay))
        self.join_workers(overlay)
        self.assertEqual(calls, [1])
        self.assertEqual(overlay.answers['row']['answer'], 'Safe answer')

    def test_hide_invalidates_before_slow_helper_teardown(self):
        state, overlay, engine = self.make_backend()
        state.overlay = overlay
        state.overlay_owner = 'test-owner'
        entered, teardown = threading.Event(), threading.Event()
        original_hide = overlay.hide_overlay
        def held_hide():
            entered.set()
            if teardown.wait(3):
                return original_hide()
        with patch.object(overlay, 'hide_overlay', side_effect=held_hide):
            worker = threading.Thread(target=state.overlay_hide, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertIsNone(state.overlay)
                self.assertFalse(self.click(overlay))
                self.join_workers(overlay)
                engine.answer.assert_not_called()
            finally:
                teardown.set()
                worker.join(3)
                self.join_workers(overlay)
        self.assertFalse(worker.is_alive())

    def test_full_click_queue_never_emits_under_bridge_lock(self):
        overlay = bridge.OverlayBridge(transport=Mock())
        overlay.update({'segments': [{'id': 'row', 'text': 'What is safe?'}], 'listening': True})
        for _ in range(32):
            overlay._click_queue.put_nowait(('unused', 'Unused?', 0))
        held, contender = threading.Event(), threading.Event()
        stdin_lock = threading.Lock()
        errors = []
        emitter = None

        class OrderedStdin:
            def __enter__(self):
                if threading.current_thread() is emitter:
                    stdin_lock.acquire()
                    held.set()
                    if not contender.wait(3):
                        stdin_lock.release()
                        raise AssertionError('queue-full path never attempted emission')
                else:
                    contender.set()
                    if overlay.lock._is_owned():
                        raise AssertionError('bridge -> stdin lock inversion')
                    stdin_lock.acquire()
                return self

            def __exit__(self, *args):
                stdin_lock.release()

        overlay._stdin_lock = OrderedStdin()
        def emit():
            try:
                overlay._emit({'type': 'snapshot', 'transcript': [], 'questions': []})
            except Exception as exc:
                errors.append(exc)
        emitter = threading.Thread(target=emit, daemon=True)
        emitter.start()
        try:
            self.assertTrue(held.wait(3))
            self.assertFalse(self.click(overlay))
        finally:
            contender.set()
            emitter.join(3)
        self.assertFalse(emitter.is_alive(), 'emitter/click deadlock')
        self.assertEqual(errors, [])
        self.assertFalse(overlay.answers['row']['busy'])
        self.assertEqual(overlay.request_by_row, {})
        self.assertIn('Too many', overlay.pushed[-1]['message'])

    def test_corrected_question_retires_queued_click_and_allows_new_click(self):
        for deliver_status in (False, True):
            with self.subTest(deliver_status=deliver_status):
                state, overlay, engine = self.make_backend()
                entered, release = threading.Event(), threading.Event()
                original_ask = overlay.ask

                def held_ask(*args):
                    entered.set()
                    if release.wait(3):
                        return original_ask(*args)

                try:
                    with patch.object(overlay, 'ask', side_effect=held_ask):
                        self.assertTrue(self.click(overlay))
                        self.assertTrue(entered.wait(3))
                        with state.lock:
                            state.segments.clear()
                        state.add_segment({'id': 'row', 'speaker': 'remote', 'text': 'What is public?'})
                        if deliver_status:
                            overlay.update(state.status(touch=False))
                        release.set()
                        self.join_workers(overlay)
                    engine.answer.assert_not_called()
                    self.assertEqual(state.requests, {})
                    overlay.update(state.status(touch=False))
                    self.assertTrue(self.click(overlay))
                    self.join_workers(overlay)
                    engine.answer.assert_called_once()
                    self.assertEqual(engine.answer.call_args.args[0], 'What is public?')
                    self.assertEqual(overlay.answers['row']['answer'], 'Safe answer')
                finally:
                    release.set()
                    self.join_workers(overlay)

    def test_scope_publication_and_admission_share_outer_lock(self):
        state, overlay, engine = self.make_backend()
        state.overlay = overlay
        state.overlay_owner = 'test-owner'
        state.overlay_scope({'owner': state.overlay_owner, 'all_projects': True, 'web_fallback': True, 'seq': 1})
        changed, publish = threading.Event(), threading.Event()
        attempted = threading.Event()
        def answer(*args, **kwargs):
            attempted.set()  # Also exposes the old unguarded provider path.
            return {'answer': 'Safe answer'}
        engine.answer.side_effect = answer
        original_set = overlay.set_scope
        original_lock = state.overlay_lock
        main = threading.current_thread()
        updater = None

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() not in (main, updater):
                    attempted.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        state.overlay_lock = ObservedLock()

        def hold_publication(scope):
            result = original_set(scope)
            changed.set()
            if not publish.wait(3):
                raise AssertionError('scope publication barrier timeout')
            return result

        try:
            with patch.object(overlay, 'set_scope', side_effect=self.checked_callback(hold_publication)):
                updater = threading.Thread(target=lambda: state.overlay_scope({'owner': state.overlay_owner, 'project': 'narrow', 'seq': 2}), daemon=True)
                updater.start()
                self.assertTrue(changed.wait(3))
                overlay.update(state.status(touch=False))
                self.assertTrue(self.click(overlay))
                self.assertTrue(attempted.wait(3), 'click never acquired scope publication lock')
                engine.answer.assert_not_called()
                publish.set()
                updater.join(3)
                self.assertFalse(updater.is_alive(), 'scope/admission lock inversion')
                self.join_workers(overlay)
                # The queued tap predates the new authorization binding, even
                # though its scope was already narrowed. Require a fresh tap.
                engine.answer.assert_not_called()
                self.assertTrue(self.click(overlay))
                self.join_workers(overlay)
                engine.answer.assert_called_once()
                kwargs = engine.answer.call_args.kwargs
                self.assertEqual(kwargs['project'], 'narrow')
                self.assertFalse(kwargs['all_projects'])
                self.assertFalse(kwargs['web_fallback'])
        finally:
            publish.set()
            if updater:
                updater.join(3)
            self.join_workers(overlay)
            state.overlay = None

    def test_queued_click_cannot_start_after_stop_hide_or_broken_pipe(self):
        for shutdown in ('stop', 'hide', 'broken_pipe'):
            with self.subTest(shutdown=shutdown):
                state, overlay, engine = self.make_backend()
                entered, release = threading.Event(), threading.Event()
                original_ask = overlay.ask

                def held_ask(*args):
                    entered.set()
                    if release.wait(3):
                        return original_ask(*args)

                try:
                    with patch.object(overlay, 'ask', side_effect=held_ask):
                        self.assertTrue(self.click(overlay))
                        self.assertTrue(entered.wait(3))
                        if shutdown == 'stop':
                            overlay.stop()
                        elif shutdown == 'hide':
                            state.overlay = overlay
                            state.overlay_owner = 'test-owner'
                            state.overlay_hide()
                        else:
                            overlay._broken_pipe_shutdown(None)
                        release.set()
                        self.join_workers(overlay)
                    engine.answer.assert_not_called()
                    self.assertEqual(state.requests, {})
                    self.assertEqual(overlay.request_by_row, {})
                    self.assertEqual(overlay.reserved_generation, {})
                    self.assertFalse(self.click(overlay))
                    overlay.update(state.status(touch=False))
                    self.assertFalse(self.click(overlay))
                finally:
                    release.set()
                    self.join_workers(overlay)

    def test_old_status_cannot_restore_clear_pause_or_same_epoch_content(self):
        for transition in ('clear', 'pause', 'resume', 'same_epoch'):
            with self.subTest(transition=transition):
                state, overlay, engine = self.make_backend()
                old = state.status(touch=False)
                entered, release = threading.Event(), threading.Event()

                def late_delivery():
                    entered.set()
                    if release.wait(3):
                        overlay.update(old)

                worker = threading.Thread(target=late_delivery, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(3))
                    if transition == 'clear':
                        # Real Clear route; avoid attaching capture after clear.
                        state.pause()
                        clear = next(r.endpoint for r in server.make_router(state).routes if r.path == '/clear')
                        clear()
                    elif transition == 'pause':
                        state.pause()
                    elif transition == 'resume':
                        state.pause()
                        state.start(True, recording_id='test-recording')
                    else:
                        state.add_segment({'id': 'new', 'speaker': 'remote', 'text': 'Newer status?'})
                    fresh = state.status(touch=False)
                    overlay.update(fresh)
                    expected = json.dumps(overlay.pushed[-1], sort_keys=True)
                    release.set()
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(json.dumps(overlay.pushed[-1], sort_keys=True), expected)
                    self.assertEqual(overlay.segments, fresh['segments'])
                    # Even missing ordering metadata cannot downgrade a real stream.
                    overlay.update({'segments': old['segments'], 'listening': True})
                    self.assertEqual(overlay.segments, fresh['segments'])
                    self.assertEqual(overlay._listening, fresh['listening'])
                finally:
                    release.set()
                    worker.join(3)

    def test_pause_and_same_session_resume_reject_before_next_poll(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                state, overlay, engine = self.make_backend()
                entered, release = threading.Event(), threading.Event()
                original_lock = state.lock
                main = threading.current_thread()

                class Gate:
                    def __enter__(self):
                        if threading.current_thread() is not main and not entered.is_set():
                            entered.set()
                            if not release.wait(3):
                                raise AssertionError('admission barrier timeout')
                        return original_lock.__enter__()

                    def __exit__(self, *args):
                        return original_lock.__exit__(*args)

                state.lock = Gate()
                try:
                    self.assertTrue(self.click(overlay))
                    self.assertTrue(entered.wait(3))
                    before = state.generation
                    state.pause()
                    if resume:
                        # Lease refresh is unrelated to capture admission and
                        # takes the outer publication lock held by this tap.
                        with patch.object(state, '_touch_pane_seen'):
                            state.start(True, recording_id='test-recording')
                    self.assertEqual(state.generation, before)
                    release.set()
                    self.join_workers(overlay)
                    engine.answer.assert_not_called()
                    self.assertEqual(state.requests, {})
                    self.assertEqual(state.request_seq, 0)
                    self.assertEqual(state.live_workers, 0)
                    if not resume:
                        state.start(True, recording_id='test-recording')
                    overlay.update(state.status(touch=False))
                    self.assertTrue(self.click(overlay))
                    self.join_workers(overlay)
                    engine.answer.assert_called_once()
                    self.assertEqual(overlay.answers['row']['answer'], 'Safe answer')
                finally:
                    release.set()
                    self.join_workers(overlay)
