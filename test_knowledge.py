import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from knowledge import KnowledgeEngine


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        self.project = self.projects / 'alpha'
        self.project.mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()
        self.engine = KnowledgeEngine(self.projects, self.home)

    def test_default_project_root_finds_home_projects_on_mac(self):
        with patch.dict(os.environ, {'MEETING_COPILOT_PROJECT_ROOT': ''}), patch('knowledge.Path.home', return_value=self.root):
            engine = KnowledgeEngine(hermes_home=self.home)
        self.assertEqual(engine.project_root, self.projects.resolve())
        self.assertEqual(engine.projects()[0]['name'], 'alpha')

    def test_existing_documents_projects_keeps_precedence(self):
        legacy = self.root / 'Documents' / 'Projects'
        legacy.mkdir(parents=True)
        with patch.dict(os.environ, {'MEETING_COPILOT_PROJECT_ROOT': ''}), patch('knowledge.Path.home', return_value=self.root):
            self.assertEqual(KnowledgeEngine(hermes_home=self.home).project_root, legacy.resolve())

    def test_selected_project_text_search(self):
        (self.project / 'README.md').write_text('Orchid deployment needs a blue approval.', encoding='utf-8')
        self.assertEqual(self.engine.projects()[0]['name'], 'alpha')
        self.assertEqual(self.engine.search('Orchid', project='alpha')[0]['text'], 'Orchid deployment needs a blue approval.')
        self.assertEqual(self.engine.search('Orchid'), [])

    def test_exclusions_redaction_and_cached_index(self):
        (self.project / '.private').mkdir()
        (self.project / '.private' / 'notes.md').write_text('Orchid hidden')
        (self.project / 'auth.txt').write_text('Orchid secret')
        (self.project / 'config.md').write_text('Orchid configuration')
        (self.project / 'notes.txt').write_text('Orchid decision. API_KEY=supersecretvalue')
        hits = self.engine.search('Orchid', 'alpha')
        self.assertEqual(len(hits), 1)
        self.assertNotIn('supersecretvalue', hits[0]['text'])
        with patch.object(Path, 'read_text', side_effect=AssertionError('must cache')):
            self.assertEqual(self.engine.search('Orchid', 'alpha'), hits)
        with self.assertRaises(ValueError):
            self.engine.search('Orchid', '../hermes')

    def test_sessions_readonly_excludes_tools_and_labels_claims(self):
        db = sqlite3.connect(self.home / 'state.db')
        db.executescript('CREATE TABLE sessions(id TEXT,title TEXT,started_at REAL,profile_name TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,timestamp REAL); CREATE VIRTUAL TABLE messages_fts USING fts5(content);')
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', ('s1', 'Synthetic notes', 1, None))
        for i, role, text, calls in [(1, 'user', 'Orchid requested review', None), (2, 'assistant', 'Orchid shipped', None), (3, 'tool', 'Orchid hidden tool result', None), (4, 'assistant', 'Orchid tool payload', '[]')]:
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (i, 's1', role, text, calls, 1))
            db.execute('INSERT INTO messages_fts(rowid,content) VALUES (?,?)', (i, text))
        db.commit()
        db.close()
        before = (self.home / 'state.db').read_bytes()
        hits = self.engine.search('Orchid')
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h['source'].startswith('session:') for h in hits))
        self.assertTrue(any('unverified assistant statement' in h['text'] for h in hits))
        self.assertEqual(before, (self.home / 'state.db').read_bytes())

    def test_grounded_answer_bounds_and_network_consent(self):
        (self.project / 'README.md').write_text('Orchid requires review.', encoding='utf-8')
        captured = []
        def complete(messages):
            captured.extend(messages)
            return 'Review is required [1].'
        with patch.object(self.engine, '_complete', side_effect=complete), patch.object(self.engine, '_web', side_effect=AssertionError('no consent')):
            result = self.engine.answer('What does Orchid require?', transcript='x' * 30000, project='alpha')
        self.assertEqual(result['answer'], 'Review is required [1].')
        self.assertTrue(result['sources'])
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        self.assertLess(len(captured[1]['content']), 24000)
        self.assertIn('untrusted', captured[0]['content'])
        self.assertNotIn('supersecretvalue', str(captured))

    def test_web_only_exact_explicit_query_after_local_and_failure_safe(self):
        order = []
        def search(*args, **kwargs):
            order.append('local')
            return []
        def web(query):
            order.append(query)
            return []
        with patch.object(self.engine, 'search', side_effect=search), patch.object(self.engine, '_web', side_effect=web), patch.object(self.engine, '_complete', return_value='Insufficient evidence.'):
            self.engine.answer('private question', transcript='private transcript', web_query='exact public query')
        self.assertEqual(order, ['local', 'exact public query'])
        with patch.object(self.engine, '_web', side_effect=RuntimeError('sensitive error')), patch.object(self.engine, '_complete', side_effect=RuntimeError('sensitive error')):
            result = self.engine.answer('Orchid', web_query='public')
        self.assertNotIn('sensitive error', str(result))
        self.assertEqual(len([w for w in result['warnings'] if 'failed' in w]), 2)

    def test_other_profile_denied_and_generic_discovery(self):
        with self.assertRaises(ValueError):
            KnowledgeEngine(self.projects, self.root / 'profiles' / 'other')
        with patch.dict(os.environ, {'MEETING_COPILOT_PROJECT_ROOT': str(self.projects), 'HERMES_HOME': str(self.home)}):
            engine = KnowledgeEngine()
        self.assertEqual(engine.project_root, self.projects.resolve())
        self.assertEqual(engine.hermes_home, self.home.resolve())

    def test_complete_selected_chunk_is_sent_not_silently_truncated(self):
        (self.project / 'note.md').write_text('Orchid ' + 'x' * 1700 + ' supported format is RST.', encoding='utf-8')
        captured = []
        def complete(messages):
            captured.extend(messages)
            return 'RST [1].'
        with patch.object(self.engine, '_complete', side_effect=complete):
            self.engine.answer('Orchid', project='alpha')
        self.assertIn('supported format is RST', captured[1]['content'])

    def test_nondefault_active_profile_is_rejected(self):
        with patch.dict(os.environ, {'HERMES_PROFILE_NAME': 'other'}):
            with self.assertRaises(ValueError):
                KnowledgeEngine(self.projects, self.home)

    def test_model_adapter_does_not_offer_tools(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        call = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Grounded [1].'))]))
        fake = SimpleNamespace(call_llm=call)
        with patch.dict(sys.modules, {'agent': SimpleNamespace(), 'agent.auxiliary_client': fake}):
            self.assertEqual(self.engine._complete([]), 'Grounded [1].')
        self.assertIsNone(call.call_args.kwargs['tools'])
        self.assertEqual(call.call_args.kwargs['timeout'], 30)

    def test_all_scope_discloses_cross_project_context_and_limits(self):
        (self.project / 'notes.md').write_text('Orchid approval.', encoding='utf-8')
        with patch.object(self.engine, '_complete', return_value='Approval [1].'):
            result = self.engine.answer('Orchid', all_projects=True)
        self.assertTrue(any('100' in w and 'unrelated projects' in w for w in result['warnings']))

    def test_all_scope_preserves_exclusions_and_sessions(self):
        self.test_sessions_readonly_excludes_tools_and_labels_claims()
        beta = self.projects / 'beta'
        beta.mkdir()
        (beta / 'notes.md').write_text('Orchid public approval. API_KEY=syntheticvalue', encoding='utf-8')
        (self.project / 'notes.md').write_text('Orchid alpha approval.', encoding='utf-8')
        (beta / 'auth.txt').write_text('Orchid excluded secret', encoding='utf-8')
        private = self.projects / '.hidden'
        private.mkdir()
        (private / 'notes.md').write_text('Orchid excluded hidden', encoding='utf-8')
        (self.root / 'outside.md').write_text('Orchid excluded outside', encoding='utf-8')
        hits = self.engine.search('Orchid', all_projects=True)
        self.assertEqual(len(hits), 4)
        self.assertTrue(any(h['source'].startswith('session:') for h in hits))
        self.assertFalse(any('excluded' in h['text'] or 'syntheticvalue' in h['text'] for h in hits))
        self.assertEqual({Path(h['source']).parent.name for h in hits if not h['source'].startswith('session:')}, {'alpha', 'beta'})
        self.assertEqual(len(self.engine.search('Orchid', all_projects=True, limit=1)), 1)
        with self.assertRaises(ValueError):
            self.engine.search('Orchid', project='alpha', all_projects=True)

    def test_all_scope_bounds_project_count(self):
        for i in range(101):
            (self.projects / f'project-{i:03}').mkdir()
        with patch.object(self.engine, '_files', return_value=[]) as scan:
            self.engine.search('Orchid', all_projects=True)
        self.assertEqual(scan.call_count, 100)

    def _fallback(self, planner, **kw):
        (self.project / 'notes.md').write_text('Orchid approval.', encoding='utf-8')
        web = [dict(id='w', title='Doc', source='https://example.com', text='Public doc', updated_at=None)]
        answers = iter([planner, 'Answer [1].'])
        with patch.object(self.engine, '_complete', side_effect=lambda m: next(answers)),              patch.object(self.engine, '_web', return_value=web) as search:
            return self.engine.answer('Orchid', web_fallback=True, **kw), search

    def test_credentials_redacted_from_planner_and_answer_payloads(self):
        question = 'Explain Kubernetes ingress. password=syntheticQuestionPassword API_KEY=syntheticQuestionApiToken'
        transcript = 'Discuss public routing. password=syntheticTranscriptPassword access_token=syntheticTranscriptApiToken'
        secrets = ('syntheticQuestionPassword', 'syntheticQuestionApiToken',
                   'syntheticTranscriptPassword', 'syntheticTranscriptApiToken')
        with patch.object(self.engine, '_complete', side_effect=[
                '{"needs_web":false,"query":""}', 'Insufficient evidence.']) as complete, \
                patch.object(self.engine, '_web', side_effect=AssertionError('no web search')) as web:
            result = self.engine.answer(question, transcript=transcript, web_fallback=True)
        self.assertEqual(result['answer'], 'Insufficient evidence.')
        self.assertFalse(result['web_search']['used'])
        web.assert_not_called()
        self.assertEqual(complete.call_count, 2)
        for stage, call in zip(('planner', 'answer'), complete.call_args_list):
            with self.subTest(stage=stage):
                messages = call.args[0]
                payload = json.loads(messages[1]['content'])
                for secret in secrets:
                    self.assertNotIn(secret, json.dumps(messages))
                self.assertEqual(payload['question'],
                                 'Explain Kubernetes ingress. password=[REDACTED] API_KEY=[REDACTED]')
                self.assertEqual(payload['transcript'],
                                 'Discuss public routing. password=[REDACTED] access_token=[REDACTED]')

    def test_automatic_web_fallback_uses_planned_public_query(self):
        result, search = self._fallback('{"needs_web":true,"query":"Kubernetes ingress annotations"}')
        search.assert_called_once_with('Kubernetes ingress annotations')
        self.assertEqual(result['web_search'], {'used': True, 'mode': 'automatic', 'query': 'Kubernetes ingress annotations'})

    def test_automatic_web_fallback_skips_when_local_sufficient_or_planner_fails(self):
        for planner in ('{"needs_web":false,"query":""}', 'not json'):
            result, search = self._fallback(planner)
            search.assert_not_called()
            self.assertFalse(result['web_search']['used'])

    def test_explicit_query_overrides_automatic_planner(self):
        (self.project / 'notes.md').write_text('Orchid approval.', encoding='utf-8')
        with patch.object(self.engine, '_complete', return_value='A [1].') as llm, patch.object(self.engine, '_web', return_value=[]) as search:
            result = self.engine.answer('Orchid', web_query='public docs', web_fallback=True)
        search.assert_called_once_with('public docs')
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(result['web_search']['mode'], 'explicit')

    def test_hidden_project_cannot_be_selected(self):
        hidden = self.projects / '.private'
        hidden.mkdir()
        (hidden / 'notes.md').write_text('Orchid hidden')
        with self.assertRaises(ValueError):
            self.engine.search('Orchid', '.private')


if __name__ == '__main__':
    unittest.main()


class AnswerStreamTests(KnowledgeTests):
    def test_answer_stream_reuses_retrieval_and_yields_provider_deltas(self):
        self.project.mkdir(exist_ok=True)
        (self.project / 'notes.md').write_text('Orchid requires review.', encoding='utf-8')
        deltas = iter(['Orchid ', 'requires review [1].'])
        header = {'model_route': 'fake/test', 'sources': [], 'warnings': [],
                  'web_search': {'used': False}}
        complete = Mock(return_value='unused')
        complete.stream_chunks = Mock(return_value=deltas)
        with patch.object(self.engine, '_complete', complete):
            out_header, chunks = self.engine.answer_stream('What does Orchid require?', project='alpha')
            received = list(chunks)
        self.assertEqual(received, ['Orchid ', 'requires review [1].'])
        self.assertEqual(out_header['model_route'], 'hermes-configured')

    def test_answer_stream_completed_only_route_raises_honestly(self):
        def plain(messages):
            return 'Final.'
        with patch.object(KnowledgeEngine, '_complete', plain):
            engine = KnowledgeEngine(self.projects, self.home)
            with self.assertRaisesRegex(NotImplementedError, 'streaming_unsupported'):
                engine.answer_stream('What does Orchid require?')

    def test_answer_stream_header_carries_citations_and_no_paid_duplicate(self):
        import json as _json
        self.project.mkdir(exist_ok=True)
        (self.project / 'notes.md').write_text('Orchid requires review.', encoding='utf-8')
        deltas = iter(['Orchid ', 'requires review [1].'])
        complete = Mock(return_value='unused')
        complete.stream_chunks = Mock(return_value=deltas)
        with patch.object(self.engine, '_complete', complete):
            out_header, chunks = self.engine.answer_stream(
                'What does Orchid require?', project='alpha', web_fallback=True)
            received = list(chunks)
        self.assertEqual(received, ['Orchid ', 'requires review [1].'])
        self.assertEqual(out_header['model_route'], 'hermes-configured')
        self.assertTrue(out_header['streaming'])
        self.assertFalse(out_header['completed_only'])
        self.assertTrue(out_header['sources'], 'streaming must carry retrieval citations')
        self.assertIn('web_search', out_header)
        self.assertIn('elapsed_seconds', out_header)
        # Same single-paid-call contract as answer(): one planner call at most
        # (it returns local_sufficient here), then exactly one streaming model
        # invocation and zero non-streaming complete() answer calls.
        planner_mark = 'Decide whether the supplied local project/session evidence'
        planner_calls = [c for c in complete.call_args_list
                         if planner_mark in str(c.args[0][0].get('content', ''))]
        self.assertEqual(len(planner_calls), 1)
        answer_calls = [c for c in complete.call_args_list if c not in planner_calls]
        self.assertEqual(answer_calls, [])
        complete.stream_chunks.assert_called_once()
        messages = complete.stream_chunks.call_args.args[0]
        payload = _json.loads(messages[1]['content'])
        self.assertTrue(any('Orchid requires review' in s.get('text', '')
                            for s in payload['evidence']))

    def test_answer_reports_streaming_capability_honestly(self):
        self.project.mkdir(exist_ok=True)
        (self.project / 'notes.md').write_text('Orchid requires review.', encoding='utf-8')
        streaming = Mock(return_value='Review is required [1].')
        streaming.stream_chunks = Mock(return_value=iter(['x']))
        with patch.object(self.engine, '_complete', streaming), patch.object(self.engine, '_web', side_effect=AssertionError('no consent')):
            result = self.engine.answer('What does Orchid require?', project='alpha')
        self.assertTrue(result['streaming'])
        self.assertFalse(result['completed_only'])
        def plain(messages):
            return 'Review is required [1].'
        with patch.object(KnowledgeEngine, '_complete', plain), patch.object(self.engine, '_web', side_effect=AssertionError('no consent')):
            fresh = KnowledgeEngine(self.projects, self.home)
            with patch.object(fresh, '_web', side_effect=AssertionError('no consent')):
                result = fresh.answer('What does Orchid require?', project='alpha')
        self.assertFalse(result['streaming'])
        self.assertTrue(result['completed_only'])
