"""Synthetic checks against Hermes' actual public scope API."""
import contextvars
import importlib.util
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import byok


@unittest.skipUnless(importlib.util.find_spec('agent'), 'requires Hermes scope API')
class InstalledHostScopeTests(unittest.TestCase):
    def setUp(self):
        from hermes_constants import get_hermes_home
        self.home = str(get_hermes_home().resolve())

    def test_bound_scope_controls_provider_and_meta_keys(self):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-process'}):
            for mapping in ({}, {'OPENAI_API_KEY': 'synthetic-injected',
                                 'OPENROUTER_API_KEY': 'synthetic-router', 'MODEL_API_KEY': 'synthetic-meta'}):
                token = set_secret_scope(mapping, profile_home=self.home)
                try:
                    self.assertEqual(byok.secret('OPENAI_API_KEY'), mapping.get('OPENAI_API_KEY', ''))
                    self.assertEqual(byok.secret('OPENROUTER_API_KEY'), mapping.get('OPENROUTER_API_KEY', ''))
                    self.assertEqual(byok.secret('MODEL_API_KEY'), mapping.get('MODEL_API_KEY', ''))
                    self.assertEqual(os.environ['OPENAI_API_KEY'], 'synthetic-process')
                finally:
                    reset_secret_scope(token)

    def test_unbound_unstamped_and_wrong_profile_fail_closed(self):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        key = {'OPENAI_API_KEY': 'synthetic-profile-a'}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-process'}):
            for mapping, home in ((None, None), (key, None), (key, str(Path(self.home) / 'other'))):
                token = set_secret_scope(mapping, profile_home=home)
                try:
                    self.assertEqual(byok.secret('OPENAI_API_KEY'), '')
                    self.assertFalse(byok._scope_available())
                finally:
                    reset_secret_scope(token)
            token = set_secret_scope(key, profile_home=self.home)
            other = set_hermes_home_override(Path(self.home) / 'other')
            try:
                self.assertEqual(byok.secret('OPENAI_API_KEY'), '')
            finally:
                reset_hermes_home_override(other)
                reset_secret_scope(token)

    def test_new_request_sees_rotation_and_deletion_old_worker_keeps_snapshot(self):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        first = set_secret_scope({'OPENAI_API_KEY': 'synthetic-first'}, profile_home=self.home)
        try:
            old = contextvars.copy_context()
        finally:
            reset_secret_scope(first)
        for mapping, expected in (({'OPENAI_API_KEY': 'synthetic-second'}, 'synthetic-second'), ({}, '')):
            token = set_secret_scope(mapping, profile_home=self.home)
            try:
                self.assertEqual(byok.secret('OPENAI_API_KEY'), expected)
            finally:
                reset_secret_scope(token)
        values = []
        thread = threading.Thread(target=lambda: values.append(old.run(byok.secret, 'OPENAI_API_KEY')))
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(values, ['synthetic-first'])

    def test_concurrent_scope_change_never_falls_back_to_environment(self):
        from agent import secret_scope
        original = {'OPENAI_API_KEY': 'synthetic-bound', 'MODEL_API_KEY': 'synthetic-unrelated'}
        token = secret_scope.set_secret_scope(original, profile_home=self.home)
        real_get = secret_scope.get_secret
        observed = []
        def refresh_during_get(name, default):
            observed.append((secret_scope.current_secret_scope() is original,
                             secret_scope.is_multiplex_active()))
            original.clear()
            return real_get(name, default)
        try:
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-process'}), \
                 patch.object(secret_scope, 'get_secret', side_effect=refresh_during_get):
                self.assertEqual(byok.secret('OPENAI_API_KEY'), '')
                self.assertEqual(byok.secret('OPENAI_API_KEY'), '')
                self.assertEqual(observed, [(True, True)])
        finally:
            secret_scope.reset_secret_scope(token)
