"""Current setup contract replaces plugin-owned credential mutation behavior."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import byok
import credentials
import server

class SetupTests(unittest.TestCase):
    def test_setup_only_surface_never_opens_files_or_accepts_credentials(self):
        app=FastAPI();app.include_router(credentials.make_router())
        with TestClient(app) as client, patch('builtins.open', side_effect=AssertionError('credential file I/O forbidden')):
            status=client.get('/credentials/meta')
            self.assertEqual(status.status_code,200)
            self.assertFalse(status.json()['editable'])
            self.assertIn('Hermes Settings',status.json()['message'])
            for body in ({'key':'synthetic-key'},{'action':'remove'},{'api_key':'synthetic-key','revision':'x'}):
                rejected=client.post('/credentials/meta',json=body)
                self.assertEqual(rejected.status_code,405)
                self.assertNotIn('synthetic-key',rejected.text)
    def test_requests_cannot_smuggle_keys_or_endpoint_overrides(self):
        from pydantic import ValidationError
        for field in ('key','api_key','base_url','endpoint','headers'):
            with self.subTest(field=field),self.assertRaises(ValidationError):
                server.AssistRequest(question='synthetic',**{field:'synthetic-secret'})
    def test_scoped_readiness_is_never_reused_across_requests(self):
        import model_selection
        with patch.object(byok,'secret',return_value='synthetic-key'):
            ready=model_selection.available_models()
        with patch.object(byok,'secret',return_value=''):
            missing=model_selection.available_models()
        self.assertTrue(all(p['configured'] for p in ready['providers']))
        self.assertTrue(all(not p['configured'] for p in missing['providers']))
        self.assertNotIn('synthetic-key',json.dumps(ready))
        self.assertEqual({p['id'] for p in ready['providers']},{'openai','openrouter'})
    def test_no_selection_never_invokes_configured_host_model(self):
        from knowledge import KnowledgeEngine
        with tempfile.TemporaryDirectory() as root:
            engine=KnowledgeEngine(root,root)
            with patch.object(engine,'_complete',side_effect=AssertionError('host default forbidden')):
                with self.assertRaises(ValueError):engine.answer('synthetic')
                with self.assertRaises(ValueError):list(engine.answer_stream('synthetic'))
    def test_scope_exception_fails_closed_even_with_environment_key(self):
        import sys,types
        scoped=types.SimpleNamespace(current_secret_scope=lambda: (_ for _ in ()).throw(RuntimeError('synthetic-key')))
        with patch.dict(sys.modules,{'agent':types.SimpleNamespace(secret_scope=scoped)}),patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-key'}):
            self.assertEqual(byok.secret('OPENAI_API_KEY'),'')
            with self.assertRaises(ValueError):byok.completion('openai','synthetic-model')

    def test_catalog_distinguishes_missing_scope_from_missing_key(self):
        import sys, types
        scope = types.SimpleNamespace(current_secret_scope=lambda: None,
                                      get_secret=lambda *_: self.fail('must not read unbound scope'))
        with patch.dict(sys.modules, {'agent': types.SimpleNamespace(secret_scope=scope)}), \
             patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-key'}):
            provider = byok.catalog()['providers'][0]
        self.assertFalse(provider['configured'])
        self.assertIn('scoped credential context', provider['status'])
        self.assertNotIn('synthetic-key', str(provider))
        scope.current_secret_scope = lambda: {}
        scope.current_secret_scope_home = lambda: '/synthetic-profile'
        scope.get_secret = lambda *_: ''
        constants = types.SimpleNamespace(get_hermes_home=lambda: Path('/synthetic-profile'))
        with patch.dict(sys.modules, {'agent': types.SimpleNamespace(secret_scope=scope), 'hermes_constants': constants}):
            provider = byok.catalog()['providers'][0]
        self.assertIn('Set key in Hermes Settings', provider['status'])
    def test_meta_readiness_import_failure_never_borrows_environment(self):
        with patch('importlib.import_module',side_effect=RuntimeError('synthetic')),patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-key'}):
            self.assertFalse(server._meta_key_present())
