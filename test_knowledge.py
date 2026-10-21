import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_hidden_project_cannot_be_selected(self):
        hidden = self.projects / '.private'
        hidden.mkdir()
        (hidden / 'notes.md').write_text('Orchid hidden')
        with self.assertRaises(ValueError):
            self.engine.search('Orchid', '.private')


if __name__ == '__main__':
    unittest.main()
