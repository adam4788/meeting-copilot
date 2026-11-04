"""Offline JSON-lines bridge: real router/state, synthetic audio and watchdog clock.

Used by test_desktop.mjs; no listening socket, credentials, or hardware.
"""
import json
import sys
import time as real_time
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import server


class TickComplete(Exception):
    pass


class Audio:
    def __init__(self):
        self.running = False
        self.stops = 0

    def start(self, **callbacks):
        self.running = True

    def stop(self):
        self.running = False
        self.stops += 1


class State(server.MeetingState):
    def watchdog(self, capture_generation=None):
        # Production launches a daemon. The test drives its real implementation
        # one iteration at a time, on demand, rather than racing wall time.
        pass


audio = Audio()
state = State(macos_factory=lambda **kw: audio)
now = 1000.0
sleeps = 0


def sleep(seconds):
    global sleeps
    sleeps += 1
    if sleeps > 1:
        raise TickComplete()


server.time = SimpleNamespace(monotonic=lambda: now, time=real_time.time, sleep=sleep)
app = FastAPI()
app.include_router(server.make_router(state))
with TestClient(app) as client:
    for line in sys.stdin:
        message = json.loads(line)
        try:
            if message.get('command') == 'tick':
                now += message['seconds']
                sleeps = 0
                try:
                    server.MeetingState.watchdog(state, state.capture_generation)
                except TickComplete:
                    pass
                result = {'listening': state.listening, 'running': audio.running,
                          'stops': audio.stops, 'seen': state.pane_seen, 'now': now,
                          'sleeps': sleeps}
            elif message.get('command') == 'segment':
                state.add_segment({'text': message['text']})
                result = {'segments': len(state.segments)}
            elif message.get('command') == 'inspect':
                result = {'listening': state.listening, 'running': audio.running,
                          'stops': audio.stops, 'seen': state.pane_seen, 'now': now}
            else:
                response = client.request(message.get('method', 'GET'), message['path'],
                                          **({'json': message['body']} if 'body' in message else {}))
                result = {'code': response.status_code, 'body': response.json()}
            print(json.dumps({'id': message['id'], 'result': result}), flush=True)
        except Exception as exc:
            print(json.dumps({'id': message['id'], 'error': repr(exc)}), flush=True)
