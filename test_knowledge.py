import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from knowledge import KnowledgeEngine as RuntimeKnowledgeEngine

# These retrieval/stream tests inject a provider explicitly. Their historical
# _complete patches are synthetic transport fixtures, never a host-model route.
class _InjectedFixture:
    def __init__(self, owner): self.owner=owner
    @property
    def complete(self): return self.owner._complete
    @property
    def stream_chunks(self): return getattr(self.owner._complete, 'stream_chunks', None)

class KnowledgeEngine(RuntimeKnowledgeEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._completion_override=_InjectedFixture(self)


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
        import io,byok
        response=io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'Grounded [1].'}}]}).encode())
        opener=Mock();opener.open.return_value=response
        with patch('byok.secret',return_value='synthetic-key'),patch('urllib.request.build_opener',return_value=opener):
            self.assertEqual(byok.completion('openai','exact-model')([]),'Grounded [1].')
        request=opener.open.call_args.args[0]
        self.assertNotIn('tools',json.loads(request.data))
        self.assertEqual(opener.open.call_args.kwargs['timeout'],30)

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
        self.assertEqual(out_header['model_route'], 'injected')

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
        self.assertEqual(out_header['model_route'], 'injected')
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


class StreamingProbeTests(unittest.TestCase):
    def test_supports_streaming_probe_needs_no_retrieval(self):
        from unittest.mock import Mock
        calls = []
        engine = KnowledgeEngine.__new__(KnowledgeEngine)
        streaming = Mock(return_value='x [1].')
        streaming.stream_chunks = Mock(return_value=iter(['x']))
        import model_selection as selection
        orig = selection.selected_completion
        selection.selected_completion = lambda p, m: streaming
        try:
            engine2 = KnowledgeEngine.__new__(KnowledgeEngine)
            self.assertTrue(engine2.supports_streaming(answer_provider='p', answer_model='m'))
        finally:
            selection.selected_completion = orig
        plain = Mock(return_value='x [1].')
        del plain.stream_chunks
        selection.selected_completion = lambda p, m: plain
        try:
            engine3 = KnowledgeEngine.__new__(KnowledgeEngine)
            self.assertFalse(engine3.supports_streaming(answer_provider='p', answer_model='m'))
        finally:
            selection.selected_completion = orig


class IncrementalRetrievalTests(KnowledgeTests):
    def test_expired_ttl_without_disk_changes_skips_rereads(self):
        from pathlib import Path
        from unittest.mock import patch
        import time as _time
        (self.project / 'notes.md').write_text('Orchid approval.', encoding='utf-8')
        first = self.engine.search('Orchid', project='alpha')
        self.assertTrue(first)
        # Expire the TTL relative to the monotonic clock (robust to clock origin).
        for key, (stamp, fingerprint, docs) in list(self.engine._cache.items()):
            self.engine._cache[key] = (_time.monotonic() - 16, fingerprint, docs)
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not re-read')):
            second = self.engine.search('Orchid', project='alpha')
        self.assertEqual(second, first)

    def test_changed_file_invalidates_fingerprint(self):
        import time as _time
        (self.project / 'notes.md').write_text('Orchid approval.', encoding='utf-8')
        self.assertTrue(self.engine.search('Orchid', project='alpha'))
        for key, (stamp, fingerprint, docs) in list(self.engine._cache.items()):
            self.engine._cache[key] = (_time.monotonic() - 16, fingerprint, docs)
        (self.project / 'notes.md').write_text('Orchid approved yesterday.', encoding='utf-8')
        hits = self.engine.search('Orchid approved', project='alpha')
        self.assertTrue(any('approved yesterday' in h['text'] for h in hits))

    def test_preselect_bounds_single_combined_rank(self):
        docs = [{'text': f'doc {i} orchid'} for i in range(50)]
        short = KnowledgeEngine._preselect(docs, ['orchid'], 6)
        self.assertEqual(len(short), 6)
        self.assertEqual(short[0]['text'], 'doc 0 orchid')
        self.assertEqual(KnowledgeEngine._preselect(docs, ['missing'], 6), [])

    def test_preselect_matches_tokens_not_substrings(self):
        # "renewal" contains the substring "new" but not the token: it must
        # not crowd the document that actually contains "new" out of the
        # shortlist before FTS ranking runs.
        docs = [{'text': f'renewal paperwork {i}'} for i in range(6)]
        docs.append({'text': 'brand new rollout'})
        short = KnowledgeEngine._preselect(docs, ['new'], 6)
        self.assertEqual([d['text'] for d in short], ['brand new rollout'])
        hits = self.engine._rank(short, ['new'], 6)
        self.assertEqual([h['text'] for h in hits], ['brand new rollout'])

    def test_preselect_mirrors_fts_tokenization_and_frequency(self):
        # FTS5 folds accents and splits on underscores; preselection must
        # not discard documents the ranker itself would match.
        docs = [{'text': 'café menu'}, {'text': 'alpha_beta launch'}]
        self.assertEqual([d['text'] for d in KnowledgeEngine._preselect(docs, ['cafe'], 6)],
                         ['café menu'])
        self.assertEqual([d['text'] for d in KnowledgeEngine._preselect(docs, ['alpha'], 6)],
                         ['alpha_beta launch'])
        # Same distinct coverage: higher term frequency wins the shortlist
        # slot over mere file order, so BM25 sees the stronger document.
        docs = [{'text': f'alpha once {i}'} for i in range(6)]
        docs.append({'text': 'alpha ' * 30})
        short = KnowledgeEngine._preselect(docs, ['alpha'], 6)
        self.assertIn('alpha ' * 30, [d['text'] for d in short])

    def test_preselect_does_not_invent_ligature_tokens(self):
        # FTS5 unicode61 does not compatibility-decompose the ﬂ ligature,
        # so preselection must not treat "ﬂower" as the token "flower".
        docs = [{'text': 'ﬂower ﬂower'}, {'text': 'flower'}]
        short = KnowledgeEngine._preselect(docs, ['flower'], 1)
        self.assertEqual([d['text'] for d in short], ['flower'])

    def test_phrase_query_survives_all_project_candidates(self):
        # FTS treats "alpha_beta" as the phrase alpha<->beta: decoys with
        # words between the terms must not crowd out the only phrase match.
        for i in range(30):
            (self.project / f'decoy{i}.md').write_text(f'alpha middle beta note {i}', encoding='utf-8')
        (self.project / 'target.md').write_text(
            'alpha beta ' + ('surrounding prose about schedules and budgets ' * 20), encoding='utf-8')
        single = self.engine.search('alpha_beta', project='alpha', limit=6)
        combined = self.engine.search('alpha_beta', all_projects=True, limit=6)
        self.assertTrue(any('alpha beta' in h['text'] for h in single))
        self.assertEqual([h['text'] for h in combined], [h['text'] for h in single])

    def test_all_project_ranking_matches_single_project(self):
        for i in range(6):
            (self.project / f'short{i}.md').write_text(f'alpha short note {i}', encoding='utf-8')
        (self.project / 'long.md').write_text(
            'alpha alpha ' + ('filler prose about schedules and budgets ' * 40), encoding='utf-8')
        single = self.engine.search('alpha', project='alpha', limit=1)
        combined = self.engine.search('alpha', all_projects=True, limit=1)
        self.assertEqual([h['text'] for h in combined], [h['text'] for h in single])

    def test_preselect_bm25_prefers_short_strong_match(self):
        docs = [{'text': f'alpha short note {i}'} for i in range(6)]
        docs.append({'text': 'alpha alpha ' + ('filler prose about schedules and budgets ' * 40)})
        short = KnowledgeEngine._preselect(docs, ['alpha'], 1)
        self.assertTrue(short[0]['text'].startswith('alpha short note'))

    def test_preselect_preserves_greek_accent_distinction(self):
        # FTS5 only folds Latin diacritics: unaccented Greek must not
        # crowd out the exact accented token.
        docs = [{'text': 'αθηνα αθηνα αθηνα'} for _ in range(6)]
        docs.append({'text': 'αθήνα'})
        short = KnowledgeEngine._preselect(docs, ['αθήνα'], 6)
        self.assertEqual([d['text'] for d in short], ['αθήνα'])


class LargeCorpusBoundTests(KnowledgeTests):
    def test_unchanged_large_project_skips_rereads_after_expiry(self):
        import time as _time
        from pathlib import Path
        from unittest.mock import patch
        # Big files fill the 600-chunk doc cap from the first ~38 files while
        # hundreds more candidate files follow: the fingerprint must still
        # match exactly, or every post-TTL search pays full re-reads.
        for i in range(40):
            (self.project / f'doc{i:02}.md').write_text('Orchid review notes. ' * 1600, encoding='utf-8')
        for i in range(300):
            (self.project / f'note{i:03}.md').write_text(f'Orchid appendix note {i}.', encoding='utf-8')
        first = self.engine.search('Orchid review', project='alpha', limit=6)
        self.assertTrue(first)
        self.assertTrue(dict(self.engine._cache))
        for key, (stamp, fingerprint, docs) in list(self.engine._cache.items()):
            self.engine._cache[key] = (_time.monotonic() - 16, fingerprint, docs)
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not re-read')):
            second = self.engine.search('Orchid review', project='alpha', limit=6)
        self.assertEqual(second, first)


class DefaultMetaStreamFallbackTests(KnowledgeTests):
    def test_default_meta_never_submits_or_falls_back(self):
        engine=RuntimeKnowledgeEngine(self.projects,self.home)
        with patch('meta_models.meta_completion',side_effect=AssertionError('automatic Meta forbidden')), patch.object(engine,'_complete',side_effect=AssertionError('host model forbidden')):
            with self.assertRaises(ValueError):engine.answer_stream('synthetic question',project='alpha')
            with self.assertRaises(ValueError):engine.answer('synthetic question',project='alpha')

    def test_explicit_provider_stream_failure_stays_an_error(self):
        import model_selection
        real = model_selection.selected_completion
        def failing_stream(messages):
            raise RuntimeError('provider down')
        complete = Mock(return_value='x')
        complete.stream_chunks = failing_stream
        with patch.object(model_selection, 'selected_completion', return_value=complete):
            with self.assertRaises(RuntimeError):
                self.engine.answer_stream('What does Orchid require?', project='alpha',
                                          web_fallback=False, answer_provider='p', answer_model='m')


class ProviderBoundaryAbortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        (self.projects / 'alpha').mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def test_cancel_during_meta_failure_never_calls_fallback(self):
        from knowledge import _Aborted
        engine = KnowledgeEngine(self.projects, self.home)
        flag = [False]
        def failing_meta(messages):
            flag[0] = True
            raise RuntimeError('meta down')
        hermes_spy = Mock(return_value='hello [1].')
        with patch('meta_models.api_key_present', return_value=True), \
             patch('meta_models.meta_completion', return_value=failing_meta), \
             patch.object(engine, '_complete', hermes_spy):
            result=engine.answer('q?', transcript='hello there', answer_provider='meta', answer_model='muse-spark-1.3', should_abort=lambda: flag[0])
            self.assertTrue(flag[0])
            self.assertIn('unavailable',result['answer'])
            with self.assertRaises(_Aborted):
                engine.answer('q?', transcript='hello there', answer_provider='meta', answer_model='muse-spark-1.3', should_abort=lambda: flag[0])
        hermes_spy.assert_not_called()

    def test_cancel_between_stream_setup_and_consumption_never_submits(self):
        from knowledge import _Aborted
        engine = KnowledgeEngine(self.projects, self.home)
        submitted = []
        def lazy(messages):
            def gen():
                submitted.append(1)
                yield 'a'
            return gen()
        provider = Mock()
        provider.stream_chunks = Mock(side_effect=lazy)
        flag = [False]
        with patch.object(engine, '_complete', provider):
            header, gen = engine.answer_stream('q?', transcript='hello there',
                                               should_abort=lambda: flag[0])
            flag[0] = True
            with self.assertRaises(_Aborted):
                list(gen)
        self.assertEqual(submitted, [])


class PreparedFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        (self.projects / 'alpha').mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def test_completed_fallback_reuses_prepared_retrieval(self):
        engine = KnowledgeEngine(self.projects, self.home)
        provider = Mock()
        provider.stream_chunks = Mock(return_value=iter(['x']))
        with patch.object(engine, '_complete', provider):
            header, chunks = engine.answer_stream('q?', 'hello there')
            list(chunks)
        self.assertIn('sources', header)
        search_spy = Mock(side_effect=engine.search)
        web_spy = Mock(side_effect=engine._web)
        with patch.object(engine, 'search', search_spy), \
             patch.object(engine, '_web', web_spy), \
             patch.object(engine, '_complete', Mock(return_value='done [1].')):
            result = engine.answer('q?', transcript='hello there', prepared=header)
        search_spy.assert_not_called()
        web_spy.assert_not_called()
        self.assertEqual(result['sources'], header['sources'])


class PreparedReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        (self.projects / 'alpha').mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def test_prepared_web_search_provenance_survives_fallback(self):
        engine = KnowledgeEngine(self.projects, self.home)
        with patch.object(engine, '_complete', Mock(return_value='done [1].')):
            result = engine.answer('q?', transcript='hello',
                                   prepared={'sources': [], 'warnings': [],
                                             'web_search': {'used': True, 'mode': 'explicit', 'query': 'q'}})
        self.assertEqual(result['web_search']['used'], True)

    def test_sync_stream_setup_failure_carries_prepared_retrieval(self):
        engine = KnowledgeEngine(self.projects, self.home)
        search_spy = Mock(side_effect=engine.search)
        with patch.object(engine, 'search', search_spy), \
             patch.object(engine, '_complete', object()):
            with self.assertRaises(NotImplementedError) as ctx:
                engine.answer_stream('q?', 'hello there')
        prepared = getattr(ctx.exception, 'prepared', None)
        self.assertIsNotNone(prepared)
        first_count = search_spy.call_count
        self.assertGreaterEqual(first_count, 1)
        with patch.object(engine, 'search', search_spy), \
             patch.object(engine, '_complete', Mock(return_value='done [1].')):
            result = engine.answer('q?', transcript='hello there', prepared=prepared)
        self.assertEqual(search_spy.call_count, first_count)
        self.assertIn('done', result['answer'])


class PreparedStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        (self.projects / 'alpha').mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def test_retrieval_stage_still_runs_requested_web_phase(self):
        engine = KnowledgeEngine(self.projects, self.home)
        web_spy = Mock(return_value=[])
        with patch.object(engine, '_web', web_spy), \
             patch.object(engine, '_complete', Mock(return_value='done [1].')):
            result = engine.answer('q?', transcript='hello', web_query='docs',
                                   prepared={'sources': [], 'warnings': [],
                                             'web': {'used': False}, 'stage': 'retrieval'})
        web_spy.assert_called_once()
        self.assertTrue(result['web_search']['used'])

    def test_full_stage_never_repeats_completed_web_phase(self):
        engine = KnowledgeEngine(self.projects, self.home)
        web_spy = Mock(return_value=[])
        with patch.object(engine, '_web', web_spy), \
             patch.object(engine, '_complete', Mock(return_value='done [1].')):
            result = engine.answer('q?', transcript='hello', web_query='docs',
                                   prepared={'sources': [], 'warnings': [],
                                             'web': {'used': False}, 'stage': 'full'})
        web_spy.assert_not_called()


class MetaSkipAndStreamingFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        (self.projects / 'alpha').mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def test_skip_meta_default_never_touches_failed_endpoint(self):
        engine = KnowledgeEngine(self.projects, self.home)
        hermes_spy = Mock(return_value='done [1].')
        with patch('meta_models.api_key_present', return_value=True), \
             patch('meta_models.meta_completion') as meta_factory, \
             patch.object(engine, '_complete', hermes_spy):
            result = engine.answer('q?', transcript='hello', skip_meta_default=True,
                                   prepared={'sources': [], 'warnings': [],
                                             'web': {'used': False}, 'stage': 'full'})
        meta_factory.assert_not_called()
        hermes_spy.assert_called_once()
        self.assertIn('done', result['answer'])

    def test_fallback_answer_reports_completed_not_streaming(self):
        engine = KnowledgeEngine(self.projects, self.home)
        with patch.object(engine, '_complete', Mock(return_value='done [1].')):
            result = engine.answer('q?', transcript='hello',
                                   prepared={'sources': [], 'warnings': [],
                                             'web': {'used': False}, 'stage': 'full'})
        self.assertFalse(result['streaming'])
        self.assertTrue(result['completed_only'])


class SyncSetupPreparedRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        (self.projects / 'alpha').mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def test_sync_setup_failure_carries_model_route(self):
        engine = KnowledgeEngine(self.projects, self.home)
        with patch.object(engine, '_complete', object()):
            with self.assertRaises(NotImplementedError) as ctx:
                engine.answer_stream('q?', 'hello there')
        prepared = getattr(ctx.exception, 'prepared', None)
        self.assertIsNotNone(prepared)
        self.assertIn('model_route', prepared)


class WebPlannedReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / 'Projects'
        self.projects.mkdir()
        (self.projects / 'alpha').mkdir()
        self.home = self.root / 'hermes'
        self.home.mkdir()

    def test_planned_but_unused_web_is_not_replanned(self):
        engine = KnowledgeEngine(self.projects, self.home)
        with patch.object(engine, '_complete', Mock(return_value='done [1].')), \
             patch('knowledge.plan_web') as plan_spy:
            engine.answer('q?', transcript='hello',
                          prepared={'sources': [], 'warnings': [],
                                    'web_search': {'used': False},
                                    'web_planned': True})
        plan_spy.assert_not_called()
