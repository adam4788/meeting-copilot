"""The file editor is retired: every mutation is rejected without file I/O.

Hermes owns secret settings. Old inode/ACL/revision/locking edit contracts no
longer apply because the plugin cannot open or mutate credential files at all.
"""
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import credentials

class CredentialTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.include_router(credentials.make_router(lambda: self.fail('edit boundary forbidden')))
        self.client=TestClient(app)
    def test_only_setup_guidance_is_exposed_to_all_principals(self):
        for headers in ({},{'X-Profile':'foreign','Authorization':'Bearer synthetic-key'}):
            result=self.client.get('/credentials/meta',headers=headers)
            self.assertEqual(result.status_code,200)
            self.assertFalse(result.json()['editable'])
            self.assertIn('Hermes Settings',result.text)
            self.assertNotIn('synthetic-key',result.text)
    def test_all_mutation_shapes_fail_without_files_or_env_publication(self):
        import os
        original=dict(os.environ)
        with patch('builtins.open',side_effect=AssertionError('credential file forbidden')),patch('pathlib.Path.open',side_effect=AssertionError('credential file forbidden')):
            for body in ({'action':'save','key':'synthetic-secret'}, {'action':'remove'}, {'revision':'x'*64,'key':'bad\nNEW=value'}, {'home':'/foreign/.env'}, {'key':'x'*10000}):
                response=self.client.post('/credentials/meta',json=body)
                self.assertEqual(response.status_code,405)
                self.assertNotIn('synthetic-secret',response.text)
        self.assertEqual(dict(os.environ),original)
    def test_setup_never_invokes_host_writer_or_lock(self):
        import sys,types
        host=types.SimpleNamespace(_env_write_lock=lambda *a: self.fail('host lock forbidden'),_write_env_lines=lambda *a:self.fail('host writer forbidden'))
        with patch.dict(sys.modules,{'hermes_cli.config':host}):
            self.assertEqual(self.client.get('/credentials/meta').status_code,200)
            self.assertEqual(self.client.post('/credentials/meta',json={'action':'remove'}).status_code,405)
