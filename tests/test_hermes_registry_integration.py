"""Exercise the installed public registry with synthetic per-profile tools."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_adapters import WebSearchUnavailable, registry_search


@unittest.skipUnless(importlib.util.find_spec('agent'), 'requires installed Hermes public API')
class InstalledRegistryTests(unittest.TestCase):
    def test_explicit_profile_scope_selects_only_that_profiles_tool(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools import registry as registry_module

        registry = registry_module.ToolRegistry()
        home_a = Path(tempfile.gettempdir()) / 'synthetic-web-profile-a'
        home_b = Path(tempfile.gettempdir()) / 'synthetic-web-profile-b'
        calls = []
        for home, name in ((home_a, 'a'), (home_b, 'b')):
            def handler(args, *, name=name):
                calls.append((name, args))
                return json.dumps({'success': True, 'data': {'web': [{'title': name}]}})
            registry.register('web_search', 'web', {}, handler, scope=str(home))

        with patch.object(registry_module, 'registry', registry):
            for home, name in ((home_a, 'a'), (home_b, 'b')):
                home_token = set_hermes_home_override(home)
                scope_token = set_secret_scope({}, profile_home=str(home))
                try:
                    self.assertEqual(registry_search('public'), [{'title': name}])
                finally:
                    reset_secret_scope(scope_token)
                    reset_hermes_home_override(home_token)
            home_token = set_hermes_home_override(home_a)
            scope_token = set_secret_scope({}, profile_home=str(home_b))
            try:
                with self.assertRaises(WebSearchUnavailable):
                    registry_search('public')
            finally:
                reset_secret_scope(scope_token)
                reset_hermes_home_override(home_token)
        self.assertEqual(calls, [('a', {'query': 'public', 'limit': 3}),
                                 ('b', {'query': 'public', 'limit': 3})])

    def test_unbound_hosted_dashboard_does_not_borrow_process_key(self):
        import asyncio
        import os
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from hermes_cli.plugin_host_child import HostRuntime
        from hermes_cli.plugin_host_wire import decode

        root = Path(__file__).resolve().parent.parent
        host = HostRuntime.__new__(HostRuntime)
        host.asgi_apps = {}
        host._run = asyncio.run
        token = set_secret_scope(None)
        try:
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-host-value'}):
                result = decode(host.op_asgi({
                    'plugin': 'meeting-copilot-live',
                    'dashboard_dir': str(root / 'dashboard'), 'api_file': 'plugin_api.py',
                    'method': 'GET', 'path': '/models', 'query': '', 'headers': [],
                    'body': None, 'authenticated': True,
                }))
        finally:
            reset_secret_scope(token)
        self.assertEqual(result['status'], 200)
        providers = json.loads(result['body'])['providers']
        openai = next(item for item in providers if item['id'] == 'openai')
        self.assertFalse(openai['configured'])
        self.assertIn('scoped credential context', openai['status'])
        self.assertNotIn('synthetic-host-value', str(openai))
