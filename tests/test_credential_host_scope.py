"""Actual Hermes scope API with synthetic mappings, no credential-file access.

Replaces the retired plugin file-editor harness. Host secret resolution and
snapshot isolation remain covered without loading session or account data.
"""
import importlib.util
import contextvars
import os
import threading
import unittest
from unittest.mock import patch
import byok

@unittest.skipUnless(importlib.util.find_spec('agent'), 'requires Hermes scope API')
class InstalledHostScopeTests(unittest.TestCase):
    def test_real_bound_scope_is_authoritative_and_environment_unchanged(self):
        from agent.secret_scope import set_secret_scope,reset_secret_scope
        with patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-owner'}):
            for mapping in ({},{'OPENAI_API_KEY':'synthetic-profile'}):
                token=set_secret_scope(mapping,profile_home='/synthetic-profile')
                try:
                    self.assertEqual(byok.secret('OPENAI_API_KEY'),mapping.get('OPENAI_API_KEY',''))
                    self.assertEqual(os.environ['OPENAI_API_KEY'],'synthetic-owner')
                finally:reset_secret_scope(token)
    def test_copied_worker_scope_retains_snapshot_and_new_scope_is_fresh(self):
        from agent.secret_scope import set_secret_scope,reset_secret_scope
        token=set_secret_scope({'OPENAI_API_KEY':'synthetic-first'},profile_home='/synthetic-profile')
        try:old=contextvars.copy_context()
        finally:reset_secret_scope(token)
        token=set_secret_scope({'OPENAI_API_KEY':'synthetic-second'},profile_home='/synthetic-profile')
        values=[]
        try:
            thread=threading.Thread(target=lambda: values.append(old.run(byok.secret,'OPENAI_API_KEY')))
            thread.start();thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(values,['synthetic-first'])
            self.assertEqual(byok.secret('OPENAI_API_KEY'),'synthetic-second')
        finally:reset_secret_scope(token)
    def test_unbound_host_fails_closed_even_for_launch_owner(self):
        from agent.secret_scope import set_secret_scope,reset_secret_scope
        token=set_secret_scope(None)
        try:
            with patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-owner'}):self.assertEqual(byok.secret('OPENAI_API_KEY'),'')
        finally:reset_secret_scope(token)
