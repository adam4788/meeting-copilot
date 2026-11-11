"""Synthetic Google protocol fixtures only: no network, device or credential access."""
import asyncio
import base64
import json
import io
import wave
import unittest
from unittest.mock import patch

import google_asr as g


class FakeSocket:
    def __init__(self, events=(), *, ack=None, finish=True):
        self.events = list(events)
        self.ack = {'setupComplete': {}} if ack is None else ack
        self.finish = finish
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if 'setup' in message:
            await self.incoming.put(json.dumps(self.ack))
        if message.get('realtimeInput', {}).get('audioStreamEnd'):
            for event in self.events:
                await self.incoming.put(event if isinstance(event, (str, bytes)) else json.dumps(event))
            if self.finish:
                await self.incoming.put(json.dumps({'serverContent': {'turnComplete': True, 'interactionStatus': 'IDLE'}}))

    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.closed = True


def final(text='Fictional Atlas checklist.', **extra):
    return {'serverContent': {'inputTranscription': {'text': text}, **extra}}


async def run(socket, frames=(b'\x00\x00' * 160,), **kwargs):
    queue = asyncio.Queue(maxsize=64)
    for frame in frames:
        await queue.put(frame)
    await queue.put(None)
    segments, usage = [], []
    with patch.object(g, 'DRAIN_TIMEOUT', .01):
        result = await g.run_session(queue, segments.append, 'synthetic-key', connect=lambda: socket,
                                     clock=lambda: 123., usage_callback=usage.append, **kwargs)
    return result, segments, usage


class ContractTests(unittest.TestCase):
    def test_fixed_model_mode_and_no_key_in_setup(self):
        setup = g.build_handshake()
        self.assertEqual(setup['setup']['model'], 'models/gemini-3.5-transcribe-live')
        self.assertEqual(setup['setup']['generationConfig'], {'responseModalities': ['TEXT'], 'maxOutputTokens': 256})
        self.assertEqual(setup['setup']['inputAudioTranscription'], {'languageCodes': []})
        for model, mode in [('other', 'ENDPOINTING'), (g.DEFAULT_MODEL, 'DIARIZATION'), (g.DEFAULT_MODEL, 'PUSH_TO_TALK')]:
            with self.assertRaises(g.GoogleAsrError):
                g.validate(model, mode)

    def test_pcm_conversion_bounds_finiteness_and_endianness(self):
        import numpy as np
        value = g.pcm16(np.array([-2., -.5, 0., .5, 2.], dtype=np.float32))
        self.assertEqual(np.frombuffer(value, '<i2').tolist(), [-32767, -16383, 0, 16383, 32767])
        for value in [[float('nan')], [float('inf')], [[0., 0.]], np.zeros(16001)]:
            with self.assertRaises(g.GoogleAsrError):
                g.pcm16(value)

    def test_connector_uses_header_only_fixed_origin_no_proxy_and_bounded_transport(self):
        captured = {}
        def connector(uri, **kwargs):
            captured.update(uri=uri, **kwargs)
            return object()
        with patch.object(g, '_connector_class', return_value=connector):
            g._connect('synthetic-header-key')
        self.assertEqual(captured['uri'], g.REALTIME_URL)
        self.assertNotIn('?', captured['uri'])
        self.assertEqual(captured['additional_headers'], {'x-goog-api-key': 'synthetic-header-key'})
        self.assertIsNone(captured['proxy'])
        self.assertIsNone(captured['compression'])
        self.assertEqual(captured['max_queue'], 8)
        self.assertEqual(captured['max_size'], g.MAX_MESSAGE_BYTES)
        self.assertTrue(captured['logger'].disabled)
        self.assertFalse(captured['logger'].propagate)

    def test_real_connector_redirect_hook_rejects_all_redirects_without_network(self):
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        cls = g._connector_class()
        obj = object.__new__(cls)
        for code in [301, 302, 307, 308]:
            err = InvalidStatus(Response(code, 'redirect', Headers({'Location': 'wss://other.invalid'})))
            self.assertIsInstance(obj.process_redirect(err), g._RedirectBlocked)
        plain = RuntimeError('synthetic')
        self.assertIs(obj.process_redirect(plain), plain)

    def test_runtime_probe_never_connects_or_reads_environment_auth(self):
        with patch.object(g, '_connect', side_effect=AssertionError('network forbidden')):
            self.assertTrue(g.runtime_available())
        with patch.object(g, '_connector_class', side_effect=g.GoogleAsrError('google_runtime')):
            self.assertFalse(g.runtime_available())


class MapperTests(unittest.TestCase):
    def test_final_only_source_labels_and_timestamp(self):
        mapper = g.EventMapper('remote')
        self.assertIsNone(mapper.map({'serverContent': {'interimInputTranscription': {'text': 'not final'}}}, 1))
        self.assertIsNone(mapper.map({'serverContent': {'inputTranscription': {'text': 'partial', 'finished': False}}}, 1))
        self.assertEqual(mapper.map(final(), 3), {'type': 'segment', 'speaker': 'remote', 'text': 'Fictional Atlas checklist.', 'timestamp': 3})
        self.assertFalse(mapper.report['usage_available'])
        self.assertIsNone(mapper.report['input_tokens'])

    def test_event_limits_fail_instead_of_truncation(self):
        mapper = g.EventMapper()
        with self.assertRaises(g.GoogleAsrError) as cm:
            mapper.map(final('x' * (g.MAX_OUTPUT_CHARACTERS + 1)), 1)
        self.assertEqual(cm.exception.code, 'google_limit')
        self.assertEqual(mapper.report['final_segments'], 0)
        for _ in range(g.MAX_FINAL_EVENTS):
            mapper.map(final('x'), 1)
        with self.assertRaises(g.GoogleAsrError):
            mapper.map(final('one more'), 1)

    def test_unexpected_provider_shapes_and_agent_outputs_rejected(self):
        events = [[], {'error': {'message': 'synthetic-key'}}, {'toolCall': {}}, {'goAway': {}},
                  {'serverContent': []}, {'serverContent': {'modelTurn': {}}},
                  {'serverContent': {'outputTranscription': {}}}, final(None),
                  {'serverContent': {'inputTranscription': 'wrong'}}]
        for event in events:
            with self.subTest(event=event), self.assertRaises(g.GoogleAsrError) as cm:
                g.EventMapper().map(event, 1)
            self.assertNotIn('synthetic-key', str(cm.exception))

    def test_usage_is_provider_report_not_fabricated_or_assumed_cumulative(self):
        mapper = g.EventMapper()
        mapper.map({'usageMetadata': {'promptTokenCount': 5, 'responseTokenCount': 2, 'totalTokenCount': 7}}, 1)
        mapper.map({'usageMetadata': {'promptTokenCount': 6, 'responseTokenCount': 3, 'totalTokenCount': 9}}, 1)
        self.assertEqual(mapper.report['input_tokens'], 6)
        self.assertEqual(mapper.report['reported_input_tokens_sum'], 11)
        self.assertEqual(mapper.report['output_tokens'], 3)
        self.assertTrue(mapper.report['usage_available'])
        mapper.map({'usageMetadata': {'totalTokenCount': 1}}, 1)
        self.assertFalse(mapper.report['usage_available'])
        self.assertIsNone(mapper.report['output_tokens'])
        for usage in [{'promptTokenCount': True}, {'responseTokenCount': -1}, {'totalTokenCount': 1.2}, {'responseTokenCount': 999999}]:
            with self.assertRaises(g.GoogleAsrError):
                g.EventMapper().map({'usageMetadata': usage}, 1)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_pcm_and_commits_only_finals_with_usage(self):
        socket = FakeSocket([{'serverContent': {'interimInputTranscription': {'text': 'partial'}}}, final(),
                             {'usageMetadata': {'promptTokenCount': 3, 'responseTokenCount': 4, 'totalTokenCount': 7}}])
        result, segments, updates = await run(socket, default_speaker='remote')
        self.assertTrue(socket.closed)
        self.assertEqual(segments, [{'type': 'segment', 'speaker': 'remote', 'text': 'Fictional Atlas checklist.', 'timestamp': 123.}])
        self.assertEqual(base64.b64decode(socket.sent[1]['realtimeInput']['audio']['data']), b'\0\0' * 160)
        self.assertEqual(socket.sent[1]['realtimeInput']['audio']['mimeType'], 'audio/pcm;rate=16000')
        self.assertEqual(socket.sent[-1], {'realtimeInput': {'audioStreamEnd': True}})
        self.assertEqual(result['audio_bytes'], 320)
        self.assertEqual(result['audio_seconds'], .01)
        self.assertFalse(result['drain_confirmed'])
        self.assertTrue(result['usage_available'])
        self.assertTrue(updates)

    async def test_audio_over_limit_never_sends_excess_frame(self):
        socket = FakeSocket()
        with self.assertRaises(g.GoogleAsrError) as cm:
            await run(socket, frames=(b'\0\0' * 161,), max_audio_seconds=.01)
        self.assertEqual(cm.exception.code, 'google_limit')
        self.assertTrue(socket.closed)
        self.assertEqual(len(socket.sent), 1, 'only setup, no rejected audio')

    async def test_invalid_frames_and_keys_fail_closed(self):
        for frame in [b'1', '', 9, b'', b'\0' * (g.MAX_FRAME_BYTES + 2)]:
            socket = FakeSocket()
            with self.assertRaises(g.GoogleAsrError):
                await run(socket, frames=(frame,))
            self.assertTrue(socket.closed)
        for key in ['', 'bad\r\nkey', 'auth_tokens/unsupported', None]:
            with self.assertRaises(g.GoogleAsrError), patch.object(g, '_connect', side_effect=AssertionError('must not connect')):
                await g.run_session(asyncio.Queue(), lambda _: None, key)

    async def test_bad_ack_oversized_json_and_provider_error_are_sanitized(self):
        for socket in [FakeSocket(ack={'setupComplete': None}), FakeSocket([{'error': {'message': 'synthetic-key'}}]),
                       FakeSocket(['{malformed synthetic-key']), FakeSocket(['x' * (g.MAX_MESSAGE_BYTES + 1)])]:
            with self.assertRaises(g.GoogleAsrError) as cm:
                await run(socket)
            self.assertNotIn('synthetic-key', str(cm.exception))
            self.assertTrue(socket.closed)

    async def test_unconfirmed_drain_keeps_finals_without_claiming_complete_tail(self):
        socket = FakeSocket([final()], finish=False)
        emitted, updates = [], []
        queue = asyncio.Queue()
        await queue.put(None)
        with patch.object(g, 'DRAIN_TIMEOUT', .01):
            result = await g.run_session(queue, emitted.append, 'synthetic-key', connect=lambda: socket, usage_callback=updates.append)
        self.assertEqual(len(emitted), 1)
        self.assertFalse(updates[-1]['drain_confirmed'])
        self.assertFalse(result['tail_confirmed'])
        self.assertIn('unconfirmed', result['warning'])
        self.assertTrue(socket.closed)

    async def test_cancellation_stops_sender_and_closes_without_retry(self):
        socket = FakeSocket()
        calls = []
        def connect():
            calls.append(1)
            return socket
        task = asyncio.create_task(g.run_session(asyncio.Queue(), lambda _: None, 'synthetic-key', connect=connect))
        while not socket.sent:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(socket.closed)
        self.assertEqual(len(calls), 1)

    async def test_failure_after_audio_send_preserves_attempted_numeric_usage(self):
        class FailingSocket(FakeSocket):
            async def send(self, raw):
                await super().send(raw)
                if 'audio' in json.loads(raw).get('realtimeInput', {}):
                    raise RuntimeError('synthetic-key in raw transport exception')
        socket, updates = FailingSocket(), []
        queue = asyncio.Queue()
        await queue.put(b'\0\0' * 160)
        with self.assertRaises(g.GoogleAsrError) as cm:
            await g.run_session(queue, lambda _: None, 'synthetic-key', connect=lambda: socket, usage_callback=updates.append)
        self.assertEqual(updates[-1]['audio_bytes'], 320)
        self.assertFalse(updates[-1]['usage_available'])
        self.assertNotIn('synthetic-key', str(cm.exception))
        self.assertTrue(socket.closed)

    async def test_close_failure_is_not_reported_success(self):
        class CloseFail(FakeSocket):
            async def close(self):
                raise RuntimeError('synthetic-key')
        with self.assertRaises(g.GoogleAsrError) as cm:
            await run(CloseFail())
        self.assertNotIn('synthetic-key', str(cm.exception))

    async def test_wall_timeout_is_bounded_even_with_no_input(self):
        socket = FakeSocket()
        with patch.object(g, 'MAX_WALL_SECONDS', .3), patch.object(g, 'CLOSE_TIMEOUT', .01), self.assertRaises(g.GoogleAsrError):
            await g.run_session(asyncio.Queue(), lambda _: None, 'synthetic-key', connect=lambda: socket)
        self.assertTrue(socket.closed)


class WavTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def wav(frames=16000, rate=16000, channels=1, width=2):
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(width)
            wav.setframerate(rate)
            wav.writeframes(b'\0' * frames * channels * width)
        return data.getvalue()

    def test_strict_wav_contract(self):
        self.assertEqual(len(g.pcm_from_wav(self.wav())), 32000)
        for value in [self.wav(rate=24000), self.wav(channels=2), self.wav(width=1),
                      self.wav(frames=15999), self.wav(frames=960001),
                      self.wav()[:-2], self.wav() + b'extra', b'not wav']:
            with self.subTest(size=len(value)), self.assertRaises(g.GoogleAsrError):
                g.pcm_from_wav(value)
        # A forged RIFF length cannot disguise a truncated PCM payload.
        value = self.wav()[:-2]
        value = value[:4] + (len(value)-8).to_bytes(4, 'little') + value[8:]
        with self.assertRaises(g.GoogleAsrError):
            g.pcm_from_wav(value)

    async def test_wav_uses_same_paced_transport_and_unknown_tail(self):
        socket, segments = FakeSocket([final()], finish=False), []
        with patch.object(g, 'DRAIN_TIMEOUT', .01):
            result = await g.transcribe_wav(self.wav(), segments.append, 'synthetic-key',
                                            default_speaker='remote', connect=lambda: socket)
        self.assertEqual(result['audio_bytes'], 32000)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['speaker'], 'remote')
        self.assertFalse(result['tail_confirmed'])
        self.assertEqual(sum('audio' in packet.get('realtimeInput', {}) for packet in socket.sent), 10)
        self.assertTrue(socket.closed)

    async def test_bad_wav_never_connects(self):
        with self.assertRaises(g.GoogleAsrError):
            await g.transcribe_wav(self.wav(rate=24000), lambda _: None, 'synthetic-key',
                                   connect=lambda: self.fail('must not connect'))

    async def test_callback_failure_always_retires_transport_and_is_sanitized(self):
        for which in ('emit', 'usage'):
            socket = FakeSocket([final()])
            queue = asyncio.Queue()
            await queue.put(None)
            def broken(_):
                raise BrokenPipeError('synthetic-secret must not escape')
            with self.assertRaises(g.GoogleAsrError) as error:
                await g.run_session(queue, broken if which == 'emit' else lambda _: None,
                                    'synthetic-key', connect=lambda: socket,
                                    usage_callback=broken if which == 'usage' else None)
            self.assertNotIn('synthetic-secret', str(error.exception))
            self.assertTrue(socket.closed)

    async def test_early_receive_failure_retires_even_while_sender_waits(self):
        socket = FakeSocket()
        original = socket.recv
        count = 0
        async def recv():
            nonlocal count
            count += 1
            if count > 1:
                raise EOFError('synthetic private provider failure')
            return await original()
        socket.recv = recv
        with self.assertRaises(g.GoogleAsrError):
            await g.run_session(asyncio.Queue(), lambda _: None, 'synthetic-key', connect=lambda: socket)
        self.assertTrue(socket.closed)


if __name__ == '__main__':
    unittest.main()
