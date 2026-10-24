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

    def test_paused_capture_callbacks_cannot_restore_context(self):
        import server
        class Capture:
            def start(self, on_segment, on_status):
                self.segment, self.status = on_segment, on_status
            def stop(self):
                pass  # A bounded stop may return before a worker finishes.
        capture = Capture()
        state = server.MeetingState(audio_factory=lambda: capture)
        state.start(True, backend='native')
        state.pause()
        capture.segment({'text': 'Late text from detached capture'})
        capture.status({'state': 'error', 'error': 'Late capture failure'})
        self.assertEqual(list(state.segments), [])
        self.assertIsNone(state.status()['error'])
        self.assertEqual(state.status()['capture']['message'], 'Paused')

    def test_previous_capture_callbacks_cannot_change_new_meeting(self):
        import server
        class Bridge:
            def __init__(self):
                self.callbacks = []
            def attach(self, recording_id, on_segment, on_status):
                self.callbacks.append((on_segment, on_status))
                on_segment({'text': recording_id})
            def stop(self):
                pass
        bridge = Bridge()
        state = server.MeetingState(bridge_factory=lambda: bridge)
        try:
            state.start(True, recording_id='first')
            old_segment, old_status = bridge.callbacks[0]
            state.pause()
            state.start(True, recording_id='second')
            old_segment({'text': 'Late first meeting text'})
            old_status({'status': 'completed', 'error': 'Late first meeting error'})
            self.assertEqual([s['text'] for s in state.segments], ['second'])
            self.assertTrue(state.listening)
            self.assertIsNone(state.status()['error'])
            bridge.callbacks[1][0]({'text': 'Current meeting text'})
            self.assertEqual(state.segments[-1]['text'], 'Current meeting text')
        finally:
            state.pause()

    def test_clear_keeps_current_capture_callbacks_active(self):
        import server
        class Bridge:
            def attach(self, recording_id, on_segment, on_status):
                self.segment = on_segment
            def stop(self):
                pass
        bridge = Bridge()
        state = server.MeetingState(bridge_factory=lambda: bridge)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            try:
                state.start(True, recording_id='first')
                bridge.segment({'text': 'Before clear'})
                self.assertEqual(client.post('/clear').status_code, 200)
                bridge.segment({'text': 'After clear'})
                self.assertEqual([s['text'] for s in state.segments], ['After clear'])
                self.assertTrue(state.listening)
            finally:
                state.pause()

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
