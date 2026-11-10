"""No credential-file bridge, standalone key fallback, or worker env leak.

Replaces launch dotenv seeding/parser/edit tests. Plugin execution must use a
bound host scope; isolated workers never inherit environment credentials.
"""
import os
import unittest
from unittest.mock import patch
import byok
import macos_audio

class CredentialRuntimeTests(unittest.TestCase):
    def test_local_worker_scrubs_provider_credentials_and_never_resolves_meta(self):
        ambient={'OPENAI_API_KEY':'synthetic-openai','OPENROUTER_API_KEY':'synthetic-router','MODEL_API_KEY':'synthetic-meta','ANTHROPIC_API_KEY':'synthetic-other','AWS_SECRET_KEY':'synthetic-aws','PYTHONPATH':'bad','PYTHONHOME':'bad','PATH':'runtime-path'}
        with patch.dict(os.environ,ambient,clear=True),patch.object(macos_audio,'_model_api_key',side_effect=AssertionError('local ASR must not load keys')):
            env=macos_audio.worker_env('local')
        self.assertEqual(env,{'PATH':'runtime-path','HF_HUB_DISABLE_IMPLICIT_TOKEN':'1'})
    def test_meta_worker_environment_contains_no_provider_key(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-openai','OPENROUTER_API_KEY':'synthetic-router','MODEL_API_KEY':'synthetic-owner'},clear=True),patch.object(macos_audio,'_model_api_key',side_effect=AssertionError('environment builder must not resolve keys')):
            env=macos_audio.worker_env('meta')
        self.assertEqual(env,{'HF_HUB_DISABLE_IMPLICIT_TOKEN':'1'})
    def test_no_import_time_file_read_or_environment_mutation(self):
        import importlib,meta_models
        before=dict(os.environ)
        with patch('builtins.open',side_effect=AssertionError('credential file forbidden')),patch('pathlib.Path.open',side_effect=AssertionError('credential file forbidden')):
            importlib.reload(meta_models)
        self.assertEqual(dict(os.environ),before)
        self.assertFalse(hasattr(meta_models,'_load_dotenv_key_once'))
    def test_standalone_environment_never_supplies_a_key(self):
        import sys,types
        with patch.dict(sys.modules,{'agent':types.SimpleNamespace(secret_scope=None)}),patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-cli'}):
            self.assertEqual(byok.secret('OPENAI_API_KEY'),'')
            self.assertEqual(byok.secret('OPENAI_API_KEY',standalone=True),'')
    def test_scope_failure_cannot_fall_back_in_audio(self):
        with patch('meta_models.resolve_model_api_key',side_effect=RuntimeError('synthetic-secret')),patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-owner'}):
            self.assertEqual(macos_audio._model_api_key(),'')
