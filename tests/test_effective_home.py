"""Synthetic public host APIs with real route, SQLite and fake HTTPS."""
import contextvars
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from knowledge import KnowledgeEngine
from server import MeetingState, make_router


class ScopedProductionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.projects = self.root / 'projects'
        (self.projects / 'example').mkdir(parents=True)
        (self.projects / 'example' / 'notes.txt').write_text('orchid project evidence')
        self.primary = self.root / '.hermes'
        self.suffixed = self.root / '.hermes-synthetic'
        self.other = self.root / 'independent-home'
        self.named = self.suffixed / 'profiles' / 'named'
        for home, marker in ((self.primary, 'PRIMARYORCHID'),
                             (self.suffixed, 'SUFFIXORCHID'),
                             (self.other, 'OTHERORCHID'),
                             (self.named, 'NAMEDORCHID')):
            home.mkdir(parents=True)
            with sqlite3.connect(home / 'state.db') as db:
                db.executescript('CREATE TABLE sessions(id TEXT,title TEXT,profile_name TEXT); '
                                 'CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
                db.execute('INSERT INTO sessions VALUES (?,?,?)', ('s1', 'fictional', 'default'))
                db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', (1, 's1', 'user', 'orchid ' + marker, 1))

        self.home_override = contextvars.ContextVar('test-host-home', default=None)
        self.scope = contextvars.ContextVar('test-host-scope', default=None)
        self.multiplex = contextvars.ContextVar('test-multiplex', default=False)
        self.platform = 'posix'
        constants = types.ModuleType('hermes_constants')

        def get_hermes_home():
            override = self.home_override.get()
            if override is not None:
                return Path(override)
            explicit = os.environ.get('HERMES_HOME', '').strip()
            if explicit:
                return Path(explicit)
            suffix = os.environ.get('HERMES_DATA_DIR_SUFFIX', '')
            if self.platform == 'windows':
                return Path(os.environ['LOCALAPPDATA']) / ('hermes' + suffix)
            return Path(os.environ['HOME']) / ('.hermes' + suffix)
        constants.get_hermes_home = get_hermes_home
        agent = types.ModuleType('agent')
        agent.__path__ = []
        host_scope = types.ModuleType('agent.secret_scope')
        host_scope.current_secret_scope = lambda: (self.scope.get() or (None, None))[0]
        host_scope.current_secret_scope_home = lambda: (self.scope.get() or (None, None))[1]
        host_scope.get_secret = lambda name, default='': host_scope.current_secret_scope().get(name, default)
        host_scope.set_multiplex_context = self.multiplex.set
        host_scope.reset_multiplex_context = self.multiplex.reset
        agent.secret_scope = host_scope
        modules = patch.dict(sys.modules, {'hermes_constants': constants, 'agent': agent,
                                           'agent.secret_scope': host_scope})
        modules.start()
        self.addCleanup(modules.stop)
        env = patch.dict(os.environ, {'HOME': str(self.root), 'HERMES_HOME': '',
                        'HERMES_DATA_DIR_SUFFIX': '-synthetic', 'HERMES_PROFILE': '',
                        'HERMES_PROFILE_NAME': '', 'LOCALAPPDATA': str(self.root / 'Local'),
                        'MEETING_COPILOT_PROJECT_ROOT': str(self.projects)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('HERMES_HOME', None)
        self.payloads = []
        self.authorizations = []
        payloads = self.payloads
        authorizations = self.authorizations

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                return b'{"choices":[{"finish_reason":"stop","message":{"content":"Synthetic [1]."}}]}'

        class Opener:
            def open(self, request, timeout):
                payloads.append(json.loads(request.data))
                authorizations.append(request.get_header('Authorization'))
                return Response()

        fake_open = patch('byok.urllib.request.build_opener', return_value=Opener())
        fake_open.start()
        self.addCleanup(fake_open.stop)
        self.state = MeetingState()
        app = FastAPI()
        app.include_router(make_router(self.state))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def post(self, home, *, stamped=None, scope=True, key='synthetic-key', **body):
        route_token = self.home_override.set(home)
        scope_token = self.scope.set(({'OPENAI_API_KEY': key},
                                      str(home if stamped is None else stamped)) if scope else None)
        try:
            return self.client.post('/assist', json={'question': 'orchid',
                'answer_provider': 'openai', 'answer_model': 'synthetic', **body})
        finally:
            self.scope.reset(scope_token)
            self.home_override.reset(route_token)

    def complete(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        rid = response.json()['request_id']
        for _ in range(300):
            entry = self.client.get('/assist/' + rid).json()
            if entry['status'] in ('done', 'error', 'cancelled'):
                self.assertEqual(entry['status'], 'done', entry)
                return entry
            time.sleep(.01)
        self.fail('answer did not finish')

    def test_suffixed_history_not_unsuffixed_reaches_provider(self):
        # No context override: the host API resolves HOME + data-dir suffix.
        entry = self.complete(self.post(None, stamped=self.suffixed, include_history=True))
        self.assertEqual(self.state.engine.hermes_home, self.suffixed)
        self.assertIn('SUFFIXORCHID', json.dumps(entry['sources']))
        self.assertIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('PRIMARYORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('OTHERORCHID', json.dumps(self.payloads[-1]))

    def test_explicit_home_precedes_suffix_in_admitted_request(self):
        with patch.dict(os.environ, {'HERMES_HOME': str(self.other)}):
            self.complete(self.post(None, stamped=self.other))
        self.assertEqual(self.state.engine.hermes_home, self.other)
        self.assertIn('OTHERORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('PRIMARYORCHID', json.dumps(self.payloads[-1]))

    def test_cached_engine_cannot_cross_scopes_and_same_scope_reuses_it(self):
        engines = []
        for home, expected, absent in ((self.suffixed, 'SUFFIXORCHID', 'OTHERORCHID'),
                                       (self.suffixed, 'SUFFIXORCHID', 'OTHERORCHID'),
                                       (self.other, 'OTHERORCHID', 'SUFFIXORCHID'),
                                       (self.suffixed, 'SUFFIXORCHID', 'OTHERORCHID')):
            self.complete(self.post(home))
            engines.append(self.state.engine)
            self.assertIn(expected, json.dumps(self.payloads[-1]))
            self.assertNotIn(absent, json.dumps(self.payloads[-1]))
            self.assertNotIn('PRIMARYORCHID', json.dumps(self.payloads[-1]))
        self.assertIs(engines[0], engines[1])
        self.assertIsNot(engines[1], engines[2])
        self.assertIsNot(engines[2], engines[3])

    def test_reused_same_home_engine_uses_new_request_credential(self):
        self.complete(self.post(self.suffixed, key='synthetic-first'))
        engine = self.state.engine
        self.complete(self.post(self.suffixed, key='synthetic-second'))
        self.assertIs(self.state.engine, engine)
        self.assertEqual(self.authorizations, ['Bearer synthetic-first', 'Bearer synthetic-second'])
        self.assertIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))

    def test_unbound_mismatched_and_named_scopes_reject_before_history(self):
        self.complete(self.post(self.suffixed))
        count = len(self.payloads)
        request_count = len(self.state.requests)
        with patch.object(KnowledgeEngine, '_sessions', side_effect=AssertionError('history must not be read')):
            for response in (self.post(self.other, stamped=self.suffixed),
                             self.post(self.other, stamped=self.suffixed, project='example', include_history=False),
                             self.post(self.other, scope=False), self.post(self.named)):
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn(str(self.root), response.text)
            with patch.dict(os.environ, {'HERMES_PROFILE_NAME': 'named'}):
                self.assertEqual(self.post(self.suffixed).status_code, 400)
        self.assertEqual(len(self.payloads), count)
        self.assertEqual(len(self.state.requests), request_count)
        self.complete(self.post(self.suffixed))
        self.assertIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))

    def test_named_profile_symlink_cannot_route_to_primary_history(self):
        alias = self.named.parent / 'alias'
        try:
            alias.symlink_to(self.primary, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('directory symlinks unavailable')
        # Resolved equality alone cannot turn a lexical named-profile route
        # (or stamp) into permission to read the primary database.
        for route, stamp in ((alias, alias), (alias, self.primary), (self.primary, alias)):
            response = self.post(route, stamped=stamp)
            self.assertEqual(response.status_code, 400, response.text)
        with self.assertRaises(ValueError):
            KnowledgeEngine(project_root=self.projects, hermes_home=alias)
        self.assertEqual(self.payloads, [])

    def test_history_off_remains_request_local_and_never_reaches_provider(self):
        self.complete(self.post(self.suffixed, project='example', include_history=False))
        self.assertIn('orchid project evidence', json.dumps(self.payloads[-1]))
        for marker in ('PRIMARYORCHID', 'SUFFIXORCHID', 'OTHERORCHID'):
            self.assertNotIn(marker, json.dumps(self.payloads[-1]))
        self.complete(self.post(self.suffixed, project='example', include_history=True))
        self.assertIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))

    def test_admitted_home_survives_cache_switch_before_engine_lookup(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = MeetingState.get_engine

        def gated(state, home=None):
            if home == self.suffixed:
                started.set()
                if not release.wait(3):
                    raise RuntimeError('synthetic gate timed out')
            return original(state, home)

        with patch.object(MeetingState, 'get_engine', gated):
            response = self.post(self.suffixed)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(started.wait(3))
            token = self.home_override.set(self.other)
            try:
                self.assertEqual(self.state.get_engine().hermes_home, self.other)
            finally:
                self.home_override.reset(token)
                release.set()
            self.complete(response)
        self.assertIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('OTHERORCHID', json.dumps(self.payloads[-1]))
        self.complete(self.post(self.other))
        self.assertIn('OTHERORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))

    def test_clear_invalidates_retired_engine_still_used_by_a_worker(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                return b'{"choices":[{"finish_reason":"stop","message":{"content":"Synthetic [1]."}}]}'

        class Opener:
            def open(self, request, timeout):
                started.set()
                if not release.wait(3):
                    raise RuntimeError('synthetic gate timed out')
                return Response()

        with patch('byok.urllib.request.build_opener', return_value=Opener()):
            response = self.post(self.suffixed, project='example')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(started.wait(3))
            old_engine = self.state.engine
            self.assertTrue(old_engine._cache)
            token = self.home_override.set(self.other)
            try:
                self.assertIsNot(self.state.get_engine(), old_engine)
            finally:
                self.home_override.reset(token)
            try:
                cleared = self.client.post('/clear')
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertEqual(old_engine._cache, {})
                self.assertEqual(old_engine._index_cache, {})
            finally:
                release.set()
            for _ in range(300):
                with self.state.lock:
                    if not self.state.live_workers:
                        break
                time.sleep(.01)
            self.assertEqual(self.state.live_workers, 0)
            self.assertEqual(self.state._active_engines, {})

    def test_worker_keeps_admitted_home_when_cached_engine_is_replaced(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = KnowledgeEngine._sessions

        def gated(engine, terms):
            if engine.hermes_home == self.suffixed:
                started.set()
                if not release.wait(3):
                    raise RuntimeError('synthetic gate timed out')
            return original(engine, terms)

        with patch.object(KnowledgeEngine, '_sessions', gated):
            response = self.post(self.suffixed)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(started.wait(3))
            token = self.home_override.set(self.other)
            try:
                self.state.get_engine()
            finally:
                self.home_override.reset(token)
                release.set()
            self.complete(response)
        self.assertIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('OTHERORCHID', json.dumps(self.payloads[-1]))
        self.complete(self.post(self.other))
        self.assertIn('OTHERORCHID', json.dumps(self.payloads[-1]))
        self.assertNotIn('SUFFIXORCHID', json.dumps(self.payloads[-1]))

    def test_host_public_home_is_authoritative_for_all_default_layouts(self):
        cases = [('', '-synthetic', 'posix', self.suffixed),
                 ('', '', 'posix', self.primary),
                 (str(self.other), '-synthetic', 'posix', self.other),
                 ('', '-synthetic', 'windows', self.root / 'Local' / 'hermes-synthetic')]
        for explicit, suffix, platform, expected in cases:
            with self.subTest(platform=platform, explicit=explicit, suffix=suffix), \
                 patch.dict(os.environ, {'HERMES_HOME': explicit, 'HERMES_DATA_DIR_SUFFIX': suffix}):
                self.platform = platform
                engine = KnowledgeEngine(project_root=self.projects)
                self.assertEqual(engine.hermes_home, expected.resolve())

    def test_broken_public_home_never_falls_back_to_environment_history(self):
        constants = sys.modules['hermes_constants']
        with patch.object(constants, 'get_hermes_home', side_effect=RuntimeError('host unavailable')):
            engine = KnowledgeEngine(project_root=self.projects)
            with self.assertRaises((RuntimeError, ValueError)):
                engine.search('orchid')
            response = self.post(self.suffixed)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.payloads, [])
