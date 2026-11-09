import asyncio
import contextlib
import http.server
import importlib.util
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import traceback
import unittest
import wave
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import meta_asr


class ResampleTests(unittest.TestCase):
    def test_resample_upsamples_and_encodes_int16(self):
        import numpy as np
        pcm = meta_asr.resample_to_24k(np.zeros(16000, dtype=np.float32))
        self.assertEqual(len(pcm), 24000 * 2)
        loud = meta_asr.resample_to_24k(np.full(160, 2.0, dtype=np.float32))
        self.assertEqual(len(loud), 240 * 2)
        self.assertEqual(meta_asr.resample_to_24k([]), b'')


class HandshakeTests(unittest.TestCase):
    def test_handshake_shape(self):
        frame = meta_asr.build_handshake('KEY123', 'muse-voice-transcribe-1.0', 'DIARIZATION')
        self.assertEqual(frame['authorization'], {'accessToken': 'Bearer KEY123'})
        self.assertEqual(frame['audioEncoding'], 'PCM_24KHZ')
        self.assertEqual(frame['mode'], 'DIARIZATION')
        self.assertEqual(frame['model'], 'muse-voice-transcribe-1.0')

    def test_missing_key_fails_without_leaking(self):
        with self.assertRaises(meta_asr.MetaAsrError) as ctx:
            meta_asr.build_handshake('')
        self.assertIn('MODEL_API_KEY', str(ctx.exception))


class EventMapperTests(unittest.TestCase):
    def test_event_mapper_finals_partials_and_speakers(self):
        mapper = meta_asr.EventMapper('you')
        self.assertIsNone(mapper.map({'type': 'speechStart', 'turnId': 1}, 1.0))
        self.assertIsNone(mapper.map({'type': 'transcript', 'transcript': 'hel', 'final': False}, 1.0))
        mapper.map({'type': 'speaker', 'label': 'B', 'turnId': 1}, 1.0)
        segment = mapper.map({'type': 'speechComplete', 'turnId': 1, 'transcript': 'Hello there.'}, 2.0)
        self.assertEqual(segment['speaker'], 'you \u00b7 Speaker B')
        self.assertEqual(segment['text'], 'Hello there.')
        final = mapper.map({'type': 'transcript', 'transcript': 'Done.', 'final': True}, 3.0)
        self.assertEqual(final['speaker'], 'you')
        with self.assertRaises(meta_asr.MetaAsrError):
            mapper.map({'type': 'error', 'message': 'boom'}, 4.0)


class FakeSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        await asyncio.sleep(0)
        return json.dumps({'sessionId': 's-1'})

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)


class SessionTests(unittest.TestCase):
    def test_run_session_streams_frames_and_emits_finals(self):
        socket = FakeSocket([
            json.dumps({'type': 'transcript', 'transcript': 'partial', 'final': False}),
            json.dumps({'type': 'speechComplete', 'turnId': 1, 'transcript': 'Final words.'}),
        ])
        emitted = []

        async def main():
            queue = asyncio.Queue()
            await queue.put(b'\x00\x00' * 100)
            await queue.put(None)
            await meta_asr.run_session(queue, emitted.append, 'KEY123',
                                       connect=lambda: socket, clock=lambda: 9.0)

        asyncio.run(main())
        handshake = json.loads(socket.sent[0])
        self.assertEqual(handshake['authorization']['accessToken'], 'Bearer KEY123')
        self.assertEqual(socket.sent[1], b'\x00\x00' * 100)
        self.assertEqual(json.loads(socket.sent[2]), {'type': 'endStream'})
        self.assertEqual(emitted, [{'type': 'segment', 'speaker': 'you',
                                    'text': 'Final words.', 'timestamp': 9.0}])


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


class FileTranscribeTests(unittest.TestCase):
    def test_transcribe_file_builds_multipart_and_parses_turns(self):
        captured = {}

        def opener(request):
            captured['request'] = request
            return _Response({'turns': [
                {'transcript': 'First turn.', 'speaker': 'A'},
                {'transcript': 'Second turn.'},
            ]})

        import tempfile, os, wave
        with tempfile.NamedTemporaryFile('wb', suffix='.wav', delete=False) as handle:
            path = handle.name
        with wave.open(path, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\x00\x00' * 2400)
        try:
            segments = meta_asr.transcribe_file(path, 'KEY123', opener=opener)
        finally:
            os.unlink(path)
        request = captured['request']
        self.assertEqual(request.headers['Authorization'], 'Bearer KEY123')
        body = request.data
        self.assertIn(b'name="request"', body)
        self.assertIn(b'muse-voice-transcribe-1.0', body)
        self.assertIn(b'name="audio"', body)
        self.assertIn(b'"audioEncoding": "WAV"', body)
        self.assertEqual([s['text'] for s in segments], ['First turn.', 'Second turn.'])
        self.assertEqual(segments[0]['speaker'], 'you \u00b7 Speaker A')

    def test_transcribe_file_requires_key(self):
        with self.assertRaises(meta_asr.MetaAsrError):
            meta_asr.transcribe_file('/nonexistent.wav', '')


class KeyResolutionTests(unittest.TestCase):
    def test_plugin_never_loads_dotenv_or_standalone_environment_keys(self):
        import os
        import meta_models
        from unittest.mock import patch
        with patch.dict(os.environ, {'MODEL_API_KEY':'synthetic-cli'}), patch('byok.secret',return_value=''):
            self.assertEqual(meta_models.resolve_model_api_key(),'')
        with patch.dict(os.environ, {'MODEL_API_KEY':'synthetic-cli'}):
            self.assertEqual(meta_models.resolve_model_api_key(standalone=True),'')
        self.assertFalse(hasattr(meta_models,'_load_dotenv_key_once'))


class WorkerProviderTests(unittest.TestCase):
    def test_worker_meta_without_key_fails_cleanly(self):
        import os
        import subprocess
        import sys
        import tempfile
        import wave
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.NamedTemporaryFile('wb', suffix='.wav', delete=False) as handle:
            path = handle.name
        try:
            with wave.open(path, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b'\x00\x00' * 2400)
            env = {k: v for k, v in os.environ.items() if k != 'MODEL_API_KEY'}
            env['HERMES_HOME'] = tempfile.mkdtemp(prefix='mc-no-key-')
            result = subprocess.run(
                [sys.executable, os.path.join(root, 'audio_worker.py'),
                 '--file', path, '--provider', 'meta'],
                capture_output=True, text=True, timeout=30, env=env)
        finally:
            os.unlink(path)
        self.assertEqual(result.returncode, 1)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        errors = [e for e in events if e.get('type') == 'error']
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['code'], 'asr_auth')
        self.assertNotIn('Bearer', result.stdout)


SYNTHETIC_KEY = 'synthetic-meta-key'
SYNTHETIC_AUDIO = b'synthetic-private-audio'
URL_SECRET = 'synthetic-url-credential'


def assert_sanitized(test, error):
    rendered = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    for private in (SYNTHETIC_KEY, SYNTHETIC_AUDIO.decode(), URL_SECRET):
        test.assertTrue(private not in rendered, 'exception echoed synthetic private content')


class SanitizedErrorTests(unittest.TestCase):
    def test_provider_error_and_invalid_mode_do_not_echo_supplied_content(self):
        private = f'{SYNTHETIC_KEY} {SYNTHETIC_AUDIO.decode()} {URL_SECRET}'
        with self.assertRaises(meta_asr.MetaAsrError) as error:
            meta_asr.EventMapper().map({'type': 'error', 'message': private}, 0)
        assert_sanitized(self, error.exception)
        with self.assertRaises(meta_asr.MetaAsrError) as error:
            meta_asr.build_handshake(SYNTHETIC_KEY, mode=private)
        assert_sanitized(self, error.exception)

    def test_injected_transport_failure_is_sanitized(self):
        class FailedConnect:
            async def __aenter__(self):
                raise RuntimeError(f'wss://user:{URL_SECRET}@example.invalid/{SYNTHETIC_AUDIO.decode()}')

            async def __aexit__(self, *args):
                pass

        async def run():
            try:
                await meta_asr.run_session(asyncio.Queue(), lambda _: None, SYNTHETIC_KEY,
                                           connect=FailedConnect)
            except meta_asr.MetaAsrError as error:
                assert_sanitized(self, error)
            else:
                self.fail('transport error was not reported')

        asyncio.run(run())

    def test_file_opener_meta_error_is_sanitized(self):
        def opener(_request):
            raise meta_asr.MetaAsrError(f'{URL_SECRET} {SYNTHETIC_AUDIO.decode()}')

        with tempfile.TemporaryDirectory() as directory:
            path = make_wav(directory)
            try:
                meta_asr.transcribe_file(path, SYNTHETIC_KEY, opener=opener)
            except meta_asr.MetaAsrError as error:
                assert_sanitized(self, error)
            else:
                self.fail('transport error was not reported')


def make_wav(directory):
    path = str(Path(directory) / 'synthetic.wav')
    with wave.open(path, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(SYNTHETIC_AUDIO)
    return path


class LocalTLSCase(unittest.TestCase):
    """Generate an ephemeral localhost-only certificate; no provider calls."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        openssl = shutil.which('openssl')
        if not openssl:
            raise unittest.SkipTest('localhost TLS tests require the openssl executable')
        cls._tls_dir = tempfile.TemporaryDirectory(prefix='meta-asr-tls-')
        cls.addClassCleanup(cls._tls_dir.cleanup)
        root = Path(cls._tls_dir.name)
        cls._cert, key = root / 'cert.pem', root / 'key.pem'
        config = root / 'openssl.cnf'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\n'
                          '[dn]\n[ext]\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n')
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-keyout', str(key), '-out', str(cls._cert),
                        '-subj', '/CN=localhost', '-config', str(config)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        cls._server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls._server_tls.load_cert_chain(str(cls._cert), str(key))

    def tls_environment(self):
        return patch.dict(os.environ, {'SSL_CERT_FILE': str(self._cert),
                                       'NO_PROXY': '*', 'no_proxy': '*'})


@contextlib.contextmanager
def https_server(handler, tls):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class HttpTransportTests(LocalTLSCase):
    def test_redirects_do_not_forward_key_or_audio(self):
        sink_requests = []
        origin_requests = []
        location = ['']
        status = [302]

        class Sink(http.server.BaseHTTPRequestHandler):
            def record(self):
                sink_requests.append((self.command, self.headers.get('Authorization')))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"transcript":"synthetic"}')

            do_GET = record
            do_POST = record

            def log_message(self, format, *args):
                pass

        class Origin(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                origin_requests.append((self.headers.get('Authorization'), body))
                self.send_response(status[0])
                self.send_header('Location', location[0])
                self.end_headers()

            def log_message(self, format, *args):
                pass

        with tempfile.TemporaryDirectory() as directory, \
             https_server(Sink, self._server_tls) as sink, \
             https_server(Origin, self._server_tls) as origin, self.tls_environment():
            path = make_wav(directory)
            origin_url = f'https://127.0.0.1:{origin.server_port}/transcribe'
            with patch.object(meta_asr, 'TRANSCRIBE_URL', origin_url):
                for code in (300, 301, 302, 303, 307, 308):
                    with self.subTest(code=code):
                        sink_requests.clear()
                        status[0] = code
                        userinfo = f'user:{URL_SECRET}@' if code == 302 else ''
                        location[0] = (f'https://{userinfo}127.0.0.1:{sink.server_port}/other'
                                       f'?token={URL_SECRET}')
                        error = None
                        try:
                            meta_asr.transcribe_file(path, SYNTHETIC_KEY)
                        except Exception as exc:
                            error = exc
                        self.assertEqual(len(sink_requests), 0, 'redirect target was contacted')
                        self.assertIsInstance(error, meta_asr.MetaAsrError)
                        self.assertIn('redirect', str(error).lower())
                        assert_sanitized(self, error)
                status[0] = 302
                location[0] = f'{origin_url}/same-origin?token={URL_SECRET}'
                before = len(origin_requests)
                with self.assertRaises(meta_asr.MetaAsrError) as error:
                    meta_asr.transcribe_file(path, SYNTHETIC_KEY)
                self.assertEqual(len(origin_requests), before + 1)
                assert_sanitized(self, error.exception)
        self.assertGreaterEqual(len(origin_requests), 1)
        self.assertTrue(all(auth == f'Bearer {SYNTHETIC_KEY}' and SYNTHETIC_AUDIO in body
                            for auth, body in origin_requests))

    def test_original_https_origin_receives_post_and_returns_transcript(self):
        requests = []

        class Origin(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.headers.get('Authorization'),
                                 self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"transcript":"synthetic result"}')

            def log_message(self, format, *args):
                pass

        with tempfile.TemporaryDirectory() as directory, \
             https_server(Origin, self._server_tls) as origin, self.tls_environment():
            url = f'https://127.0.0.1:{origin.server_port}/transcribe'
            with patch.object(meta_asr, 'TRANSCRIBE_URL', url):
                result = meta_asr.transcribe_file(make_wav(directory), SYNTHETIC_KEY)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0][0] == f'Bearer {SYNTHETIC_KEY}')
        self.assertTrue(SYNTHETIC_AUDIO in requests[0][1])
        self.assertEqual(result[0]['text'], 'synthetic result')


@unittest.skipUnless(importlib.util.find_spec('websockets'),
                     'optional audio-runtime websockets dependency is not installed')
class WebSocketTransportTests(LocalTLSCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from websockets.legacy.server import serve
        cls.serve = staticmethod(serve)

    async def receiver(self, ws):
        self.connections += 1
        handshake = await ws.recv()
        self.frames.append(handshake)
        await ws.send(json.dumps({'sessionId': 'synthetic-session'}))
        async for message in ws:
            self.frames.append(message)
            if isinstance(message, str) and json.loads(message).get('type') == 'endStream':
                await ws.send(json.dumps({'type': 'speechComplete', 'transcript': 'synthetic result'}))
                await ws.close()
                break

    def setUp(self):
        self.connections = 0
        self.frames = []

    @staticmethod
    def client_families():
        import websockets
        installed = int(getattr(websockets, '__version__').split('.')[0])
        # Modern releases still include the legacy client. Exercise both
        # hooks without claiming the separately packaged older release ran.
        return (installed, 12) if installed >= 14 else (installed,)

    async def run_client(self):
        queue = asyncio.Queue()
        await queue.put(SYNTHETIC_AUDIO)
        await queue.put(None)
        emitted = []
        await asyncio.wait_for(meta_asr.run_session(queue, emitted.append, SYNTHETIC_KEY,
                                                    clock=lambda: 7.0), timeout=8)
        return emitted

    def test_tls_redirects_do_not_connect_to_target_or_send_frames(self):
        import websockets
        from websockets.legacy.client import connect as legacy_connect
        redirect_limit = getattr(legacy_connect, 'MAX_REDIRECTS_ALLOWED', None)

        async def exercise():
            async def redirect_server(code, location, attempts):
                finished = asyncio.Event()

                async def respond(reader, writer):
                    try:
                        await reader.readuntil(b'\r\n\r\n')
                        attempts.append(1)
                        writer.write((f'HTTP/1.1 {code} {HTTPStatus(code).phrase}\r\n'
                                      f'Location: {location}\r\nContent-Length: 0\r\n'
                                      'Connection: close\r\n\r\n').encode())
                        await writer.drain()
                    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
                        pass
                    finally:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except (ConnectionError, ssl.SSLError):
                            pass
                        finished.set()

                server = await asyncio.start_server(respond, '127.0.0.1', 0, ssl=self._server_tls)
                return server, finished

            async with self.serve(self.receiver, '127.0.0.1', 0, ssl=self._server_tls) as sink:
                sink_port = next(iter(sink.sockets)).getsockname()[1]
                for code in (300, 301, 302, 303, 307, 308):
                    with self.subTest(code=code):
                        attempts = []
                        self.connections = 0
                        self.frames.clear()

                        userinfo = f'user:{URL_SECRET}@' if code == 302 else ''
                        location = f'wss://{userinfo}127.0.0.1:{sink_port}/other?token={URL_SECRET}'
                        origin, finished = await redirect_server(code, location, attempts)
                        async with origin:
                            port = next(iter(origin.sockets)).getsockname()[1]
                            with patch.object(meta_asr, 'REALTIME_URL', f'wss://127.0.0.1:{port}/asr'):
                                error = None
                                try:
                                    await self.run_client()
                                except Exception as exc:
                                    error = exc
                            await asyncio.wait_for(finished.wait(), 3)
                            self.assertEqual(len(attempts), 1)
                            self.assertEqual(self.connections, 0, 'redirect target was contacted')
                            self.assertEqual(len(self.frames), 0, 'redirect target received frames')
                            self.assertIsInstance(error, meta_asr.MetaAsrError)
                            if code != 300:
                                self.assertIn('redirect', str(error).lower())
                            assert_sanitized(self, error)

                attempts = []
                origin, finished = await redirect_server(302, '/moved', attempts)
                async with origin:
                    port = next(iter(origin.sockets)).getsockname()[1]
                    with patch.object(meta_asr, 'REALTIME_URL', f'wss://127.0.0.1:{port}/asr'):
                        with self.assertRaises(meta_asr.MetaAsrError) as error:
                            await self.run_client()
                    await asyncio.wait_for(finished.wait(), 3)
                    self.assertEqual(len(attempts), 1)
                    assert_sanitized(self, error.exception)

        with self.tls_environment():
            for major in self.client_families():
                with self.subTest(client_family=major), patch.object(websockets, '__version__', f'{major}.0'):
                    asyncio.run(exercise())
        self.assertEqual(getattr(legacy_connect, 'MAX_REDIRECTS_ALLOWED', None), redirect_limit)

    def test_original_wss_origin_streams_handshake_audio_and_events(self):
        import websockets
        async def exercise():
            async with self.serve(self.receiver, '127.0.0.1', 0, ssl=self._server_tls) as origin:
                port = next(iter(origin.sockets)).getsockname()[1]
                with patch.object(meta_asr, 'REALTIME_URL', f'wss://127.0.0.1:{port}/asr'):
                    emitted = await self.run_client()
            self.assertEqual(self.connections, 1)
            self.assertEqual(len(self.frames), 3)
            handshake = json.loads(self.frames[0])
            self.assertTrue(handshake['authorization']['accessToken'] == f'Bearer {SYNTHETIC_KEY}')
            self.assertTrue(self.frames[1] == SYNTHETIC_AUDIO)
            self.assertEqual(json.loads(self.frames[2]), {'type': 'endStream'})
            self.assertEqual(emitted[0]['text'], 'synthetic result')
            self.assertEqual(emitted[0]['timestamp'], 7.0)

        with self.tls_environment():
            for major in self.client_families():
                self.connections = 0
                self.frames.clear()
                with self.subTest(client_family=major), patch.object(websockets, '__version__', f'{major}.0'):
                    asyncio.run(exercise())

    def test_websockets_15_keeps_original_origin_proxy_behavior(self):
        import websockets
        if int(getattr(websockets, '__version__').split('.')[0]) != 15:
            self.skipTest('automatic WebSocket proxy support is specific to websockets 15')

        async def exercise():
            attempts = []
            async with self.serve(self.receiver, '127.0.0.1', 0, ssl=self._server_tls) as origin:
                origin_port = next(iter(origin.sockets)).getsockname()[1]

                async def proxy(reader, writer):
                    peer = None
                    try:
                        request = await reader.readuntil(b'\r\n\r\n')
                        attempts.append(request.split(b'\r\n', 1)[0])
                        target, peer = await asyncio.open_connection('127.0.0.1', origin_port)
                        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                        await writer.drain()

                        async def forward(source, destination):
                            while data := await source.read(65536):
                                destination.write(data)
                                await destination.drain()
                            destination.close()

                        await asyncio.gather(forward(reader, peer), forward(target, writer),
                                             return_exceptions=True)
                    finally:
                        writer.close()
                        if peer is not None:
                            peer.close()

                server = await asyncio.start_server(proxy, '127.0.0.1', 0)
                try:
                    proxy_port = next(iter(server.sockets)).getsockname()[1]
                    proxy_url = f'http://127.0.0.1:{proxy_port}'
                    with patch.object(meta_asr, 'REALTIME_URL', f'wss://127.0.0.1:{origin_port}/asr'), \
                         patch.dict(os.environ, {'wss_proxy': proxy_url, 'no_proxy': '', 'NO_PROXY': ''}), \
                         patch('urllib.request.proxy_bypass', return_value=False):
                        emitted = await self.run_client()
                    self.assertEqual(len(attempts), 1, 'the configured proxy was bypassed')
                    self.assertTrue(attempts[0] == f'CONNECT 127.0.0.1:{origin_port} HTTP/1.1'.encode())
                    self.assertEqual(emitted[0]['text'], 'synthetic result')
                finally:
                    server.close()
                    await server.wait_closed()

        with self.tls_environment():
            asyncio.run(exercise())

    def test_cancellation_closes_actual_wss_connection(self):
        async def exercise():
            received_audio = asyncio.Event()
            closed = asyncio.Event()
            frames = []

            async def waiting_receiver(ws):
                try:
                    await ws.recv()  # handshake
                    await ws.send(json.dumps({'sessionId': 'synthetic-session'}))
                    async for frame in ws:
                        frames.append(frame)
                        received_audio.set()
                finally:
                    closed.set()

            async with self.serve(waiting_receiver, '127.0.0.1', 0, ssl=self._server_tls) as origin:
                port = next(iter(origin.sockets)).getsockname()[1]
                with patch.object(meta_asr, 'REALTIME_URL', f'wss://127.0.0.1:{port}/asr'):
                    queue = asyncio.Queue()
                    await queue.put(SYNTHETIC_AUDIO)
                    task = asyncio.create_task(meta_asr.run_session(queue, lambda _: None, SYNTHETIC_KEY))
                    await asyncio.wait_for(received_audio.wait(), 3)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await asyncio.wait_for(closed.wait(), 3)
            self.assertEqual(len(frames), 1)
            self.assertTrue(frames[0] == SYNTHETIC_AUDIO)

        with self.tls_environment():
            asyncio.run(exercise())

    def test_unsupported_websockets_version_fails_closed(self):
        import websockets

        async def exercise():
            with patch.object(meta_asr, 'REALTIME_URL', 'wss://127.0.0.1:9/asr'):
                for version in ('11.0', '16.0', 'unrecognized'):
                    with self.subTest(version=version), patch.object(websockets, '__version__', version):
                        with self.assertRaises(meta_asr.MetaAsrError) as error:
                            await self.run_client()
                        self.assertIn('websockets >=12,<16', str(error.exception))

        asyncio.run(exercise())


class CancellationTests(unittest.TestCase):
    def test_cancellation_closes_injected_socket_and_stops_sender(self):
        async def exercise():
            ready, sending, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

            class BlockingSocket(FakeSocket):
                async def __aexit__(self, *args):
                    closed.set()
                    return False

                async def send(self, payload):
                    await super().send(payload)
                    if payload == SYNTHETIC_AUDIO:
                        sending.set()

                async def __anext__(self):
                    ready.set()
                    await asyncio.Event().wait()

            socket = BlockingSocket([])
            queue = asyncio.Queue()
            await queue.put(SYNTHETIC_AUDIO)
            task = asyncio.create_task(meta_asr.run_session(queue, lambda _: None, SYNTHETIC_KEY,
                                                             connect=lambda: socket))
            await asyncio.wait_for(ready.wait(), 2)
            await asyncio.wait_for(sending.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
            await asyncio.sleep(0)
            await queue.put(b'unconsumed')
            await asyncio.sleep(0)
            self.assertEqual(queue.qsize(), 1)

        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
