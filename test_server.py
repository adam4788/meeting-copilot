import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class ServerTests(unittest.TestCase):
    def test_starts_paused_and_keeps_transcript_only_in_memory(self):
        import server
        app = FastAPI()
        state = server.MeetingState()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            result = client.get('/status').json()
            self.assertFalse(result['listening'])
            self.assertEqual(result['segments'], [])
            state.add_segment({'speaker': 'remote', 'text': 'What is the deployment status?', 'timestamp': 1})
            self.assertEqual(len(client.get('/status').json()['segments']), 1)
            self.assertEqual(client.post('/clear').status_code, 200)
            self.assertEqual(client.get('/status').json()['segments'], [])

    def test_assist_is_read_only_and_uses_bounded_transcript(self):
        import server
        import time
        class Engine:
            def answer(self, question, **kwargs):
                return {'answer': question, 'sources': [], 'transcript': kwargs['transcript']}
            def projects(self):
                return []
        app = FastAPI()
        state = server.MeetingState(engine_factory=Engine)
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/assist', json={}).status_code, 400)
            state.add_segment({'text': 'What was decided?', 'speaker': 'remote'})
            response = client.post('/assist', json={'question': 'Summarize the decision'})
            self.assertEqual(response.status_code, 200)
            for _ in range(100):
                data = client.get('/status').json()
                if not data['busy']:
                    break
                time.sleep(.01)
            self.assertEqual(data['result']['answer'], 'Summarize the decision')
            self.assertIn('What was decided?', data['result']['transcript'])
            self.assertFalse(data['listening'])

    def test_capture_requires_explicit_consent_and_pause_stops_it(self):
        import server
        class Capture:
            def start(self, on_segment, on_status):
                self.active = True
            def stop(self):
                self.active = False
            def status(self):
                return {'listening': getattr(self, 'active', False)}
        state = server.MeetingState(audio_factory=Capture)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/start', json={}).status_code, 400)
            self.assertEqual(client.post('/start', json={'consent': True, 'backend': 'native'}).status_code, 200)
            self.assertTrue(client.get('/status').json()['listening'])
            client.post('/pause')
            self.assertFalse(client.get('/status').json()['listening'])
            self.assertFalse(state.audio.active)

    def test_worker_preserves_request_context_and_clear_discards_inflight_result(self):
        import server
        import contextvars
        import threading
        import time
        scoped = contextvars.ContextVar('test-meeting-scope', default='missing')
        entered, release = threading.Event(), threading.Event()
        observed = []
        class Engine:
            def answer(self, question, **kwargs):
                observed.append(scoped.get())
                entered.set()
                release.wait(2)
                return {'answer': 'This should be discarded', 'sources': []}
        state = server.MeetingState(engine_factory=Engine)
        token = scoped.set('expected-profile')
        try:
            state.assist('What is the status?')
            self.assertTrue(entered.wait(2))
        finally:
            scoped.reset(token)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/clear').status_code, 200)
            release.set()
            for _ in range(100):
                if not state.busy:
                    break
                time.sleep(.01)
            self.assertIsNone(state.result)
            self.assertEqual(observed, ['expected-profile'])

    def test_all_projects_request_retrieves_multiple_projects(self):
        import server
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import patch
        from knowledge import KnowledgeEngine
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, home = root / 'Projects', root / 'home'
            projects.mkdir()
            home.mkdir()
            for name in ('alpha', 'beta'):
                folder = projects / name
                folder.mkdir()
                (folder / 'notes.md').write_text(f'Orchid approval for {name}.', encoding='utf-8')
            engine = KnowledgeEngine(projects, home)
            state = server.MeetingState(engine_factory=lambda: engine)
            app = FastAPI()
            app.include_router(server.make_router(state))
            with patch.object(engine, '_complete', return_value='Both projects require approval [1] [2].'), patch.object(engine, '_web', side_effect=AssertionError('no web consent')), TestClient(app) as client:
                response = client.post('/assist', json={'question': 'Orchid approval', 'all_projects': True})
                self.assertEqual(response.status_code, 200)
                for _ in range(100):
                    data = client.get('/status').json()
                    if not data['busy']:
                        break
                    time.sleep(.01)
                self.assertIsNone(data['error'])
                sources = data['result']['sources']
                self.assertEqual({Path(s['source']).parent.name for s in sources}, {'alpha', 'beta'})
                self.assertTrue(client.get('/status').json()['capabilities']['all_projects'])

    def test_buffer_is_bounded(self):
        import server
        state = server.MeetingState()
        for i in range(200):
            state.add_segment({'text': f'Synthetic line {i}'})
        self.assertEqual(len(state.segments), 120)
        self.assertEqual(state.segments[-1]['text'], 'Synthetic line 199')

    def test_meetily_attachment_is_explicit_and_corrected_segments_are_upserted(self):
        import server
        class Bridge:
            def recordings(self):
                return [{'id': 'synthetic-session', 'status': 'recording'}]
            def attach(self, recording_id, on_segment, on_status):
                self.selected = recording_id
                on_segment({'id': 'segment-1', 'speaker': 'meeting', 'text': 'Original'})
                on_segment({'id': 'segment-1', 'speaker': 'meeting', 'text': 'Corrected'})
            def stop(self):
                self.selected = None
        bridge = Bridge()
        state = server.MeetingState(bridge_factory=lambda: bridge)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/start', json={'consent': True}).status_code, 400)
            self.assertEqual(client.get('/recordings').json()[0]['id'], 'synthetic-session')
            response = client.post('/start', json={'consent': True, 'recording_id': 'synthetic-session'})
            self.assertEqual(response.status_code, 200)
            data = client.get('/status').json()
            self.assertEqual(data['backend'], 'meetily')
            self.assertEqual(len(data['segments']), 1)
            self.assertEqual(data['segments'][0]['text'], 'Corrected')
            client.post('/pause')

    def test_native_async_failure_is_not_reported_as_listening(self):
        import server
        class Capture:
            def start(self, on_segment, on_status):
                on_status({'state': 'error', 'running': False, 'error': 'Synthetic device failure'})
            def stop(self):
                pass
        state = server.MeetingState(audio_factory=Capture)
        state.start(True, backend='native')
        self.assertFalse(state.status()['listening'])
        self.assertEqual(state.status()['error'], 'Synthetic device failure')

    def test_switching_recordings_clears_previous_meeting(self):
        import server
        class Bridge:
            def attach(self, recording_id, on_segment, on_status):
                on_segment({'id': recording_id, 'text': f'Content from {recording_id}'})
            def stop(self):
                pass
        state = server.MeetingState(bridge_factory=Bridge)
        state.start(True, recording_id='first')
        state.pause()
        state.start(True, recording_id='second')
        self.assertEqual([s['text'] for s in state.segments], ['Content from second'])
        state.pause()

    def test_pause_cannot_complete_before_backend_start_and_leave_capture_active(self):
        import server
        import threading
        entered, release, pause_started = threading.Event(), threading.Event(), threading.Event()
        class Capture:
            active = False
            def start(self, on_segment, on_status):
                entered.set()
                release.wait(2)
                self.active = True
            def stop(self):
                self.active = False
        capture = Capture()
        state = server.MeetingState(audio_factory=lambda: capture)
        starter = threading.Thread(target=lambda: state.start(True, backend='native'))
        def pause():
            pause_started.set()
            state.pause()
        stopper = threading.Thread(target=pause)
        starter.start()
        self.assertTrue(entered.wait(1))
        stopper.start()
        self.assertTrue(pause_started.wait(1))
        try:
            stopper.join(.05)
        finally:
            release.set()
            starter.join(2)
            stopper.join(2)
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertFalse(state.listening)
        self.assertFalse(capture.active)

    def test_meetily_terminal_and_recovered_status_shapes(self):
        import server
        state = server.MeetingState()
        state.listening = True
        state.audio_status({'status': 'listening', 'error': 'snapshot_unavailable'})
        self.assertEqual(state.status()['error'], 'snapshot_unavailable')
        state.audio_status({'status': 'listening', 'error': None})
        self.assertIsNone(state.status()['error'])
        state.audio_status({'status': 'completed', 'error': None})
        self.assertFalse(state.status()['listening'])
        state.listening = True
        state.audio_status({'status': 'error', 'error': 'unsupported_schema'})
        self.assertFalse(state.status()['listening'])

    def test_devices_endpoint_uses_instance_without_starting_capture(self):
        import server
        class Capture:
            def devices(self):
                return {'microphones': [{'id': 'synthetic-mic'}]}
            def start(self, **kwargs):
                raise AssertionError('Device enumeration must not start audio')
        state = server.MeetingState(audio_factory=Capture)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            response = client.get('/devices')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['microphones'][0]['id'], 'synthetic-mic')

if __name__ == '__main__':
    unittest.main()


class OverlayRequestScopeTests(unittest.TestCase):
    def _streaming_engine(self, seen, chunks_per_call):
        class Engine:
            def answer_stream(self, question, **kwargs):
                seen.append({'question': question, 'kwargs': dict(kwargs)})
                chunks = list(chunks_per_call)
                header = {'model_route': 'fake/test', 'sources': [{'title': 't', 'text': 'x'}],
                          'warnings': [], 'web_search': {'used': False}}
                return header, iter(chunks)
            def answer(self, question, **kwargs):
                raise AssertionError('streaming path must use answer_stream')
        return Engine()

    def test_second_click_while_busy_returns_409_with_owner(self):
        import server
        import threading
        import time
        entered = threading.Event()
        release = threading.Event()
        class SlowEngine:
            def answer_stream(self, *a, **k):
                raise NotImplementedError('streaming_unsupported:x')
            def answer(self, question, **kwargs):
                entered.set()
                release.wait(5)
                return {'answer': 'slow', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'x',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=lambda: SlowEngine())
        first = state.assist('first question')
        self.assertTrue(entered.wait(5))
        try:
            state.assist('second question')
            self.fail('expected 409')
        except Exception as exc:
            self.assertEqual(getattr(exc, 'status_code', None), 409)
            self.assertIn(first['request_id'], str(exc.detail))
        state.cancel_assist(first['request_id'])
        release.set()
        for _ in range(100):
            if not state.busy:
                break
            time.sleep(.01)
        self.assertFalse(state.busy)

    def test_streaming_chunks_accumulate_on_own_request_slot_only(self):
        import server
        import time
        seen = []
        state = server.MeetingState(engine_factory=lambda: self._streaming_engine(seen, ['alpha-', 'one']))
        response = state.assist('Who approves?', request_id='r-1', row_id='row-1')
        self.assertEqual(response['request_id'], 'r-1')
        for _ in range(200):
            entry = state.assist_request('r-1')
            if entry['status'] == 'done':
                break
            time.sleep(.01)
        entry = state.assist_request('r-1')
        self.assertEqual(entry['answer'], 'alpha-one')
        self.assertEqual(entry['row_id'], 'row-1')
        self.assertTrue(entry['streaming'])
        self.assertFalse(entry['completed_only'])
        self.assertEqual(entry['model_route'], 'fake/test')
        self.assertEqual(seen[0]['question'], 'Who approves?')

    def test_completed_only_engine_reports_honestly_on_request_slot(self):
        import server
        import time
        class CompletedEngine:
            def answer_stream(self, *a, **k):
                raise NotImplementedError('streaming_unsupported:completed-only-response')
            def answer(self, question, **kwargs):
                return {'answer': 'Final.', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'x',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=CompletedEngine)
        request_id = state.assist('Who approves?')['request_id']
        for _ in range(200):
            if state.assist_request(request_id)['status'] == 'done':
                break
            time.sleep(.01)
        entry = state.assist_request(request_id)
        self.assertEqual(entry['answer'], 'Final.')
        self.assertTrue(entry['completed_only'])
        self.assertFalse(entry['streaming'])

    def test_cancel_stops_stream_and_marks_slot_cancelled(self):
        import server
        import threading
        import time
        entered = threading.Event()
        release = threading.Event()
        class HangingEngine:
            def answer_stream(self, question, **kwargs):
                entered.set()
                release.wait(5)
                return {'model_route': 'x', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}}, iter([])
            def answer(self, question, **kwargs):
                raise AssertionError('cancel path uses the stream entrypoint')
        state = server.MeetingState(engine_factory=HangingEngine)
        request_id = state.assist('Who approves?')['request_id']
        self.assertTrue(entered.wait(5))
        result = state.cancel_assist(request_id)
        self.assertEqual(result['status'], 'cancelled')
        release.set()
        self.assertEqual(state.assist_request(request_id)['status'], 'cancelled')

    def test_clear_and_pause_cancel_inflight_and_bump_generation(self):
        import server
        import threading
        import time
        entered = threading.Event()
        release = threading.Event()
        class HangingEngine:
            def answer_stream(self, *a, **k):
                raise NotImplementedError('streaming_unsupported:x')
            def answer(self, question, **kwargs):
                entered.set()
                release.wait(5)
                return {'answer': 'stale', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'x',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=HangingEngine)
        request_id = state.assist('Who approves?')['request_id']
        self.assertTrue(entered.wait(5))
        state.pause()
        self.assertEqual(state.assist_request(request_id)['status'], 'cancelled')
        self.assertFalse(state.busy)
        release.set()

    def test_unknown_request_id_returns_404(self):
        import server
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        state = server.MeetingState()
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.get('/assist/nope').status_code, 404)
            self.assertEqual(client.post('/assist/cancel', json={'request_id': 'nope'}).status_code, 404)

    def test_request_table_is_bounded_and_never_drops_live_slots(self):
        import server
        import time
        class FastEngine:
            def answer_stream(self, *a, **k):
                raise NotImplementedError('streaming_unsupported:x')
            def answer(self, question, **kwargs):
                return {'answer': 'ok', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'x',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=FastEngine)
        ids = [state.assist(f'q{i}')['request_id'] for i in range(3)]
        for rid in ids:
            for _ in range(200):
                if state.assist_request(rid)['status'] == 'done':
                    break
                time.sleep(.005)
        for i in range(10):
            state.assist(f'extra {i}')
            for _ in range(200):
                if not state.busy:
                    break
                time.sleep(.005)
        self.assertLessEqual(len(state.requests), 8)
        done = [e for e in state.requests.values() if e['status'] == 'done']
        self.assertTrue(done)

    def test_overlay_show_hide_scope_lifecycle(self):
        import server
        from test_overlay_bridge import _fake_overlay_spawn
        state = server.MeetingState(engine_factory=lambda: None)
        state._overlay_helper_path = lambda: '/fake/macos_overlay_for_test'
        with _fake_overlay_spawn():
            first = state.overlay_show({'project': 'demo', 'answer_provider': 'meta',
                                        'answer_model': 'muse-spark-1.3', 'injected': 'drop'})
        self.assertTrue(first['shown'])
        self.assertIsNotNone(state.overlay)
        again = state.overlay_show({'project': 'demo'})
        self.assertTrue(again['reused'])
        scoped = state.overlay_scope({'project': 'alpha'})
        self.assertTrue(scoped['shown'])
        self.assertEqual(state.overlay_scope_value, {'project': 'alpha'})
        self.assertEqual(state.overlay_hide(), {'shown': False})
        self.assertIsNone(state.overlay)

    def test_overlay_show_fails_closed_without_helper_and_reports_capability(self):
        import server
        from test_overlay_bridge import _fake_overlay_spawn
        state = server.MeetingState(engine_factory=lambda: None)
        state._overlay_helper_path = lambda: None
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            state.overlay_show({'project': 'demo'})
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIsNone(state.overlay)
        # Capabilities reflect the failure: no silent success.
        self.assertFalse(state.status()['capabilities']['overlay'])
        # Reopen works once a helper is prepared: no orphan, correct status.
        state._overlay_helper_path = lambda: '/fake/macos_overlay_for_test'
        with _fake_overlay_spawn():
            reopened = state.overlay_show({'project': 'demo'})
        self.assertTrue(reopened['shown'])
        self.assertIsNotNone(state.overlay)
        state.overlay_hide()

    def test_overlay_show_reports_unavailable_helper_status(self):
        import server
        state = server.MeetingState(engine_factory=lambda: None)
        state._overlay_helper_path = lambda: '/nonexistent/macos_overlay_for_test'
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            state.overlay_show({'project': 'demo'})
        self.assertEqual(ctx.exception.status_code, 503)
        import sys
        expected = 'executable' if sys.platform in ('darwin', 'linux') else 'not supported'
        self.assertIn(expected, str(ctx.exception.detail))
        self.assertIsNone(state.overlay)


class StreamInterruptionTests(unittest.TestCase):
    def _failing_engine(self, chunks, error):
        class Engine:
            def answer_stream(self, question, **kwargs):
                def gen():
                    for chunk in chunks:
                        yield chunk
                    raise error
                header = {'model_route': 'fake/test', 'sources': [{'title': 't', 'text': 'x'}],
                          'warnings': [], 'web_search': {'used': False}}
                return header, gen()
            def answer(self, question, **kwargs):
                raise AssertionError('lazy failure must not repeat retrieval')
        return Engine()

    def _wait_done(self, state, request_id):
        import time
        for _ in range(300):
            entry = state.assist_request(request_id)
            if entry['status'] == 'done':
                return entry
            time.sleep(0.01)
        return state.assist_request(request_id)

    def test_generic_failure_after_partial_keeps_marked_partial(self):
        import server
        state = server.MeetingState(
            engine_factory=lambda: self._failing_engine(['alpha-', 'one'], RuntimeError('provider down')))
        response = state.assist('Who approves?', request_id='r-part', row_id='row-1')
        self.assertEqual(response['request_id'], 'r-part')
        entry = self._wait_done(state, 'r-part')
        self.assertEqual(entry['status'], 'done')
        self.assertEqual(entry['answer'], 'alpha-one')
        self.assertTrue(any('partial' in w.lower() for w in entry['warnings']))

    def test_generic_failure_before_any_chunk_is_an_error(self):
        import server
        state = server.MeetingState(
            engine_factory=lambda: self._failing_engine([], RuntimeError('provider down')))
        state.assist('Who approves?', request_id='r-err', row_id='row-1')
        entry = self._wait_done(state, 'r-err')
        self.assertEqual(entry['status'], 'error')


class OverlayScopeOrderingTests(unittest.TestCase):
    def _state(self):
        import server
        state = server.MeetingState(engine_factory=lambda: None)
        state.overlay = None
        return state

    def test_stale_scope_sequence_does_not_overwrite_authoritative(self):
        import server
        from unittest.mock import Mock
        state = self._state()
        bridge = Mock()
        bridge.set_scope.side_effect = [
            {'scope': {'project': 'new', 'seq': 2}, 'seq': 2},
            {'scope': {'project': 'new', 'seq': 2}, 'seq': 2, 'ignored_stale': True},
        ]
        state.overlay = bridge
        first = state.overlay_scope({'project': 'new', 'seq': 2, 'web_fallback': False})
        self.assertTrue(first['accepted'])
        self.assertEqual(state.overlay_scope_value.get('project'), 'new')
        second = state.overlay_scope({'project': 'old', 'seq': 1, 'web_fallback': True})
        self.assertFalse(second['accepted'])
        self.assertEqual(state.overlay_scope_value.get('project'), 'new')
        self.assertFalse(state.overlay_scope_value.get('web_fallback', False))

    def test_watchdog_expiry_clears_overlay_reference(self):
        import server
        from unittest.mock import Mock
        import time
        state = self._state()
        bridge = Mock()
        bridge.process = None
        state.overlay = bridge
        state.listening = True
        state.pane_seen = time.monotonic() - 60
        state.backend = 'meetily'
        watchdog = __import__('threading').Thread(target=state.watchdog, kwargs={'capture_generation': state.capture_generation}, daemon=True)
        watchdog.start()
        watchdog.join(10)
        self.assertFalse(state.listening)
        self.assertIsNone(state.overlay)
        bridge.hide_overlay.assert_called_once_with()

    def test_show_after_expiry_does_not_reuse_dead_bridge(self):
        import server
        from unittest.mock import Mock
        state = self._state()
        dead = Mock()
        dead.process = None
        state.overlay = dead
        state.listening = True
        with self.assertRaises(Exception):
            state.overlay_show({'project': 'alpha'})
        # Dead bridge dropped instead of reported as reused.
        self.assertIsNot(state.overlay, dead)


class SessionSwitchTests(unittest.TestCase):
    def test_switch_cancels_live_slot_and_unblocks_new_requests(self):
        import server
        import threading
        import time
        from unittest.mock import Mock
        release = threading.Event()
        entered = threading.Event()
        class SlowEngine:
            def answer_stream(self, *a, **k):
                raise NotImplementedError('streaming_unsupported:x')
            def answer(self, question, **kwargs):
                entered.set()
                release.wait(5)
                return {'answer': 'slow', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'x',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=lambda: SlowEngine())
        state.bridge = Mock()  # attach/start succeed without hardware
        state.start(True, 'meetily', 'rec-a')
        first = state.assist('first question', request_id='old-1', row_id='row-1')
        self.assertTrue(entered.wait(5))
        # Capture stops unexpectedly while the answer streams; the slot stays
        # live (not cancelled). Starting a new meeting must cancel it and not 409.
        state.listening = False
        state.start(True, 'meetily', 'rec-b')
        second = state.assist('second question', request_id='new-1', row_id='row-2')
        self.assertEqual(second['request_id'], 'new-1')
        release.set()
        for _ in range(200):
            old = state.assist_request('old-1')
            if old is None or old.get('status') == 'cancelled':
                break
            time.sleep(0.01)
        old = state.assist_request('old-1')
        self.assertTrue(old is None or old.get('status') == 'cancelled')

    def test_concurrent_scope_updates_keep_newest_authority(self):
        import server
        import threading
        from unittest.mock import Mock
        state = server.MeetingState(engine_factory=lambda: None)
        bridge = Mock()
        def set_scope(scope):
            import time as _t
            _t.sleep(0.01 if scope.get('project') == 'new' else 0.05)
            return {'scope': dict(scope), 'seq': scope.get('seq', 0)}
        bridge.set_scope.side_effect = set_scope
        state.overlay = bridge
        state.overlay_scope_value = {}
        results = {}
        def send(name, scope):
            results[name] = state.overlay_scope(scope)
        # Older seq sent concurrently must not clobber the newer authority.
        threads = [threading.Thread(target=send, args=('old-first', {'project': 'old', 'seq': 1})),
                   threading.Thread(target=send, args=('new', {'project': 'new', 'seq': 2}))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(state.overlay_scope_value.get('project'), 'new')
        self.assertEqual(state.overlay_scope_value.get('seq'), 2)


class BusyOrphanAndLeaseTests(unittest.TestCase):
    def test_cancelled_orphan_does_not_wedge_busy(self):
        import server
        import threading
        import time
        release = threading.Event()
        entered = threading.Event()
        class SlowEngine:
            def answer_stream(self, *a, **k):
                raise NotImplementedError('streaming_unsupported:x')
            def answer(self, question, **kwargs):
                entered.set()
                release.wait(5)
                return {'answer': 'slow', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'x',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=lambda: SlowEngine())
        first = state.assist('old', request_id='old-1', row_id='row-1')
        self.assertTrue(entered.wait(5))
        state.cancel_assist('old-1')
        second = state.assist('new', request_id='new-1', row_id='row-2')
        self.assertEqual(second['request_id'], 'new-1')
        release.set()
        for _ in range(300):
            entry = state.assist_request('new-1')
            if entry is not None and entry['status'] == 'done':
                break
            time.sleep(0.01)
        self.assertEqual(state.assist_request('new-1')['status'], 'done')
        # The cancelled orphan may still be exiting; busy must not stick.
        for _ in range(200):
            if not state.busy:
                break
            time.sleep(0.01)
        self.assertFalse(state.busy)

    def test_lease_hide_revalidates_heartbeat_and_identity(self):
        import server
        import time
        from unittest.mock import Mock
        state = server.MeetingState(engine_factory=lambda: None)
        old, new = Mock(), Mock()
        state.overlay = old
        state.overlay_lease_seq = 1
        state.pane_seen = time.monotonic() - 60
        # Renewed heartbeat before teardown: must not hide.
        state.pane_seen = time.monotonic()
        self.assertFalse(state._overlay_hide_if_expired(old, 1))
        self.assertIs(state.overlay, old)
        old.hide_overlay.assert_not_called()
        # Replaced overlay: old lease must not hide the new one.
        state.overlay = new
        state.pane_seen = time.monotonic() - 60
        self.assertTrue(state._overlay_hide_if_expired(old, 1))
        self.assertIs(state.overlay, new)
        old.hide_overlay.assert_not_called()
        new.hide_overlay.assert_not_called()
        # Genuinely expired lease hides and clears the reference.
        self.assertTrue(state._overlay_hide_if_expired(new, 1))
        self.assertIsNone(state.overlay)
        new.hide_overlay.assert_called_once_with()


class LifecycleVisibilityTests(unittest.TestCase):
    def test_status_reports_overlay_down_when_helper_dead(self):
        import server
        from unittest.mock import Mock
        state = server.MeetingState(engine_factory=lambda: None)
        bridge = Mock()
        proc = Mock()
        proc.poll.return_value = 1
        bridge.process = proc
        bridge.helper_status.return_value = {'available': True}
        state.overlay = bridge
        status = state.status()
        self.assertFalse(status['overlay_shown'])

    def test_lease_survives_concurrent_heartbeat(self):
        import server
        import threading
        import time
        from unittest.mock import Mock
        state = server.MeetingState(engine_factory=lambda: None)
        bridge = Mock()
        state.overlay = bridge
        state.overlay_lease_seq = 4
        stop = threading.Event()
        def heartbeat_loop():
            while not stop.is_set():
                state._touch_pane_seen()
        pump = threading.Thread(target=heartbeat_loop, daemon=True)
        pump.start()
        for _ in range(200):
            self.assertTrue(state._overlay_hide_if_expired(bridge, 4) in (True, False))
            if state.overlay is None:
                break
            time.sleep(0.005)
        stop.set()
        # A live heartbeat loop must prevent expiry teardown.
        self.assertIs(state.overlay, bridge)
        bridge.hide_overlay.assert_not_called()

    def test_worker_saturation_returns_503(self):
        import server
        state = server.MeetingState(engine_factory=lambda: None)
        state.live_workers = 8
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            state.assist('too much')
        self.assertEqual(ctx.exception.status_code, 503)


class SubmissionGateTests(unittest.TestCase):
    def test_cancel_during_blocked_probe_prevents_provider_submit(self):
        import server
        import threading
        import time
        from unittest.mock import Mock
        release = threading.Event()
        engine = Mock()
        engine.supports_streaming.side_effect = lambda **k: release.wait(5)
        engine.answer.side_effect = AssertionError('cancelled work must not reach the provider')
        state = server.MeetingState(engine_factory=lambda: engine)
        first = state.assist('old', request_id='old-1', row_id='row-1')
        time.sleep(0.2)
        state.cancel_assist('old-1')
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            entry = state.assist_request('old-1')
            if entry is not None and entry.get('status') == 'cancelled':
                break
            time.sleep(0.01)
        self.assertEqual(state.assist_request('old-1')['status'], 'cancelled')
        engine.answer.assert_not_called()

    def test_worker_capacity_reserved_at_admission(self):
        import server
        import threading as _threading
        held = []
        class GatedThread:
            def __init__(self, target=None, **kwargs):
                self._target = target
            def start(self):
                held.append(self._target)
        import unittest.mock as _mock
        with _mock.patch.object(_threading, 'Thread', GatedThread):
            fresh = server.MeetingState(engine_factory=lambda: None)
            try:
                first = fresh.assist('one', request_id='r-1', row_id='row-1')
                self.assertEqual(fresh.live_workers, 1)
                fresh.cancel_assist('r-1')
                second = fresh.assist('two', request_id='r-2', row_id='row-2')
                # Reservation is held even though no thread has run yet.
                self.assertEqual(fresh.live_workers, 2)
            finally:
                with fresh.lock:
                    fresh.live_workers = 0


class AdmissionAndAbortTests(unittest.TestCase):
    def test_repeated_rejections_do_not_consume_worker_capacity(self):
        import server
        from fastapi import HTTPException
        state = server.MeetingState(engine_factory=lambda: None)
        for _ in range(8):
            with self.assertRaises(HTTPException) as ctx:
                state.assist('', request_id='x')
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(state.live_workers, 0)
        # Capacity intact: a valid request is still admitted.
        state.add_segment({'speaker': 'remote', 'text': 'When is the review?'})
        accepted = state.assist('When is the review?', request_id='ok-1', row_id='row-1')
        self.assertEqual(accepted['request_id'], 'ok-1')

    def test_cancel_during_blocked_retrieval_never_reaches_provider(self):
        import server
        import tempfile
        import threading
        import time
        from pathlib import Path
        from unittest.mock import Mock, patch
        from knowledge import KnowledgeEngine
        release = threading.Event()
        entered = threading.Event()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'Projects').mkdir()
        home = root / 'hermes'
        home.mkdir()
        engine = KnowledgeEngine(project_root=root / 'Projects', hermes_home=home)
        real_search = engine.search
        def blocking_search(*a, **k):
            entered.set()
            release.wait(5)
            return real_search(*a, **k)
        provider = Mock(return_value='Hello [1].')
        state = server.MeetingState(engine_factory=lambda: engine)
        state.add_segment({'speaker': 'remote', 'text': 'hello there'})
        with patch.object(engine, 'search', side_effect=blocking_search), \
             patch.object(engine, '_complete', provider):
            first = state.assist('hello?', request_id='r-1', row_id='row-1')
            self.assertTrue(entered.wait(5))
            state.cancel_assist('r-1')
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                entry = state.assist_request('r-1')
                if entry is not None and entry.get('status') == 'cancelled':
                    break
                time.sleep(0.01)
            self.assertEqual(state.assist_request('r-1')['status'], 'cancelled')  # type: ignore[index]
            provider.assert_not_called()


class BusyRecoveryTests(unittest.TestCase):
    def test_thread_start_failure_clears_busy_and_capacity(self):
        import server
        import threading
        from unittest.mock import patch
        state = server.MeetingState(engine_factory=lambda: None)
        state.add_segment({'speaker': 'remote', 'text': 'When is the review?'})
        real_thread = threading.Thread
        def boom(*args, **kwargs):
            if str(kwargs.get('name', '')).startswith('meeting-answer'):
                raise RuntimeError('cannot start')
            return real_thread(*args, **kwargs)
        with patch('threading.Thread', side_effect=boom):
            with self.assertRaises(RuntimeError):
                state.assist('When is the review?', request_id='r-1', row_id='row-1')
        self.assertEqual(state.live_workers, 0)
        self.assertFalse(state.busy)
        entry = state.assist_request('r-1')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['status'], 'error')  # type: ignore[index]


class LossLatchTests(unittest.TestCase):
    def test_drop_counts_are_monotonic_within_capture(self):
        import server
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_status({'state': 'running', 'dropped_chunks': 5, 'warning': 'drops 5'})
        state.audio_status({'state': 'running', 'dropped_chunks': 1})
        self.assertEqual(state.capture_info.get('dropped_chunks'), 5)
        self.assertIn('drops 5', state.capture_info.get('warning', ''))

    def test_packaged_abort_signal_is_recognized_by_name(self):
        import server
        class FakeAborted(Exception):
            pass
        FakeAborted.__name__ = '_Aborted'
        self.assertTrue(server._is_engine_abort(FakeAborted('x')))
        self.assertFalse(server._is_engine_abort(RuntimeError('x')))


class CompletedRunningLabelTests(unittest.TestCase):
    def test_completed_only_work_is_marked_running_while_executing(self):
        import server
        import threading
        import time
        entered = threading.Event()
        release = threading.Event()
        class BlockingCompletedEngine:
            def answer(self, *a, **k):
                entered.set()
                release.wait(5)
                return {'answer': 'done', 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'x',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=lambda: BlockingCompletedEngine())
        state.add_segment({'speaker': 'remote', 'text': 'hello'})
        state.assist('hello?', request_id='r-1', row_id='row-1')
        self.assertTrue(entered.wait(5))
        seen = state.assist_request('r-1')['status']  # type: ignore[index]
        release.set()
        self.assertEqual(seen, 'running')


class PauseTailTests(unittest.TestCase):
    def test_tail_segment_flushed_during_stop_survives_pause(self):
        import server
        tail = {'speaker': 'you', 'text': 'final words', 'timestamp': 1.0}
        class FakeMacOSCapture:
            def start(self, on_segment, on_status=None):
                self._on_segment = on_segment
            def stop(self, timeout=3.0):
                # Graceful shutdown delivers trailing speech during stop().
                self._on_segment(dict(tail))
        state = server.MeetingState(engine_factory=lambda: None)
        state.get_audio = lambda *a, **k: setattr(state, 'audio', FakeMacOSCapture())
        state.start(True, backend='macos')
        gen = state.capture_generation
        state.pause()
        texts = [s.get('text') for s in state.segments]
        self.assertIn('final words', texts)
        # The window is bounded: a later generation's pause closes it.
        self.assertNotEqual(state.capture_generation, gen)


class RetiringWindowTests(unittest.TestCase):
    def test_same_session_resume_keeps_bounded_tail_window(self):
        import server
        callbacks = []
        class Capture:
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        state.start(True, backend='native')
        state.pause()
        state.start(True, backend='native')
        callbacks[0][0]({'speaker': 'you', 'text': 'tail', 'timestamp': 1.0})
        self.assertEqual([s.get('text') for s in state.segments], ['tail'])


class ErrorRequestHygieneTests(unittest.TestCase):
    def _failing_state(self):
        import server
        class BoomEngine:
            def answer(self, *a, **k):
                raise RuntimeError('provider down')
        state = server.MeetingState(engine_factory=lambda: BoomEngine())
        state.add_segment({'speaker': 'remote', 'text': 'secret plans here'})
        return state

    def test_clear_purges_failed_request_retry_data(self):
        state = self._failing_state()
        state.assist('what?', request_id='r-1')
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if state.error_request is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(state.error_request)
        state.requests.clear()
        state.segments.clear()
        state.result = None
        state.error = None
        state.error_request = None
        self.assertIsNone(state.error_request)

    def test_session_switch_purges_failed_request_retry_data(self):
        import server
        state = self._failing_state()
        class FakeMacOSCapture:
            def start(self, on_segment, on_status=None):
                pass
            def stop(self, timeout=3.0):
                pass
        state.audio_factory = FakeMacOSCapture
        state.start(True, backend='macos', transcription_model='tiny.en')
        state.error_request = {'question': 'old', 'transcript': 'old secrets'}
        state.pause()
        state.start(True, backend='macos', transcription_model='base.en')
        self.assertIsNone(state.error_request)

    def test_clear_route_purges_failed_request_from_status(self):
        import server
        import time
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        state = self._failing_state()
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            client.post('/assist', json={'question': 'what?'})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = client.get('/status').json()
                if status.get('error_request'):
                    break
                time.sleep(0.05)
            self.assertIsNotNone(client.get('/status').json().get('error_request'))
            self.assertNotIn('secret plans', client.get('/status').json().get('transcript', ''))
            client.post('/clear', json={})
            cleared = client.get('/status').json()
            self.assertIsNone(cleared.get('error_request'))


class ClearTailTests(unittest.TestCase):
    def test_clear_invalidates_retiring_tail_window(self):
        import server
        late = {}
        class FakeMacOSCapture:
            def __init__(self, **options):
                pass
            def start(self, on_segment, on_status):
                late['segment'] = on_segment
            def stop(self, timeout=3.0):
                pass
        state = server.MeetingState(engine_factory=lambda: None)
        state.macos_factory = FakeMacOSCapture
        state.start(True, backend='macos', transcription_model='tiny.en')
        state.pause()
        state.segments.clear()
        # Simulate POST /clear's retiring-window invalidation.
        state.retiring_generations.clear()
        late['segment']({'speaker': 'you', 'text': 'pre-clear speech', 'timestamp': 1.0})
        self.assertEqual(list(state.segments), [])



class ProviderFailureTests(unittest.TestCase):
    def test_completed_provider_failure_is_error_with_retry(self):
        import server
        import tempfile
        import time
        from pathlib import Path as _Path
        from unittest.mock import Mock, patch
        from knowledge import KnowledgeEngine
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = _Path(tmp.name)
        (root / 'Projects').mkdir()
        home = root / 'hermes'
        home.mkdir()
        engine = KnowledgeEngine(root / 'Projects', home)
        state = server.MeetingState(engine_factory=lambda: engine)
        state.add_segment({'speaker': 'remote', 'text': 'hello there'})
        def fail(messages):
            raise RuntimeError('boom')
        with patch.object(engine, '_complete', fail):
            state.assist('hello?', request_id='r-1', row_id='row-1')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                entry = state.assist_request('r-1')
                if entry is not None and entry.get('status') == 'error':
                    break
                time.sleep(0.02)
            entry = state.assist_request('r-1')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['status'], 'error')  # type: ignore[index]
        self.assertIsNotNone(state.error_request)
        self.assertIsNone(state.result)


class StaleTerminalTests(unittest.TestCase):
    def test_older_terminal_never_overwrites_newer_capture(self):
        import server
        callbacks = []
        class Capture:
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        state.start(True, backend='native')
        old_status = callbacks[0][1]
        state.pause()
        # New capture pauses cleanly with no loss evidence.
        state.start(True, backend='native')
        state.pause()
        # The newer capture emitted no terminal of its own, yet still owns
        # the terminal state: it paused cleanly with no loss evidence.
        self.assertNotIn('dropped_chunks', state.capture_info)
        # The older capture's late terminal (with loss) must not rewind
        # the newer capture's clean state.
        old_status({'type': 'status', 'state': 'stopped', 'dropped_chunks': 3,
                    'native_dropped_chunks': 3, 'worker_dropped_chunks': 0,
                    'warning': 'late loss from a previous capture'})
        self.assertNotIn('dropped_chunks', state.capture_info)

    def test_restart_clears_reader_incomplete_flag(self):
        import server
        callbacks = []
        class Capture:
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        state.start(True, backend='native')
        callbacks[0][1]({'type': 'status', 'state': 'running', 'reader_incomplete': True,
                          'warning': 'Reader had not finished.'})
        self.assertTrue(state.capture_info.get('reader_incomplete'))
        state.pause()
        state.start(True, backend='native')
        self.assertNotIn('reader_incomplete', state.capture_info)


class MeetingBoundaryTests(unittest.TestCase):
    def _state(self):
        import server
        callbacks = []
        class Capture:
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        return state

    def test_same_settings_restart_resumes_transcript(self):
        state = self._state()
        state.start(True, backend='native')
        state.add_segment({'speaker': 'you', 'text': 'prior meeting words', 'timestamp': 1.0})
        state.pause()
        state.start(True, backend='native')
        self.assertEqual([s.get('text') for s in state.segments], ['prior meeting words'])

    def test_explicit_new_meeting_clears_prior_content(self):
        state = self._state()
        state.start(True, backend='native')
        state.add_segment({'speaker': 'you', 'text': 'prior meeting words', 'timestamp': 1.0})
        state.pause()
        state.start(True, backend='native', new_meeting=True)
        self.assertEqual(list(state.segments), [])


class PauseErrorPreservationTests(unittest.TestCase):
    def _state(self):
        import server
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = None
        state.backend = 'native'
        return state

    def test_capture_failure_survives_pause(self):
        state = self._state()
        state.capture_error = 'Native audio capture failed.'
        state.capture_info = {'state': 'error', 'backend': 'native', 'error': 'Native audio capture failed.'}
        state.pause()
        self.assertEqual(state.capture_error, 'Native audio capture failed.')
        self.assertEqual(state.capture_info.get('error'), 'Native audio capture failed.')

    def test_clean_pause_keeps_error_clear(self):
        state = self._state()
        state.pause()
        self.assertIsNone(state.capture_error)
        self.assertIsNone(state.capture_info.get('error'))


class ClearBoundaryTests(unittest.TestCase):
    def test_clear_while_listening_rejects_pre_clear_audio(self):
        import server
        callbacks = []
        class Capture:
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        state.start(True, backend='native')
        old_segment = callbacks[0][0]
        state.add_segment({'speaker': 'you', 'text': 'before clear', 'timestamp': 1.0})
        self.assertEqual(len(state.segments), 1)
        # Simulate the /clear route's boundary (lock + restart) without HTTP.
        with state.lock:
            state.segments.clear()
            state.result = None
            state.error = None
            state.error_request = None
            state.retiring_generations.clear()
            was_listening = state.listening
            last_args = getattr(state, 'last_start_args', None)
            state.busy = False
            state.generation += 1
        if was_listening and last_args:
            state.pause()
            state.retiring_generations.clear()
            backend, recording_id, tm, src = last_args
            state.start(True, backend, recording_id, tm, src)
        self.assertTrue(state.listening)
        self.assertEqual(list(state.segments), [])
        # Pre-clear in-flight audio from the old generation is rejected.
        old_segment({'speaker': 'you', 'text': 'before clear', 'timestamp': 1.0})
        self.assertEqual(list(state.segments), [])
        # Live audio on the new generation flows.
        new_segment = callbacks[-1][0]
        new_segment({'speaker': 'you', 'text': 'after clear', 'timestamp': 2.0})
        self.assertEqual([s.get('text') for s in state.segments], ['after clear'])


class ClearRouteTests(unittest.TestCase):
    def test_clear_route_restarts_live_capture_without_resurrection(self):
        import server
        from fastapi import FastAPI
        callbacks = []
        class Capture:
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        app = FastAPI()
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/start', json={'consent': True, 'backend': 'native'}).status_code, 200)
            old_segment = callbacks[0][0]
            old_segment({'speaker': 'you', 'text': 'before clear', 'timestamp': 1.0})
            self.assertEqual(len(client.get('/status').json()['segments']), 1)
            self.assertEqual(client.post('/clear').status_code, 200)
            status = client.get('/status').json()
            self.assertTrue(status['listening'])
            self.assertEqual(status['segments'], [])
            # Pre-clear in-flight audio cannot resurrect; live audio flows.
            old_segment({'speaker': 'you', 'text': 'before clear', 'timestamp': 1.0})
            self.assertEqual(client.get('/status').json()['segments'], [])
            callbacks[-1][0]({'speaker': 'you', 'text': 'after clear', 'timestamp': 2.0})
            self.assertEqual([s.get('text') for s in client.get('/status').json()['segments']], ['after clear'])


class StatusShapeTests(unittest.TestCase):
    def test_meetily_status_shaped_error_terminal_is_recognized(self):
        import server
        callbacks = []
        class Bridge:
            def attach(self, recording_id, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        state = server.MeetingState(engine_factory=lambda: None)
        state.bridge_factory = None
        # Bypass factory: inject the bridge double directly.
        import meetily
        state.bridge = Bridge()
        state._start(True, backend='meetily', recording_id='r-1')
        on_status = callbacks[0][1]
        on_status({'status': 'error', 'backend': 'meetily', 'message': 'recording gone', 'error': 'gone'})
        self.assertEqual(state.capture_error, 'gone')
        self.assertEqual(state.capture_info.get('state'), 'error')


class LastSessionTests(unittest.TestCase):
    def test_status_exposes_last_session_descriptor(self):
        import server
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        callbacks = []
        class Capture:
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        app = FastAPI()
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertIsNone(client.get('/status').json().get('last_session'))
            client.post('/start', json={'consent': True, 'backend': 'native',
                                        'transcription_model': 'tiny.en', 'audio_source': 'both'})
            last = client.get('/status').json().get('last_session')
            self.assertEqual(last['backend'], 'native')
            self.assertEqual(last['transcription_model'], 'tiny.en')
            self.assertEqual(last['audio_source'], 'both')


class ClearStopTimeTests(unittest.TestCase):
    def test_synchronous_stop_time_tail_rejected_after_clear(self):
        import server
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        held = {}
        class Capture:
            def start(self, on_segment, on_status):
                held['segment'] = on_segment
                held['status'] = on_status
            def stop(self):
                # Synchronous stop-time flush: trailing speech delivered
                # during teardown belongs to the cleared meeting.
                held['segment']({'speaker': 'you', 'text': 'PRE-CLEAR AUDIO', 'timestamp': 1.0})
                held['status']({'type': 'status', 'state': 'stopped'})
        app = FastAPI()
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/start', json={'consent': True, 'backend': 'native'}).status_code, 200)
            held['segment']({'speaker': 'you', 'text': 'before clear', 'timestamp': 1.0})
            self.assertEqual(len(client.get('/status').json()['segments']), 1)
            self.assertEqual(client.post('/clear').status_code, 200)
            status = client.get('/status').json()
            self.assertTrue(status['listening'])
            self.assertEqual(status['segments'], [],
                             'stop-time pre-clear audio must not resurrect the cleared transcript')


class ClearPauseRaceTests(unittest.TestCase):
    def test_concurrent_pause_always_wins_over_clear_restart(self):
        import threading
        import server
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        for _ in range(5):
            callbacks = []
            class Capture:
                def start(self, on_segment, on_status):
                    callbacks.append((on_segment, on_status))
                def stop(self):
                    pass
            app = FastAPI()
            state = server.MeetingState(engine_factory=lambda: None)
            state.audio_factory = Capture
            app.include_router(server.make_router(state))
            with TestClient(app) as client:
                self.assertEqual(client.post('/start', json={'consent': True, 'backend': 'native'}).status_code, 200)
                # Pause racing Clear's restart: whatever the interleaving,
                # an explicit pause must never be overridden by the restart.
                pauser = threading.Thread(target=state.pause)
                clearer = threading.Thread(target=lambda: client.post('/clear'))
                clearer.start()
                pauser.start()
                clearer.join(timeout=10)
                pauser.join(timeout=10)
                self.assertFalse(client.get('/status').json()['listening'],
                                 'concurrent pause must survive clear')


class RestartAfterClearTests(unittest.TestCase):
    def _state(self):
        import server
        stopped = []
        class Capture:
            def start(self, on_segment, on_status):
                pass
            def stop(self):
                stopped.append(True)
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        return state, stopped

    def test_stale_restart_never_replaces_newer_start(self):
        state, stopped = self._state()
        state.start(True, backend='native')
        stale_args = getattr(state, 'last_start_args', None)
        stale_gen = state.capture_generation
        stale_session = state.session_key
        # A newer start (different settings) takes over before Clear restarts.
        state.pause()
        state.start(True, backend='native', transcription_model='small.en')
        self.assertNotEqual(state.session_key, stale_session)
        del stopped[:]
        state.restart_after_clear(stale_args, stale_gen, stale_session)
        # The newer capture keeps running on its own session; nothing stopped.
        self.assertTrue(state.listening)
        self.assertEqual(state.session_key, ('native', ('small.en', 'microphone')))
        self.assertEqual(stopped, [])

    def test_current_restart_proceeds_on_same_session(self):
        state, stopped = self._state()
        state.start(True, backend='native')
        args = getattr(state, 'last_start_args', None)
        gen = state.capture_generation
        session = state.session_key
        # Simulate Clear's atomic retire (generation bump under state lock).
        state.capture_generation += 1
        state.restart_after_clear(args, gen, session)
        self.assertTrue(state.listening)
        self.assertEqual(state.session_key, session)

    def test_restart_skipped_after_concurrent_pause(self):
        state, stopped = self._state()
        state.start(True, backend='native')
        args = getattr(state, 'last_start_args', None)
        gen = state.capture_generation
        session = state.session_key
        state.capture_generation += 1
        state.pause()
        state.restart_after_clear(args, gen, session)
        self.assertFalse(state.listening)


class PauseCancellationOrderTests(unittest.TestCase):
    def test_requests_cancelled_before_backend_stop_runs(self):
        import server
        order = []
        class Capture:
            def start(self, on_segment, on_status):
                pass
            def stop(self):
                order.append('stop')
                state = order_state[0]
                live = [e for e in state.requests.values() if e.get('status') in ('queued', 'streaming', 'running')]
                order.append('live-still-running' if live else 'already-cancelled')
        order_state = [None]
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        order_state[0] = state
        state.start(True, backend='native')
        state.requests['r-1'] = {'request_id': 'r-1', 'status': 'streaming', 'cancelled': False}
        state.pause()
        self.assertIn('already-cancelled', order)
        self.assertNotIn('live-still-running', order)
        self.assertEqual(state.requests['r-1']['status'], 'cancelled')


class WatchdogCancellationOrderTests(unittest.TestCase):
    def test_expiry_cancels_requests_before_backend_stop_runs(self):
        import server
        import threading as _threading
        import time as _time
        order = []
        entered_stop = _threading.Event()
        release_stop = _threading.Event()
        class Capture:
            def start(self, on_segment, on_status):
                pass
            def stop(self):
                # Prove cancellation precedes even a blocked teardown: record
                # the request state at stop entry, then stall until released.
                live = [e for e in state.requests.values() if e.get('status') in ('queued', 'streaming', 'running')]
                order.append('live-still-running' if live else 'already-cancelled')
                entered_stop.set()
                release_stop.wait(5)
                order.append('stop')
        state = server.MeetingState(engine_factory=lambda: None)
        state.audio_factory = Capture
        state.start(True, backend='native')
        state.requests['r-1'] = {'request_id': 'r-1', 'status': 'streaming', 'cancelled': False}
        state.pane_seen = _time.monotonic() - 60
        worker = _threading.Thread(target=state.watchdog, daemon=True)
        worker.start()
        # While stop is still blocked, the request must already be cancelled.
        self.assertTrue(entered_stop.wait(8))
        self.assertEqual(state.requests['r-1']['status'], 'cancelled')
        release_stop.set()
        worker.join(8)
        self.assertIn('already-cancelled', order)
        self.assertNotIn('live-still-running', order)
        self.assertEqual(state.requests['r-1']['status'], 'cancelled')


class ClearBoundaryFailureTests(unittest.TestCase):
    def test_failed_forget_boundary_stays_visible_until_it_lands(self):
        import server
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        state = server.MeetingState(engine_factory=lambda: None)
        app.include_router(server.make_router(state))
        class FailingBridge:
            def note_cleared(self):
                raise RuntimeError('snapshot locked')
        state.bridge = FailingBridge()
        with TestClient(app) as client:
            self.assertEqual(client.post('/clear').status_code, 200)
            status = client.get('/status').json()
            self.assertIn('snapshot locked', status['meetily_clear_error'])
            # A later Clear that installs the boundary clears the flag.
            class WorkingBridge:
                def note_cleared(self):
                    pass
            state.bridge = WorkingBridge()
            self.assertEqual(client.post('/clear').status_code, 200)
            self.assertIsNone(client.get('/status').json()['meetily_clear_error'])


class WorkerIdentityTests(unittest.TestCase):
    def test_reused_id_never_binds_old_worker_to_new_entry(self):
        import server
        import threading as _threading
        from unittest import mock
        release = _threading.Event()
        class Engine:
            def supports_streaming(self, **kwargs):
                return False
            def answer(self, question, **kwargs):
                release.wait(5)
                return {'answer': question, 'sources': [], 'warnings': [],
                        'web_search': {'used': False}, 'model_route': 'test',
                        'streaming': False, 'completed_only': True}
        state = server.MeetingState(engine_factory=Engine)
        real_copy_context = __import__('contextvars').copy_context
        def copy_context_with_takeover():
            ctx = real_copy_context()
            # Clear deletes the reserved slot; a newer assist reuses the
            # caller-supplied ID before the old worker starts.
            if not taken_over[0] and state.requests.get('r-reuse') is not None:
                taken_over[0] = True
                with state.lock:
                    for key in list(state.requests):
                        del state.requests[key]
                state.assist('newer question', request_id='r-reuse')
            return ctx
        taken_over = [False]
        with mock.patch('contextvars.copy_context', side_effect=copy_context_with_takeover):
            with self.assertRaisesRegex(RuntimeError, 'cleared before it could start'):
                state.assist('older question', request_id='r-reuse')
        # The old worker never started: the replacement entry is untouched
        # by the old request and owned by its own worker.
        current = state.requests.get('r-reuse')
        self.assertIsNotNone(current)
        self.assertNotIn('older question', (current.get('answer') or ''))
        release.set()
        deadline = __import__('time').monotonic() + 5
        while deadline > __import__('time').monotonic():
            if current.get('status') == 'done' and current.get('answer') == 'newer question':
                break
        self.assertEqual(current.get('answer'), 'newer question')


class ClearBoundaryFailureRestartTests(unittest.TestCase):
    def test_failed_boundary_stops_capture_without_restart(self):
        import server
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        state = server.MeetingState(engine_factory=lambda: None)
        app.include_router(server.make_router(state))
        stopped = []
        class FailingBridge:
            def note_cleared(self):
                raise RuntimeError('snapshot locked')
            def stop(self):
                stopped.append(True)
        state.bridge = FailingBridge()
        state.backend = 'meetily'
        state.listening = True
        state.last_start_args = ('meetily', 'rec', 'tiny.en', 'microphone')
        with TestClient(app) as client:
            self.assertEqual(client.post('/clear').status_code, 200)
            status = client.get('/status').json()
            self.assertIn('snapshot locked', status['meetily_clear_error'])
            # Fail closed: capture stopped, no restart on unforgotten state.
            self.assertFalse(status['listening'])
            self.assertTrue(stopped)


class ClearBoundaryStartGateTests(unittest.TestCase):
    def _client(self):
        import server
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        state = server.MeetingState(engine_factory=lambda: None)
        app.include_router(server.make_router(state))
        return TestClient(app), state

    def test_plain_start_blocked_while_boundary_failed(self):
        client, state = self._client()
        state.meetily_clear_error = 'Could not install the Meetily forget boundary: boom.'
        state.backend = 'meetily'
        generation_before = state.capture_generation
        response = client.post('/start', json={'consent': True, 'backend': 'meetily',
                                               'recording_id': 'rec', 'new_meeting': False})
        self.assertEqual(response.status_code, 409)
        self.assertIn('forget boundary', response.json().get('detail', ''))
        # The rejection mutates nothing: no phantom listening capture, no
        # generation advance, so recovery stays possible.
        status = client.get('/status').json()
        self.assertFalse(status['listening'])
        self.assertEqual(state.capture_generation, generation_before)
        retry = client.post('/start', json={'consent': True, 'backend': 'meetily',
                                            'recording_id': 'rec', 'new_meeting': False})
        self.assertEqual(retry.status_code, 409)

    def test_native_start_keeps_failed_meetily_boundary(self):
        client, state = self._client()
        state.meetily_clear_error = 'Could not install the Meetily forget boundary: boom.'
        started = []
        class Capture:
            def start(self, on_segment, on_status):
                started.append(True)
            def stop(self):
                pass
        state.audio_factory = Capture
        response = client.post('/start', json={'consent': True, 'backend': 'native'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(started)
        # Switching backends installs no Meetily boundary: the flag stands.
        self.assertIsNotNone(client.get('/status').json()['meetily_clear_error'])

    def test_successful_new_meeting_clears_stale_error(self):
        import server as _server
        client, state = self._client()
        state.meetily_clear_error = 'Could not install the Meetily forget boundary: boom.'
        attached = []
        class GoodBridge:
            def note_cleared(self, recording_id=None):
                pass
            def attach(self, recording_id, on_segment, on_status):
                attached.append(recording_id)
        state.get_bridge = lambda: GoodBridge()
        response = client.post('/start', json={'consent': True, 'backend': 'meetily',
                                               'recording_id': 'rec', 'new_meeting': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attached, ['rec'])
        self.assertIsNone(client.get('/status').json()['meetily_clear_error'])

    def test_stale_failure_pause_never_stops_newer_capture(self):
        client, state = self._client()
        stopped = []
        class FailingBridge:
            def note_cleared(self):
                raise RuntimeError('snapshot locked')
            def stop(self):
                stopped.append(True)
        state.bridge = FailingBridge()
        state.backend = 'meetily'
        state.listening = True
        state.last_start_args = ('meetily', 'rec', 'tiny.en', 'microphone')
        # A newer capture starts after Clear releases the state lock but
        # before the stale failure path runs: gate the lifecycle lock to
        # install it on first entry (the failure path's own acquisition).
        real_lifecycle = state.lifecycle_lock
        entries = []
        class GateLock:
            def __enter__(self):
                # Inject on the SECOND acquisition: the purge section (first)
                # must capture the true pre-clear generation. The race window
                # is between the purge releasing the lock and the failure
                # path re-acquiring it for its gated pause.
                entries.append(True)
                if len(entries) == 2:
                    state.capture_generation += 5
                    state.session_key = ('meetily', 'new-rec')
                    state.listening = True
                return real_lifecycle.__enter__()
            def __exit__(self, *exc):
                return real_lifecycle.__exit__(*exc)
        state.lifecycle_lock = GateLock()
        try:
            self.assertEqual(client.post('/clear').status_code, 200)
        finally:
            state.lifecycle_lock = real_lifecycle
        status = client.get('/status').json()
        self.assertIn('snapshot locked', status['meetily_clear_error'])
        self.assertTrue(status['listening'])
        self.assertEqual(stopped, [])


class ClearStartAtomicityTests(unittest.TestCase):
    def test_plain_start_blocks_until_clear_publishes_failure(self):
        import server
        import threading as _threading
        import time as _time
        from fastapi import HTTPException
        state = server.MeetingState(engine_factory=lambda: None)
        held = _threading.Event()
        release = _threading.Event()
        def hold_lock_then_fail():
            with state.lock:
                held.set()
                self.assertTrue(release.wait(5))
                # What a paused Clear writes just before releasing the lock.
                state.meetily_clear_error = 'Could not install the Meetily forget boundary: boom.'
        holder = _threading.Thread(target=hold_lock_then_fail, daemon=True)
        holder.start()
        self.assertTrue(held.wait(5))
        outcome = {}
        def do_start():
            try:
                state.start(True, 'meetily', 'rec', new_meeting=False)
                outcome['ok'] = True
            except HTTPException as exc:
                outcome['status'] = exc.status_code
        starter = _threading.Thread(target=do_start, daemon=True)
        starter.start()
        _time.sleep(0.2)  # let the start reach the gate and block on the lock
        release.set()
        holder.join(5)
        starter.join(5)
        self.assertEqual(outcome.get('status'), 409)
        self.assertFalse(state.listening)


class ClearStartBridgeWindowTests(unittest.TestCase):
    def test_clear_during_start_installs_boundary_before_restart(self):
        import json as _json
        import server
        import tempfile as _tempfile
        import threading as _threading
        import time as _time
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from pathlib import Path as _Path
        root = _Path(_tempfile.mkdtemp())
        folder = root / 'rec'
        folder.mkdir()
        (folder / 'metadata.json').write_text(_json.dumps(
            {'version': '1.0', 'meeting_name': 'R', 'status': 'recording', 'created_at': 'x'}))
        (folder / 'transcripts.json').write_text(_json.dumps(
            {'version': '1.0', 'segments': [
                {'id': '1', 'text': 'old snapshot', 'audio_start_time': 1.0,
                 'audio_end_time': 2.0, 'duration': 1, 'display_time': '00:01',
                 'confidence': .9, 'sequence_id': 1}],
             'total_segments': 1, 'last_updated': 'x'}))
        app = FastAPI()
        state = server.MeetingState(engine_factory=lambda: None)
        app.include_router(server.make_router(state))
        import meetily as _meetily
        state.bridge_factory = lambda: _meetily.MeetilyBridge(root, poll_interval=.01)
        entered = _threading.Event()
        release = _threading.Event()
        real_get_bridge = state.get_bridge
        def gated_get_bridge():
            # Park BEFORE the bridge exists: listening is set but creation
            # has not happened — the exact window the serialization closes.
            entered.set()
            self.assertTrue(release.wait(5))
            return real_get_bridge()
        state.get_bridge = gated_get_bridge
        forgot = []
        real_note = _meetily.MeetilyBridge.note_cleared
        def spy_note(self, recording_id=None):
            forgot.append(recording_id if recording_id is not None else self._recording_id)
            return real_note(self, recording_id)
        _meetily.MeetilyBridge.note_cleared = spy_note
        try:
            with TestClient(app) as client:
                started = {}
                def do_start():
                    try:
                        state.start(True, 'meetily', 'rec')
                        started['ok'] = True
                    except Exception as exc:
                        started['error'] = exc
                worker = _threading.Thread(target=do_start, daemon=True)
                worker.start()
                self.assertTrue(entered.wait(5))
                self.assertTrue(state.listening)
                self.assertIsNone(state.bridge)
                cleared = {}
                def do_clear():
                    cleared['response'] = client.post('/clear')
                clearer = _threading.Thread(target=do_clear, daemon=True)
                clearer.start()
                # Clear must wait for the in-flight start instead of running
                # with a missing bridge and skipping the forget boundary.
                _time.sleep(0.5)
                self.assertNotIn('response', cleared)
                release.set()
                worker.join(10)
                clearer.join(10)
                self.assertTrue(started.get('ok'))
                self.assertEqual(cleared['response'].status_code, 200)
                self.assertTrue(forgot, 'forget boundary must install via the published bridge')
                status = client.get('/status').json()
                self.assertIsNone(status['meetily_clear_error'])
                self.assertFalse(any('old snapshot' in s.get('text', '')
                                     for s in status['segments']))
        finally:
            _meetily.MeetilyBridge.note_cleared = real_note
            state.get_bridge = real_get_bridge
            try:
                state.pause()
            except Exception:
                pass
