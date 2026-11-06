"""Public ctx.llm adapter; synthetic facade only, no host or account lookup."""
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from hermes_adapters import WebSearchUnavailable, default_completion, default_search, registry_search

class PublicCompletionTests(unittest.TestCase):
    def test_explicit_public_sync_facade_signature_and_result(self):
        facade=SimpleNamespace(complete=Mock(return_value=SimpleNamespace(text='  Synthetic answer.  ')))
        messages=[{'role':'user','content':'Synthetic question'}]
        with patch.dict('sys.modules',{'agent.auxiliary_client':None}):
            complete=default_completion(facade)
            self.assertEqual(complete(messages),'Synthetic answer.')
        facade.complete.assert_called_once_with(messages=messages,max_tokens=2048,timeout=30,purpose='meeting_copilot')
        self.assertFalse(hasattr(complete,'stream_chunks'))
    def test_missing_facade_cannot_implicitly_resolve_host_or_credentials(self):
        with patch.dict('sys.modules',{'agent':None}),patch('builtins.open',side_effect=AssertionError('no file reads')):
            with self.assertRaisesRegex(RuntimeError,'explicitly supply public ctx.llm'):
                default_completion()([])
    def test_failures_are_sanitized_and_never_retry_or_fallback(self):
        facade=SimpleNamespace(complete=Mock(side_effect=RuntimeError('synthetic-secret')))
        with self.assertRaisesRegex(RuntimeError,'^Hermes public completion failed$'):
            default_completion(facade)([])
        self.assertEqual(facade.complete.call_count,1)
        for text in ('',None,[], '  '):
            with self.subTest(text=text),self.assertRaisesRegex(RuntimeError,'No text answer'):
                default_completion(SimpleNamespace(complete=lambda **kw:SimpleNamespace(text=text)))([])


class PublicSearchTests(unittest.TestCase):
    def test_public_dispatch_exact_query_and_bounded_results(self):
        import json
        rows=[{'title':str(i),'url':'https://example.invalid/'+str(i)} for i in range(5)]
        dispatch=Mock(return_value=json.dumps({'success':True,'data':{'web':rows}}))
        with patch.dict('sys.modules',{'tools.web_tools':None}):
            self.assertEqual(default_search(dispatch)('synthetic public query'),rows[:3])
        dispatch.assert_called_once_with('web_search',{'query':'synthetic public query','limit':3})

    def test_missing_dispatch_never_discovers_context_or_files(self):
        with patch('builtins.open',side_effect=AssertionError('no context discovery')):
            with self.assertRaisesRegex(RuntimeError,'ctx.dispatch_tool'):
                default_search()('synthetic')

    def test_dispatch_failure_is_sanitized_without_retry(self):
        dispatch=Mock(side_effect=RuntimeError('synthetic-sensitive-error'))
        with self.assertRaisesRegex(RuntimeError,'^Hermes public web search failed$'):
            default_search(dispatch)('synthetic')
        dispatch.assert_called_once()

    def test_provider_failure_or_malformed_results_are_not_success(self):
        for result in ('not json','{"success":false}','{"error":"unknown tool"}',
                       '{"data":{"web":[]}}','{"success":true}',
                       '{"success":true,"data":{}}','{"success":true,"data":{"web":{}}}'):
            with self.subTest(result=result),self.assertRaises(RuntimeError):
                default_search(Mock(return_value=result))('synthetic')


class RegistrySearchTests(unittest.TestCase):
    def setUp(self):
        self.registry = SimpleNamespace(get_entry=Mock(return_value=object()),
                                        dispatch=Mock(return_value='{"success":true,"data":{"web":[{"url":"https://example.invalid"}]}}'))
        self.scope = {'value': {'OPENAI_API_KEY': 'synthetic'}}
        self.home = {'value': 'profile-a'}
        agent = types.ModuleType('agent'); agent.__path__ = []
        secrets = types.ModuleType('agent.secret_scope')
        secrets.current_secret_scope = lambda: self.scope['value']
        secrets.current_secret_scope_home = lambda: self.home['value']
        tools = types.ModuleType('tools'); tools.__path__ = []
        tool_registry = types.ModuleType('tools.registry'); tool_registry.registry = self.registry
        constants = types.ModuleType('hermes_constants')
        constants.hermes_home_key = lambda home=None: home or 'profile-a'
        modules = {'agent': agent, 'agent.secret_scope': secrets, 'tools': tools,
                   'tools.registry': tool_registry, 'tools.web_tools': None,
                   'hermes_constants': constants}
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start(); self.addCleanup(self.modules.stop)

    def test_public_registry_uses_bound_profile_and_exact_query(self):
        self.assertEqual(registry_search('public query'), [{'url':'https://example.invalid'}])
        self.registry.get_entry.assert_called_once_with('web_search', scope='profile-a')
        self.registry.dispatch.assert_called_once_with(
            'web_search', {'query':'public query', 'limit':3}, scope='profile-a')

    def test_unbound_mismatched_or_missing_registry_is_unavailable(self):
        for scope, home, entry in ((None, 'profile-a', object()),
                                   ({}, None, object()),
                                   ({}, 'profile-b', object()),
                                   ({}, 'profile-a', None)):
            with self.subTest(scope=scope, home=home, entry=entry):
                self.scope['value'], self.home['value'] = scope, home
                self.registry.get_entry.return_value = entry
                with self.assertRaises(WebSearchUnavailable):
                    registry_search('public query')
        self.registry.dispatch.assert_not_called()

    def test_failed_dispatch_is_sanitized_and_not_retried(self):
        self.registry.dispatch.side_effect = RuntimeError('synthetic-sensitive')
        with self.assertRaisesRegex(RuntimeError, '^Hermes public web search failed$'):
            registry_search('public query')
        self.registry.dispatch.assert_called_once()

    def test_missing_public_registry_does_not_load_private_web_tools(self):
        with patch.dict(sys.modules, {'tools.registry': None}):
            with self.assertRaises(WebSearchUnavailable):
                registry_search('public query')
        self.registry.dispatch.assert_not_called()
