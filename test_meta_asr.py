import asyncio
import json
import unittest

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


class WorkerProviderTests(unittest.TestCase):
    def test_worker_meta_without_key_fails_cleanly(self):
        import os
        import subprocess
        import sys
        import tempfile
        import wave
        root = os.path.dirname(os.path.abspath(__file__))
        with tempfile.NamedTemporaryFile('wb', suffix='.wav', delete=False) as handle:
            path = handle.name
        try:
            with wave.open(path, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b'\x00\x00' * 2400)
            env = {k: v for k, v in os.environ.items() if k != 'MODEL_API_KEY'}
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


if __name__ == '__main__':
    unittest.main()
