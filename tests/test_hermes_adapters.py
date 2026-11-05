"""Public ctx.llm adapter; synthetic facade only, no host or account lookup."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from hermes_adapters import default_completion, default_search

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
        for result in ('not json','{"success":false}','{"data":{"web":{}}}'):
            with self.subTest(result=result),self.assertRaises(RuntimeError):
                default_search(Mock(return_value=result))('synthetic')
