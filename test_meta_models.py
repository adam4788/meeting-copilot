"""Direct Meta Model API route: allowlist, key hygiene, and answer-path wiring."""
import importlib
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def meta_module():
    return importlib.import_module('meta_models')


class MetaModelTests(unittest.TestCase):
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
            with patch('urllib.request.urlopen', side_effect=fake_urlopen):
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
            with patch('urllib.request.urlopen', side_effect=TimeoutError('slow')):
                with self.assertRaisesRegex(RuntimeError, '^Meta Model API request failed'):
                    complete([{'role': 'user', 'content': 'hi'}])
            empty = MagicMock()
            empty.read.return_value = json.dumps({'choices': []}).encode('utf-8')
            opener = MagicMock()
            opener.__enter__.return_value = empty
            opener.__exit__.return_value = False
            with patch('urllib.request.urlopen', return_value=opener):
                with self.assertRaisesRegex(RuntimeError, 'no text answer'):
                    complete([{'role': 'user', 'content': 'hi'}])
            with self.assertRaisesRegex(ValueError, 'not configured'):
                with patch.dict(os.environ, {'MODEL_API_KEY': ''}):
                    module.meta_completion()

    def test_selection_routes_meta_without_hermes_catalog(self):
        selection = importlib.import_module('model_selection')
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            self.assertEqual(selection.validate_selection('meta', None), ('meta', 'muse-spark-1.3'))
            self.assertEqual(selection.validate_selection('meta', 'muse-spark-1.1'), ('meta', 'muse-spark-1.1'))
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                selection.validate_selection('meta', 'gpt-4')
            with patch.object(selection, 'available_models', side_effect=AssertionError('must not consult Hermes')):
                complete = selection.selected_completion('meta', 'muse-spark-1.3')
        self.assertTrue(callable(complete))

    def test_catalog_advertises_meta_only_when_key_present(self):
        selection = importlib.import_module('model_selection')
        hermes = {'providers': [{'id': 'openai-codex', 'label': 'Codex',
                                 'models': [{'id': 'gpt-public', 'label': 'gpt-public'}]}]}
        # Drive through the real function with a stubbed Hermes backend.
        from types import SimpleNamespace
        models = SimpleNamespace(
            list_available_providers=lambda: [{'id': 'openai-codex', 'label': 'Codex', 'authenticated': True}],
            cached_provider_model_ids=Mock(return_value=['gpt-public']))
        with patch.dict(sys.modules, {'hermes_cli.models': models}):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('MODEL_API_KEY', None)
                without = selection.available_models()
            with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
                with_key = selection.available_models()
        self.assertEqual(without, hermes)
        meta = [p for p in with_key['providers'] if p['id'] == 'meta']
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0]['label'], 'Meta Model API')
        self.assertIn('muse-spark-1.3', [m['id'] for m in meta[0]['models']])
        self.assertNotIn('test-key-12345', json.dumps(with_key))

    def test_answer_defaults_to_meta_and_falls_back_visibly(self):
        knowledge = importlib.import_module('knowledge')
        engine = knowledge.KnowledgeEngine(project_root='/nonexistent-test-root')
        engine.search = lambda *a, **k: []
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models.meta_completion', return_value=lambda messages: 'Meta answer.'):
                engine._complete = Mock(side_effect=AssertionError('Hermes must not be used'))
                result = engine.answer('quixotic zygomorphic fjordlake')
        self.assertEqual(result['answer'], 'Meta answer.')
        self.assertEqual(result['model_route'], 'meta/muse-spark-1.3')
        engine._complete = lambda messages: 'Hermes answer.'
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('meta_models.meta_completion', return_value=Mock(side_effect=RuntimeError('down'))):
                result = engine.answer('quixotic zygomorphic fjordlake')
        self.assertEqual(result['answer'], 'Hermes answer.')
        self.assertTrue(any('Meta Model API unavailable' in w for w in result['warnings']))

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
            with patch('urllib.request.urlopen', return_value=FakeStream(frames)):
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
            with patch('urllib.request.urlopen', return_value=FakeStream()):
                chunks = list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
        self.assertEqual(chunks, ['Two reviewers.'])
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-key-12345'}):
            with patch('urllib.request.urlopen', side_effect=TimeoutError('slow')):
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
            with patch('urllib.request.urlopen', return_value=FakeCompleted()):
                with self.assertRaisesRegex(NotImplementedError, 'streaming_unsupported'):
                    list(module.meta_completion().stream_chunks([{'role': 'user', 'content': 'hi'}]))
