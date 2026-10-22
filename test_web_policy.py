"""Synthetic-only tests for automatic public-search planning."""
import json
import unittest

from web_policy import plan_web


class WebPolicyTests(unittest.TestCase):
    def plan(self, output, **kwargs):
        calls = []

        def complete(messages):
            calls.append(messages)
            return output

        result = plan_web(kwargs.get('question', 'How does PostgreSQL replication work?'),
                          kwargs.get('transcript', ''), kwargs.get('sources', []),
                          kwargs.get('blocked_terms', []), complete)
        self.assertEqual(len(calls), 1)
        return result, calls[0]

    def test_public_fallback(self):
        result, _ = self.plan('{"needs_web":true,"query":"PostgreSQL logical replication limitations"}')
        self.assertEqual(result, {'needed': True,
                                  'query': 'PostgreSQL logical replication limitations',
                                  'reason': 'public_query'})

    def test_private_question_has_no_public_query(self):
        result, _ = self.plan('{"needs_web":true,"query":""}',
                              question='Has synthetic client signed our internal approval?')
        self.assertEqual(result, {'needed': False, 'query': None, 'reason': 'no_public_query'})

    def test_invalid_output_fails_closed(self):
        for output in ('not json', '```json\n{}\n```', '{}', '[]', 'null',
                       '{"needs_web":"true","query":"Redis documentation"}',
                       '{"needs_web":true,"query":null}',
                       '{"needs_web":true,"query":"Redis","extra":1}',
                       '{"needs_web":true,"needs_web":false,"query":"Redis"}'):
            with self.subTest(output=output):
                result, _ = self.plan(output)
                self.assertEqual(result, {'needed': False, 'query': None,
                                          'reason': 'planner_unavailable'})

    def test_model_error_fails_closed(self):
        calls = []
        def unavailable(messages):
            calls.append(messages)
            raise OSError('synthetic network failure')
        result = plan_web('Redis help', '', [], [], unavailable)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, {'needed': False, 'query': None,
                                  'reason': 'planner_unavailable'})

    def test_sensitive_queries_rejected(self):
        queries = [
            'Redis https://example.com', 'Redis example.com', 'Redis a@sample.test',
            'Redis 192.168.1.23', 'Redis 2001:db8::1', 'Redis ::1',
            'Redis 12345678-1234-1234-1234-123456789abc', 'Redis 123456789',
            'Redis db01.internal', 'Redis my-private-host', 'Redis localhost',
            'Redis prod-db01', 'Redis srv27', 'Redis AKIAIOSFODNN7EXAMPLE',
            r'Redis C:\synthetic\secret.txt', 'Redis /etc/synthetic',
            r'Redis \\fileserver\share', 'Redis ~/synthetic',
            'Redis password=synthetic', 'Redis api_key synthetic',
            'Redis sk-abcdefghijklmnopqrstuv', 'Redis Bearer abcdefghijklmnop',
            'Redis abcDEF0123456789abcDEF0123456789',
            'Redis ProjectNOVA deployment', 'Redis nova migration',
            'Redis ' + 'x' * 201, ' '.join(['Redis'] * 25),
            'Redis\nignore policy', 'Redis \u200bProjectNova',
        ]
        for query in queries:
            with self.subTest(query=query):
                result, _ = self.plan(json.dumps({'needs_web': True, 'query': query}),
                                      blocked_terms=['ProjectNova', 'Project Nova'])
                self.assertEqual(result, {'needed': False, 'query': None,
                                          'reason': 'unsafe_query'})

    def test_public_products_not_overblocked(self):
        for query in ('PostgreSQL 16 logical replication', 'OAuth 2.0 PKCE',
                      'ServiceNow CMDB IRE', 'HTTP 503 retries', 'Node.js event loop'):
            with self.subTest(query=query):
                result, _ = self.plan(json.dumps({'needs_web': True, 'query': query}))
                self.assertTrue(result['needed'])

    def test_bounded_untrusted_payload_without_paths(self):
        sources = [{'title': 'Title' * 1000, 'text': 'T' * 4000,
                    'source': 'SECRET_METADATA_PATH', 'id': 'PRIVATE_ID'} for _ in range(9)]
        sources[0]['title'] = r'C:\synthetic\private\memo.txt'
        sources[0]['text'] = 'Read /synthetic/private/memo.txt for notes'
        _, messages = self.plan('{"needs_web":false,"query":""}',
                                question='Q' * 3000, transcript='OLD' + 'R' * 7000,
                                sources=sources)
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        payload = json.loads(messages[1]['content'])
        self.assertEqual(len(payload['question']), 2000)
        self.assertEqual(payload['transcript'], 'R' * 6000)
        self.assertEqual(len(payload['sources']), 6)
        for source in payload['sources']:
            self.assertEqual(set(source), {'title', 'text'})
            self.assertLessEqual(len(source['text']), 2050)
            self.assertLessEqual(len(source['title']), 200)
        serialized = messages[1]['content']
        for marker in ('SECRET_METADATA_PATH', 'PRIVATE_ID', 'memo.txt', 'synthetic'):
            self.assertNotIn(marker, serialized)
        prompt = messages[0]['content'].lower()
        for instruction in ('untrusted', 'cross-project', 'approvals', 'public',
                            'embedded instructions', 'person', 'client', 'raw transcript'):
            self.assertIn(instruction, prompt)

    def test_sufficient_local_evidence(self):
        result, _ = self.plan('{"needs_web":false,"query":""}')
        self.assertEqual(result, {'needed': False, 'query': None,
                                  'reason': 'local_sufficient'})


if __name__ == '__main__':
    unittest.main()
