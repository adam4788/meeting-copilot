"""Direct Meta Model API route: allowlist, key hygiene, and answer-path wiring."""
import importlib
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def meta_module():
    return importlib.import_module('meta_models')


class MetaModelTests(unittest.TestCase):
    def setUp(self):
        fixture=patch('meta_models.resolve_model_api_key',side_effect=lambda **kw: os.environ.get('MODEL_API_KEY','').strip())
        fixture.start(); self.addCleanup(fixture.stop)

    def test_key_gating_and_allowlist(self):
        module = meta_module()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MODEL_API_KEY', None)
            self.assertFalse(module.api_key_present())
        with patch.dict(os.environ, {'MODEL_API_KEY': 'short'}):
            self.assertFalse(module.api_key_present())
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            self.assertTrue(module.api_key_present())
        self.assertEqual(module.validate_meta_model(None), 'muse-spark-1.3')
        self.assertEqual(module.validate_meta_model('muse-spark-1.2'), 'muse-spark-1.2')
        for bad in ('gpt-4', 'muse-spark-9.9', '', '../../etc', 123, {'m': 1}):
            with self.subTest(model=bad), self.assertRaises(ValueError):
                module.validate_meta_model(bad)

    def test_completion_posts_to_meta_with_bearer_and_parses_text(self):
        module = meta_module()
        payload = {'choices': [{'message': {'content': '  Grounded answer [1].  '}}]}
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode('utf-8')
        opener = MagicMock()
        opener.__enter__.return_value = response
        opener.__exit__.return_value = False
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen['url'] = request.full_url
            seen['auth'] = request.get_header('Authorization')
            seen['body'] = json.loads(request.data.decode('utf-8'))
            seen['timeout'] = timeout
            return opener

        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', side_effect=fake_urlopen):
                text = module.meta_completion('muse-spark-1.2')([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(text, 'Grounded answer [1].')
        self.assertEqual(seen['url'], 'https://api.meta.ai/v1/chat/completions')
        self.assertEqual(seen['auth'], 'Bearer test-key-12345')
        self.assertEqual(seen['body']['model'], 'muse-spark-1.2')
        self.assertEqual(seen['timeout'], 30)

    def test_failures_are_sanitized_and_never_leak_the_key(self):
        module = meta_module()
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            complete = module.meta_completion()
            with patch('meta_models._urlopen', side_effect=TimeoutError('slow')):
                with self.assertRaisesRegex(RuntimeError, '^Meta Model API request failed'):
                    complete([{'role': 'user', 'content': 'hi'}])
            empty = MagicMock()
            empty.read.return_value = json.dumps({'choices': []}).encode('utf-8')
            opener = MagicMock()
            opener.__enter__.return_value = empty
            opener.__exit__.return_value = False
            with patch('meta_models._urlopen', return_value=opener):
                with self.assertRaisesRegex(RuntimeError, 'no text answer'):
                    complete([{'role': 'user', 'content': 'hi'}])
            with self.assertRaisesRegex(ValueError, 'not configured'):
                with patch.dict(os.environ, {'MODEL_API_KEY': ''}):
                    module.meta_completion()

    def test_selection_routes_meta_without_hermes_catalog(self):
        selection = importlib.import_module('model_selection')
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with self.assertRaises(ValueError): selection.validate_selection('meta', None)
            self.assertEqual(selection.validate_selection('meta', 'muse-spark-1.1'), ('meta', 'muse-spark-1.1'))
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                selection.validate_selection('meta', 'gpt-4')
            with patch.object(selection, 'available_models', side_effect=AssertionError('must not consult Hermes')):
                complete = selection.selected_completion('meta', 'muse-spark-1.3')
        self.assertTrue(callable(complete))

    def test_catalog_advertises_only_bounded_byok_routes(self):
        import model_selection
        self.assertEqual({p['id'] for p in model_selection.available_models()['providers']}, {'openai','openrouter'})

    def test_no_automatic_meta_or_configured_model_submission(self):
        from knowledge import KnowledgeEngine
        engine=KnowledgeEngine(project_root='/nonexistent-test-root')
        engine.search=lambda *a,**k: []
        with patch('meta_models.meta_completion',side_effect=AssertionError('automatic Meta forbidden')), patch.object(engine,'_complete',side_effect=AssertionError('default fallback forbidden')):
            with self.assertRaises(ValueError):engine.answer('synthetic question')
            with self.assertRaises(ValueError):list(engine.answer_stream('synthetic question'))

    def test_completed_token_limit_is_an_error_not_success(self):
        import io
        raw=json.dumps({'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}).encode()
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-key'}),patch('meta_models._urlopen',return_value=io.BytesIO(raw)):
            with self.assertRaisesRegex(RuntimeError,'incomplete answer'):
                meta_module().meta_completion('muse-spark-1.3')([])

    def test_explicit_meta_selection_labels_route(self):
        knowledge = importlib.import_module('knowledge')
        engine = knowledge.KnowledgeEngine(project_root='/nonexistent-test-root')
        engine.search = lambda *a, **k: []
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models.meta_completion', return_value=lambda messages: 'Picked [1].'):
                result = engine.answer('quixotic zygomorphic fjordlake',
                                       answer_provider='meta', answer_model='muse-spark-1.2')
        self.assertEqual(result['model_route'], 'meta/muse-spark-1.2')


if __name__ == '__main__':
    unittest.main()


class MetaStreamingTests(unittest.TestCase):
    def setUp(self):
        fixture=patch('meta_models.resolve_model_api_key',side_effect=lambda **kw: os.environ.get('MODEL_API_KEY','').strip())
        fixture.start(); self.addCleanup(fixture.stop)

    def test_sse_deltas_stream_in_order_and_report_completion_honestly(self):
        module = meta_module()
        frames = [('data: {"choices":[{"delta":{"content":"Two "}}]}\n\n').encode(),
                  ('data: {"choices":[{"delta":{"content":"reviewers."}}]}\n\n').encode(),
                  b'data: [DONE]\n\n']

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def close(self):
                pass

        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeStream(frames)):
                chunks = list(module.meta_completion('muse-spark-1.2').stream_chunks(
                    [{'role': 'user', 'content': 'hi'}]))
        self.assertEqual(chunks, ['Two ', 'reviewers.'])

    def test_split_frames_reassemble_and_errors_stay_sanitized(self):
        module = meta_module()
        pieces = [b'data: {"choices":[{"delta":{"content":"Two', b' rev', b'iewers."}}]}\n\ndata: [DONE]\n\n']

        class FakeStream:
            def read(self, size=-1):
                return pieces.pop(0) if pieces else b''
            def close(self):
                pass

        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeStream()):
                chunks = list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
        self.assertEqual(chunks, ['Two reviewers.'])
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', side_effect=TimeoutError('slow')):
                with self.assertRaisesRegex(RuntimeError, '^Meta Model API request failed'):
                    list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))

    def test_completed_payload_without_deltas_reports_honestly(self):
        module = meta_module()

        class FakeCompleted:
            def read(self, size=-1):
                return b''
            def close(self):
                pass

        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeCompleted()):
                with self.assertRaisesRegex(NotImplementedError, 'streaming_unsupported'):
                    list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))

    def test_split_utf8_reassembles_across_reads(self):
        module = meta_module()
        raw = ('data: {"choices":[{"delta":{"content":"✓"}}]}\n\ndata: [DONE]\n\n').encode('utf-8')

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def close(self):
                pass

        # Every byte split position across the multi-byte sequence reassembles
        # to the intact character via the incremental decoder.
        for split in range(1, len(raw)):
            with self.subTest(split=split):
                with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
                    with patch('meta_models._urlopen', return_value=FakeStream([raw[:split], raw[split:]])):
                        chunks = list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
                self.assertEqual(chunks, ['✓'])

    def test_provider_error_frame_is_sanitized_failure(self):
        module = meta_module()

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def close(self):
                pass

        # Error-only stream: sanitized RuntimeError, never completed-only,
        # never echoing the provider message.
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeStream(
                    [b'data: {"error":{"message":"quota super-secret-key"}}\n\ndata: [DONE]\n\n'])):
                with self.assertRaisesRegex(RuntimeError, 'provider-error'):
                    list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
        # Error after a partial delta: still a failure, still sanitized.
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeStream(
                    [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                     b'data: {"error":{"message":"quota super-secret-key"}}\n\ndata: [DONE]\n\n'])):
                try:
                    list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
                except RuntimeError as exc:
                    self.assertIn('provider-error', str(exc))
                    self.assertNotIn('quota', str(exc))
                    self.assertNotIn('secret', str(exc))
                else:
                    self.fail('provider error after partial delta must raise')

    def test_invalid_byte_is_sanitized_and_response_closed(self):
        module = meta_module()
        secret = 'super-secret-key-12345'

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
                self.closed = False
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def close(self):
                self.closed = True

        stream = FakeStream([b'data: {"choices":[{"delta":{"content":"\xff' + secret.encode() + b'"}}]}\n\n'])
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=stream):
                try:
                    list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
                except RuntimeError as exc:
                    self.assertIn('no text answer', str(exc))
                    self.assertNotIn(secret, str(exc))
                    self.assertNotIn('\xff', str(exc))
                else:
                    self.fail('invalid byte must raise')
        self.assertTrue(stream.closed)

    def test_truncated_utf8_on_eof_is_sanitized_and_response_closed(self):
        module = meta_module()

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
                self.closed = False
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def close(self):
                self.closed = True

        # Truncated 3-byte sequence for U+2713 at EOF: incremental finalize
        # raises UnicodeDecodeError, which must surface sanitized.
        bad = b'data: {"choices":[{"delta":{"content":"a\xe2\x82'
        stream = FakeStream([bad])
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=stream):
                try:
                    list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
                except RuntimeError as exc:
                    self.assertIn('no text answer', str(exc))
                    self.assertNotIn('\xe2', str(exc))
                else:
                    self.fail('truncated UTF-8 at EOF must raise')
        self.assertTrue(stream.closed)

    def test_read1_delivers_first_delta_before_second_frame(self):
        import queue
        import threading
        module = meta_module()
        first = b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
        second = b'data: {"choices":[{"delta":{"content":"two"}}]}\n\ndata: [DONE]\n\n'
        release_second = threading.Event()

        class FakeRawHTTPResponse:
            """Mimics real urllib HTTPResponse: read1 returns promptly while
            plain read(4096) would block for the full amount."""
            def __init__(self):
                self.closed = False
                self.calls = []
                self._n = 0
            def read1(self, size=-1):
                self.calls.append('read1')
                self._n += 1
                if self._n == 1:
                    return first
                release_second.wait(timeout=10)
                return second
            def read(self, size=-1):
                self.calls.append('read')
                raise AssertionError('must use read1, not blocking read')
            def close(self):
                self.closed = True

        stream = FakeRawHTTPResponse()
        out = queue.Queue()
        errors = queue.Queue()

        def consume():
            try:
                for delta in module.meta_completion('muse-spark-1.2').stream_chunks(
                        [{'role': 'user', 'content': 'hi'}]):
                    out.put(delta)
            except Exception as exc:  # pragma: no cover - surfaced on join
                errors.put(exc)

        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=stream):
                worker = threading.Thread(target=consume, daemon=True)
                worker.start()
                try:
                    # First delta must arrive while the second frame is still
                    # blocked (provider not complete).
                    got = out.get(timeout=10)
                    self.assertEqual(got, 'one')
                finally:
                    release_second.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                rest = []
                while not out.empty():
                    rest.append(out.get_nowait())
                self.assertEqual(rest, ['two'])
                self.assertTrue(all(c == 'read1' for c in stream.calls))
                self.assertTrue(stream.closed)
                self.assertTrue(errors.empty())

    def test_stream_cancel_closes_response(self):
        module = meta_module()

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
                self.closed = False
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def close(self):
                self.closed = True

        frames = [b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
                  b'data: {"choices":[{"delta":{"content":"two"}}]}\n\ndata: [DONE]\n\n']
        stream = FakeStream(frames)
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=stream):
                gen = module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}])
                self.assertEqual(next(gen), 'one')
                gen.close()
        self.assertTrue(stream.closed)


class StreamBoundTests(unittest.TestCase):
    def setUp(self):
        fixture=patch('meta_models.resolve_model_api_key',side_effect=lambda **kw: os.environ.get('MODEL_API_KEY','').strip())
        fixture.start(); self.addCleanup(fixture.stop)

    def test_oversized_stream_truncates_with_explicit_signal(self):
        module = meta_module()
        big = 'x' * 70000
        frames = [f'data: {{"choices":[{{"delta":{{"content":"{big}"}}}}]}}\n\n'.encode()] * 5 + [b'data: [DONE]\n\n']

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def read1(self, size=-1):
                return self.read(size)
            def close(self):
                pass

        import json as _json
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeStream(frames)):
                gen = module.meta_completion('muse-spark-1.2').stream_chunks([{'role': 'user', 'content': 'hi'}])
                seen = []
                with self.assertRaisesRegex(NotImplementedError, 'truncated'):
                    for chunk in gen:
                        seen.append(chunk)
        self.assertTrue(seen)
        self.assertLess(sum(map(len, seen)), 400000)


class CompletedBoundTests(unittest.TestCase):
    def setUp(self):
        fixture=patch('meta_models.resolve_model_api_key',side_effect=lambda **kw: os.environ.get('MODEL_API_KEY','').strip())
        fixture.start(); self.addCleanup(fixture.stop)

    def test_oversized_completed_response_is_rejected(self):
        module = meta_module()

        class FakeBig:
            def read(self, size=-1):
                return b'x' * (262144 + 1) if size is None or size < 0 or size > 262144 else b'x' * size
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeBig()):
                with self.assertRaisesRegex(RuntimeError, 'size bound'):
                    module.meta_completion('muse-spark-1.2')([{'role': 'user', 'content': 'hi'}])


class TruncatedStreamTests(unittest.TestCase):
    def setUp(self):
        fixture=patch('meta_models.resolve_model_api_key',side_effect=lambda **kw: os.environ.get('MODEL_API_KEY','').strip())
        fixture.start(); self.addCleanup(fixture.stop)

    def test_eof_without_done_is_truncation_not_success(self):
        module = meta_module()

        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def read1(self, size=-1):
                return self.read(size)
            def close(self):
                pass

        frames = [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n']
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeStream(frames)):
                gen = module.meta_completion('muse-spark-1.2').stream_chunks([{'role': 'user', 'content': 'hi'}])
                seen = []
                with self.assertRaisesRegex(NotImplementedError, 'truncated'):
                    for chunk in gen:
                        seen.append(chunk)
        self.assertEqual(seen, ['partial'])


class MetaFinishReasonTests(unittest.TestCase):
    def setUp(self):
        fixture=patch('meta_models.resolve_model_api_key',side_effect=lambda **kw: os.environ.get('MODEL_API_KEY','').strip())
        fixture.start(); self.addCleanup(fixture.stop)

    def _stream(self, frames):
        module = meta_module()
        class FakeStream:
            def __init__(self, pieces):
                self._pieces = list(pieces)
            def read(self, size=-1):
                return self._pieces.pop(0) if self._pieces else b''
            def close(self):
                pass
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models._urlopen', return_value=FakeStream(frames)):
                gen = module.meta_completion('muse-spark-1.2').stream_chunks([{'role': 'user', 'content': 'hi'}])
                seen = []
                try:
                    for chunk in gen:
                        seen.append(chunk)
                except NotImplementedError as exc:
                    return seen, str(exc)
                return seen, None

    def test_length_limit_is_truncation_even_with_done(self):
        seen, err = self._stream([
            b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n',
            b'data: [DONE]\n\n'])
        self.assertEqual(seen, ['partial'])
        self.assertIn('length-limit', err or '')

    def test_stop_without_done_completes_cleanly(self):
        seen, err = self._stream([
            b'data: {"choices":[{"delta":{"content":"Two "}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"reviewers."},"finish_reason":"stop"}]}\n\n'])
        self.assertEqual(seen, ['Two ', 'reviewers.'])
        self.assertIsNone(err)
