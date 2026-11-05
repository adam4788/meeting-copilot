"""End-to-end history-scope safety using synthetic files and in-process routes."""
import tempfile
import time
import unittest
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from knowledge import KnowledgeEngine
from server import MeetingState, make_router


class HistoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'fictional').mkdir()
        (root / 'fictional' / 'plan.txt').write_text('The moonlight project uses seven blue lanterns.', encoding='utf-8')
        (root / 'other').mkdir()
        (root / 'other' / 'other.txt').write_text('Moonlight lanterns contain an unrelated sentinel.', encoding='utf-8')
        self.calls = []
        self.messages = []
        def completion(messages):
            self.messages.append(messages)
            return 'Seven blue lanterns [1].'
        self.engine = KnowledgeEngine(project_root=root, hermes_home=root, _completion=completion, _standalone=True)
        def history(terms):
            self.calls.append(terms)
            return [{'id':'synthetic-history','title':'Synthetic history','source':'session:fake','text':'moonlight secret sentinel'}]
        self.engine._sessions = history
        self.state = MeetingState(engine_factory=lambda: self.engine)
        app = FastAPI(); app.include_router(make_router(self.state))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def ask(self, **kwargs):
        response = self.client.post('/assist', json={'question':'How many moonlight lanterns?', 'project':'fictional', **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        rid = response.json()['request_id']
        for _ in range(300):
            entry = self.client.get('/assist/' + rid).json()
            if entry['status'] in ('done','error','cancelled'):
                return entry
            time.sleep(.01)
        self.fail('answer did not finish')

    def test_history_off_route_uses_selected_project_without_calling_host_sessions(self):
        entry = self.ask(include_history=False)
        self.assertEqual(entry['status'], 'done', entry)
        self.assertEqual(self.calls, [])
        self.assertTrue(any('plan.txt' in source.get('title','') for source in entry['sources']))
        self.assertFalse(any(str(s.get('source','')).startswith('session:') for s in entry['sources']))
        self.assertEqual(entry['request']['include_history'], False)
        self.assertNotIn('moonlight secret sentinel', str(self.messages))
        self.assertNotIn('unrelated sentinel', str(self.messages))

    def test_default_and_explicit_history_on_keep_existing_behavior(self):
        for args in ({}, {'include_history':True}):
            entry = self.ask(**args)
            self.assertEqual(entry['status'], 'done', entry)
            self.assertTrue(self.calls)
            self.assertTrue(any(str(s.get('source','')).startswith('session:') for s in entry['sources']))
            self.calls.clear()

    def test_streaming_route_preserves_opt_out(self):
        class Streaming:
            def __call__(self, messages): return 'Seven lanterns [1].'
            def stream_chunks(self, messages):
                yield 'Seven lanterns [1].'
        self.engine._completion_override = Streaming()
        entry = self.ask(include_history=False)
        self.assertEqual(entry['status'], 'done', entry)
        self.assertEqual(self.calls, [])
        self.assertTrue(any('plan.txt' in source.get('title','') for source in entry['sources']))
        self.assertTrue(entry['streaming'])

    def test_shared_engine_concurrent_searches_keep_history_request_local(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            off = pool.submit(self.engine.search, 'moonlight lanterns', project='fictional', include_history=False)
            on = pool.submit(self.engine.search, 'moonlight lanterns', project='fictional')
            off_sources, on_sources = off.result(), on.result()
        self.assertFalse(any(str(s.get('source','')).startswith('session:') for s in off_sources))
        self.assertTrue(any(str(s.get('source','')).startswith('session:') for s in on_sources))
        self.assertEqual(len(self.calls), 1)

    def test_overlay_click_routes_opt_out_to_same_engine(self):
        import overlay_bridge
        scope = {'project':'fictional', 'include_history':False, 'web_fallback':False}
        self.state.overlay_scope_value = self.state._clean_overlay_scope(scope)
        marker = overlay_bridge._PANE_ACTION.set('synthetic-pane')
        self.addCleanup(overlay_bridge._PANE_ACTION.reset, marker)
        transport = overlay_bridge.BackendTransport(self.state, pane_token='synthetic-pane')
        rid = transport.assist('How many moonlight lanterns?', scope, 'synthetic-row')
        for _ in range(300):
            entry = self.state.assist_request(rid)
            if entry['status'] in ('done','error','cancelled'):
                break
            time.sleep(.01)
        self.assertEqual(entry['status'], 'done', entry)
        self.assertEqual(entry['request']['include_history'], False)
        self.assertEqual(self.calls, [])
        self.assertTrue(any('plan.txt' in s.get('title','') for s in entry['sources']))

    def test_overlay_rejects_non_boolean_history_setting(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.state._clean_overlay_scope({'include_history':'false'})

    def test_off_fails_closed_on_invalid_boolean_and_unscoped_or_web_request(self):
        for body in ({'include_history':'false','project':'fictional'},
                     {'include_history':False},
                     {'include_history':False,'all_projects':True},
                     {'include_history':False,'project':'fictional','web_fallback':True},
                     {'include_history':False,'project':'fictional','web_query':'public query'}):
            response = self.client.post('/assist', json={'question':'moonlight?', **body})
            self.assertIn(response.status_code, (400,422), body)
        self.assertEqual(self.calls, [])
