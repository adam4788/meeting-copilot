"""Independent capture/API contracts; no devices or network."""
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class IndependentServerTests(unittest.TestCase):
    def test_assist_forwards_model_selection_without_global_state(self):
        import server
        import threading
        seen = []
        done = threading.Event()
        class Engine:
            def answer(self, question, **options):
                seen.append(options)
                done.set()
                return {'answer': 'synthetic', 'sources': []}
        state = server.MeetingState(engine_factory=Engine)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            result = client.post('/assist', json={'question': 'Public test', 'answer_provider': 'openai-codex', 'answer_model': 'gpt-test'})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(done.wait(2))
            self.assertEqual(seen[0].get('answer_provider'), 'openai-codex')
            self.assertEqual(seen[0].get('answer_model'), 'gpt-test')
            done.clear()
            other = server.MeetingState(engine_factory=Engine)
            other.assist('Public test', answer_provider='', answer_model='')
            self.assertTrue(done.wait(2))
            self.assertIn('answer_provider', seen[-1])
            self.assertEqual(seen[-1]['answer_provider'], '')

    def test_stopped_capture_callbacks_cannot_leak_into_new_meeting(self):
        import server
        callbacks = []
        class Capture:
            def __init__(self, **options):
                pass
            def start(self, on_segment, on_status):
                callbacks.append((on_segment, on_status))
            def stop(self):
                pass
        state = server.MeetingState(macos_factory=Capture)
        state.start(True, 'macos')
        state.pause()
        state.start(True, 'macos', transcription_model='tiny.en')
        callbacks[0][0]({'text': 'Late old meeting text'})
        callbacks[0][1]({'state': 'error', 'error': 'stale'})
        self.assertEqual(state.status()['segments'], [])
        self.assertTrue(state.status()['listening'])
        self.assertIsNone(state.status()['error'])
        callbacks[1][0]({'text': 'Current meeting'})
        self.assertEqual(len(state.status()['segments']), 1)
        state.pause()

    def test_device_listing_does_not_replace_active_capture(self):
        import server
        class Capture:
            def __init__(self, **options):
                self.options = options
            def start(self, **callbacks):
                pass
            def stop(self):
                pass
            def devices(self):
                return {'backend': 'macos', 'capture_started': False}
        state = server.MeetingState(macos_factory=Capture)
        state.start(True, 'macos', transcription_model='tiny.en')
        active = state.audio
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.get('/devices?backend=macos').status_code, 200)
            self.assertIs(state.audio, active)
        state.pause()

    def test_macos_capture_requires_consent_and_passes_local_options(self):
        import server
        made = []
        class Capture:
            def __init__(self, **options):
                made.append(options)
                self.active = False
            def start(self, on_segment, on_status):
                self.active = True
                on_segment({'speaker': 'you', 'text': 'Synthetic test'})
            def stop(self):
                self.active = False
        state = server.MeetingState(macos_factory=Capture)
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            body = {'backend': 'macos', 'transcription_model': 'tiny.en', 'audio_source': 'microphone'}
            self.assertEqual(client.post('/start', json=body).status_code, 400)
            self.assertEqual(made, [])
            self.assertEqual(client.post('/start', json={**body, 'consent': True}).status_code, 200)
            self.assertEqual(made, [{'model_size': 'tiny.en', 'source': 'microphone'}])
            self.assertTrue(state.audio.active)
            self.assertEqual(client.get('/status').json()['segments'][0]['text'], 'Synthetic test')
            self.assertEqual(client.post('/pause').status_code, 200)
            self.assertFalse(state.audio.active)
            self.assertFalse(state.status()['listening'])
            self.assertEqual(state.status()['capture'].get('state'), 'stopped')
            self.assertEqual(client.post('/start', json={**body, 'consent': True, 'transcription_model': '../weights'}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
