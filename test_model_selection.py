"""Per-request answer routing, with Hermes boundaries faked (no credentials/network)."""
import importlib
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class ModelSelectionTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('model_selection')
        except ModuleNotFoundError:
            self.fail('model_selection must provide the safe Hermes model catalog')

    def test_catalog_lists_only_authenticated_concrete_providers_and_safe_fields(self):
        models = SimpleNamespace(
            list_available_providers=lambda: [
                {'id': 'openai-codex', 'label': 'Codex', 'authenticated': True, 'api_key': 'hidden'},
                {'id': 'anthropic', 'label': 'Anthropic', 'authenticated': False},
                {'id': 'auto', 'label': 'Automatic', 'authenticated': True},
                {'id': 'custom', 'label': 'Custom', 'authenticated': True},
                {'id': 'lmstudio', 'label': 'Local alias', 'authenticated': True},
                {'id': 'moa', 'label': 'Mixture', 'authenticated': True},
                {'id': 'openai-codex', 'label': 'Duplicate', 'authenticated': True}],
            cached_provider_model_ids=Mock(return_value=['gpt-public', 'gpt-public', 'https://secret', 'sk-credentialvalue123456789']))
        with patch.dict(sys.modules, {'hermes_cli.models': models}):
            result = self.module().available_models()
        self.assertEqual(result, {'providers': [{'id': 'openai-codex', 'label': 'Codex',
                                                'models': [{'id': 'gpt-public', 'label': 'gpt-public'}]}]})
        models.cached_provider_model_ids.assert_called_once_with('openai-codex')
        self.assertNotIn('hidden', json.dumps(result))

    def test_validation_requires_exact_catalog_pair_and_rejects_unsafe_before_lookup(self):
        module = self.module()
        catalog = {'providers': [{'id': 'openai-codex', 'models': [{'id': 'gpt-public'}]}]}
        with patch.object(module, 'available_models', return_value=catalog) as lookup:
            self.assertEqual(module.validate_selection(None, None), (None, None))
            lookup.assert_not_called()
            invalid = [('', ''), ('openai-codex', None), (None, 'gpt-public'),
                       ('auto', 'gpt-public'), ('https://host', 'gpt-public'),
                       ('openai-codex', 'sk-' + 'x' * 30), ('openai-codex', 'eyJ' + 'x' * 50),
                       ('openai-codex', 'x' * 201), ([], 'gpt-public'),
                       ('openai-codex', {'base_url': 'https://host'}),
                       ('openai-codex', ' gpt-public'), ('openai-codex', 'gpt-public\n')]
            for provider, model in invalid:
                with self.subTest(provider=provider, model=model), self.assertRaises(ValueError) as error:
                    module.validate_selection(provider, model)
                self.assertEqual(str(error.exception), 'Invalid answer model selection')
            lookup.assert_not_called()
            self.assertEqual(module.validate_selection('openai-codex', 'gpt-public'), ('openai-codex', 'gpt-public'))
            for pair in [('openai-codex', 'unknown'), ('unknown', 'gpt-public')]:
                with self.assertRaisesRegex(ValueError, '^Answer model selection is unavailable$'):
                    module.validate_selection(*pair)

    def test_catalog_failures_are_empty_and_sanitized(self):
        models = SimpleNamespace(list_available_providers=Mock(side_effect=RuntimeError('sk-private https://private')),
                                 cached_provider_model_ids=Mock(return_value=[]))
        with patch.dict(sys.modules, {'hermes_cli.models': models}):
            self.assertEqual(self.module().available_models(), {'providers': []})
        models.list_available_providers = lambda: [{'id': 'openai-codex', 'authenticated': True}]
        models.cached_provider_model_ids = Mock(side_effect=RuntimeError('sk-private https://private'))
        with patch.dict(sys.modules, {'hermes_cli.models': models}):
            self.assertEqual(self.module().available_models(), {'providers': []})

    def test_empty_selected_response_is_a_failure_not_a_blank_answer(self):
        module = self.module()
        for text in [None, '', '   ', []]:
            create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))]))
            auxiliary = SimpleNamespace(resolve_provider_client=lambda **kwargs: (SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), 'gpt-public'),
                                        _build_call_kwargs=lambda *args, **kwargs: {})
            with patch.dict(sys.modules, {'agent.auxiliary_client': auxiliary}), patch.object(module, 'validate_selection', return_value=('openai-codex', 'gpt-public')):
                with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
                    module.selected_completion('openai-codex', 'gpt-public')([])

    def test_explicit_completion_uses_resolver_and_wire_builder_without_fallback(self):
        module = self.module()
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=' selected answer '))])
        create = Mock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), base_url='https://configured.private')
        auxiliary = SimpleNamespace(resolve_provider_client=Mock(return_value=(client, 'gpt-public')),
                                    _build_call_kwargs=Mock(return_value={'model': 'gpt-public', 'messages': [], 'max_completion_tokens': 600}),
                                    call_llm=Mock(side_effect=AssertionError('fallback forbidden')))
        with patch.dict(sys.modules, {'agent.auxiliary_client': auxiliary}), patch.object(module, 'validate_selection', return_value=('openai-codex', 'gpt-public')):
            complete = module.selected_completion('openai-codex', 'gpt-public')
            self.assertEqual(complete([{'role': 'user', 'content': 'public prompt'}]), 'selected answer')
            auxiliary.resolve_provider_client.assert_called_once_with(provider='openai-codex', model='gpt-public')
            wire = auxiliary._build_call_kwargs.call_args
            self.assertEqual(wire.args[0:2], ('openai-codex', 'gpt-public'))
            self.assertEqual(wire.kwargs['max_tokens'], 600)
            self.assertEqual(wire.kwargs['timeout'], 30)
            create.assert_called_once_with(model='gpt-public', messages=[], max_completion_tokens=600)
            auxiliary.call_llm.assert_not_called()
            create.side_effect = RuntimeError('sk-private https://private.endpoint')
            with self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
                complete([])
            auxiliary.resolve_provider_client.return_value = (client, 'different-model')
            with self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
                complete([])
            auxiliary.resolve_provider_client.return_value = (None, None)
            with self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
                complete([])
            self.assertEqual(create.call_count, 2)
            auxiliary.call_llm.assert_not_called()


class KnowledgeSelectionTests(unittest.TestCase):
    def engine(self):
        from knowledge import KnowledgeEngine
        engine = KnowledgeEngine()
        engine.search = Mock(return_value=[])
        engine.projects = Mock(return_value=[])
        engine._complete = Mock(return_value='default answer')
        return engine

    def test_default_keeps_one_argument_injected_completion_for_planner_and_answer(self):
        engine = self.engine()
        def plan(question, transcript, sources, projects, complete):
            self.assertIs(complete, engine._complete)
            complete([{'role': 'user', 'content': 'planner'}])
            return {'needed': False, 'reason': 'not_needed'}
        with patch('knowledge.plan_web', side_effect=plan), patch('model_selection.selected_completion') as selected:
            result = engine.answer('public', web_fallback=True, answer_provider=None, answer_model=None)
        self.assertEqual(result['answer'], 'default answer')
        self.assertEqual(engine._complete.call_count, 2)
        selected.assert_not_called()

    def test_concurrent_selected_and_default_requests_never_share_routes(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        engine = self.engine()
        original = engine._complete
        barrier = threading.Barrier(3)
        calls = []
        lock = threading.Lock()
        def resolver(provider, model):
            def create(**kwargs):
                with lock:
                    calls.append((provider, model, kwargs['model'], kwargs['messages'][0]['content']))
                text = f'{provider}/{model}'
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), model
        def builder(provider, model, messages, **kwargs):
            return {'model': model, 'messages': messages}
        def planner(question, transcript, sources, projects, complete):
            barrier.wait(timeout=5)
            complete([{'role': 'user', 'content': 'planner:' + question}])
            barrier.wait(timeout=5)
            return {'needed': False, 'reason': 'not_needed'}
        auxiliary = SimpleNamespace(resolve_provider_client=resolver, _build_call_kwargs=builder,
                                    call_llm=Mock(side_effect=AssertionError('no default routing')))
        with patch.dict(sys.modules, {'agent.auxiliary_client': auxiliary}), patch('model_selection.validate_selection', side_effect=lambda p, m: (p, m)), patch('knowledge.plan_web', side_effect=planner):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(engine.answer, model, web_fallback=True, answer_provider=provider, answer_model=model)
                           for provider, model in [('provider-one', 'model-one'), ('provider-two', 'model-two')]]
                futures.append(pool.submit(engine.answer, 'default', web_fallback=True))
                results = [future.result(timeout=10) for future in futures]
        self.assertEqual([r['answer'] for r in results], ['provider-one/model-one', 'provider-two/model-two', 'default answer'])
        self.assertIs(engine._complete, original)
        self.assertEqual(original.call_count, 2)
        self.assertEqual(len(calls), 4)
        for provider, model, wire_model, prompt in calls:
            self.assertEqual(model, wire_model)
            self.assertEqual(provider[-3:], model[-3:])
        self.assertEqual({row[3] for row in calls if row[3].startswith('planner:')}, {'planner:model-one', 'planner:model-two'})

    def test_invalid_selection_is_rejected_before_retrieval_and_failure_never_uses_default(self):
        engine = self.engine()
        with patch('model_selection.available_models', return_value={'providers': []}):
            with self.assertRaises(ValueError):
                engine.answer('public', answer_provider='openai-codex', answer_model='unknown')
        engine.search.assert_not_called()
        with patch('model_selection.selected_completion', return_value=Mock(side_effect=RuntimeError('sanitized'))):
            result = engine.answer('public', answer_provider='openai-codex', answer_model='gpt-public')
        self.assertIn('unavailable', result['answer'])
        self.assertTrue(any('Model request failed (RuntimeError)' in w for w in result['warnings']))
        engine._complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
