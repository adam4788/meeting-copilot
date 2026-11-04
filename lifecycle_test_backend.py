"""Offline JSON-lines bridge: real router/state, synthetic audio and watchdog clock.

Used by test_desktop.mjs; no listening socket, credentials, or hardware.
"""
import json
import sys
import time as real_time
from types import SimpleNamespace
from contextlib import ExitStack
from unittest.mock import patch
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
fault_process = None
capture = None
overlay_signals = []
overlay_process = None
close_overlay_stdout = None
with ExitStack() as faults, TestClient(app) as client:
    for line in sys.stdin:
        message = json.loads(line)
        try:
            if message.get('command') == 'close-overlay-stdout':
                assert close_overlay_stdout is not None
                close_overlay_stdout()
                result = {'closed': True}
            elif message.get('command') in ('install-overlay-fault', 'install-overlay-channel-fault'):
                if message['command'] == 'install-overlay-channel-fault':
                    from test_overlay_channel_health import closing_display
                    # The synthetic clock is driven only by explicit tick RPCs,
                    # not a wall-clock lease daemon racing this EOF scenario.
                    faults.enter_context(patch.object(state, '_start_overlay_lease_locked'))
                    display, children, close_overlay_stdout = faults.enter_context(closing_display())
                    assert display.show_overlay()['shown']
                    overlay_process = display.process
                else:
                    from test_overlay_teardown import real_display
                    display, overlay_process = faults.enter_context(real_display())
                state.overlay = display
                for name in ('terminate', 'kill'):
                    signal_patch = patch.object(overlay_process, name, side_effect=PermissionError('injected denied signaling'))
                    signal_patch.start()
                    faults.callback(signal_patch.stop)
                    overlay_signals.append(signal_patch)
                result = {'installed': True}
            elif message.get('command') == 'allow-overlay-stop':
                for signal_patch in overlay_signals:
                    signal_patch.stop()
                result = {'allowed': True}
            elif message.get('command') == 'inspect-overlay':
                result = {'alive': overlay_process is not None and overlay_process.poll() is None,
                          'pending': state.pending_overlay is not None,
                          'status': state.status(touch=False)}
            elif message.get('command') == 'install-macos-fault':
                # Production parser, status callback and group termination;
                # only OS I/O is synthetic. Never launch hardware/providers.
                import macos_audio
                from test_async_stop_safety import ParserWorker
                fault_process = ParserWorker()
                capture = macos_audio.MacOSAudioCapture()
                state.macos_factory = lambda **kw: capture
                faults.enter_context(patch('macos_audio.worker_python', return_value='/synthetic/python'))
                faults.enter_context(patch('macos_audio.subprocess.Popen', return_value=fault_process))
                faults.enter_context(patch('macos_audio._signal_group', side_effect=fault_process.signal))
                # Retry time is explicitly driven; no daemon races in UI checks.
                faults.enter_context(patch.object(state, '_start_stop_retry_locked'))
                result = {'installed': True}
            elif message.get('command') == 'wait-macos-error':
                assert capture is not None and capture._thread is not None and fault_process is not None
                capture._thread.join(2)
                assert not capture._thread.is_alive(), 'parser did not finish'
                assert capture.status()['callback_error'] is None, 'swallowed callback failure'
                result = {'running': fault_process.alive, 'status': state.status(touch=False)}
            elif message.get('command') == 'allow-stop':
                assert fault_process is not None
                fault_process.denied = False
                result = {'allowed': True}
            elif message.get('command') == 'tick':
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
                result = {'listening': state.listening, 'running': fault_process.alive if fault_process else audio.running,
                          'stops': audio.stops, 'seen': state.pane_seen, 'now': now}
            else:
                response = client.request(message.get('method', 'GET'), message['path'],
                                          **({'json': message['body']} if 'body' in message else {}))
                result = {'code': response.status_code, 'body': response.json()}
            print(json.dumps({'id': message['id'], 'result': result}), flush=True)
        except Exception as exc:
            print(json.dumps({'id': message['id'], 'error': repr(exc)}), flush=True)
