import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import host_default
import runtime_loader


class Facade:
    def __init__(self):
        self.calls = []
        self.result = types.SimpleNamespace(text=' answer ', provider='provider', model='model')
    def complete(self, **kw):
        self.calls.append(kw)
        return self.result


class HostDefaultTests(unittest.TestCase):
    def setUp(self):
        host_default._BINDING = None
        self.facade = Facade()
        self.cleanup = []
        self.ctx = types.SimpleNamespace(llm=self.facade, on_unload=lambda cb: self.cleanup.append(cb) or object())
        self.patches = [patch.dict(sys.modules, {
            'agent': types.ModuleType('agent'),
            'agent.plugin_llm': types.SimpleNamespace(PluginLlm=Facade),
            'hermes_constants': types.SimpleNamespace(get_hermes_home=lambda: Path('/profile').resolve())}),
            patch.object(host_default, '_scope_home', return_value=Path('/profile').resolve())]
        for p in self.patches:
            p.start()
    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        host_default._BINDING = None
    def test_no_model_calls_until_completion(self):
        self.assertFalse(host_default.catalog_entry()['configured'])
        host_default.register(self.ctx)
        self.assertTrue(host_default.catalog_entry()['configured'])
        fn = host_default.completion()
        self.assertEqual(self.facade.calls, [])
        self.assertEqual(fn([{'role':'user', 'content':'hello'}]), 'answer')
        self.assertEqual(fn.model_route, 'provider/model')
        self.assertEqual(set(self.facade.calls[0]), {'messages', 'max_tokens', 'timeout', 'purpose'})
        self.assertEqual(self.facade.calls[0]['timeout'], 30)
    def test_remote_and_subclass_rejected(self):
        class RemoteFacade(Facade):
            pass
        self.ctx.llm = RemoteFacade()
        host_default.register(self.ctx)
        self.assertFalse(host_default.catalog_entry()['configured'])
    def test_home_mismatch_and_unload_revoke_prepared_call(self):
        host_default.register(self.ctx)
        fn = host_default.completion()
        with patch.object(host_default, '_scope_home', return_value=Path('/other').resolve()):
            self.assertFalse(host_default.catalog_entry()['configured'])
            with self.assertRaises(RuntimeError): fn([])
        self.cleanup[-1]()
        with self.assertRaises(RuntimeError): fn([])
        self.assertEqual(self.facade.calls, [])
    def test_stale_cleanup_does_not_remove_replacement(self):
        host_default.register(self.ctx)
        old = self.cleanup[-1]
        host_default.register(self.ctx)
        old()
        self.assertTrue(host_default.catalog_entry()['configured'])
    def test_unload_during_registration_cannot_publish_retired_context(self):
        self.ctx.on_unload = lambda callback: callback() or object()
        host_default.register(self.ctx)
        self.assertFalse(host_default.catalog_entry()['configured'])
        with self.assertRaises(RuntimeError): host_default.completion()
        self.assertEqual(self.facade.calls, [])

    def test_replacement_revokes_prepared_call(self):
        host_default.register(self.ctx)
        old = host_default.completion()
        host_default.register(self.ctx)
        with self.assertRaises(RuntimeError): old([])
        self.assertEqual(self.facade.calls, [])

    def test_result_limits_and_sanitization(self):
        host_default.register(self.ctx)
        fn = host_default.completion()
        for secret in ('sk-secret', 'ghp_secret', 'github_pat_secret', 'eyJ' + 'x' * 30):
            self.facade.result.provider = secret
            fn([])
            self.assertEqual(fn.model_route, 'hermes/default (host route unreported)')
        self.facade.result.text = 'x' * 64001
        with self.assertRaisesRegex(RuntimeError, '^Hermes default answer failed'):
            fn([])
    def test_exception_sanitization(self):
        host_default.register(self.ctx)
        fn = host_default.completion()
        with patch.object(self.facade, 'complete', side_effect=RuntimeError('sk-secret')):
            with self.assertRaises(RuntimeError) as cm: fn([])
        self.assertNotIn('sk-secret', str(cm.exception))
    def test_catalog_and_legacy_entrypoints_share_runtime(self):
        source = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            for legacy in (False, True):
                plugin = Path(tmp) / ('legacy' if legacy else 'catalog')
                dashboard = plugin / 'dashboard'
                root = dashboard / 'core' if legacy else plugin
                root.mkdir(parents=True)
                dashboard.mkdir(exist_ok=True)
                for name in ('runtime_loader.py', 'host_default.py'):
                    (root / name).write_text((source / name).read_text())
                (root / 'byok.py').write_text("from pathlib import Path\ndef _host_scope(): return object(), Path('/profile').resolve()\n")
                (root / 'server.py').write_text("from . import host_default\nrouter = host_default\n")
                (plugin / '__init__.py').write_text((source / '__init__.py').read_text())
                if legacy:
                    (root / '__init__.py').write_text('')
                api = dashboard / 'plugin_api.py'
                api.write_text((source / 'dashboard/plugin_api.py').read_text())
                def load(name, path):
                    spec = importlib.util.spec_from_file_location(name, path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return mod
                entry = load('fixture_entry', plugin / '__init__.py')
                entry.register(self.ctx)
                route = load('fixture_api', api).router
                self.assertTrue(route.catalog_entry()['configured'])
                self.assertEqual(route.completion()([]), 'answer')

    def test_only_explicit_default_mode_uses_host_and_byok_never_falls_back(self):
        import model_selection
        host_default.register(self.ctx)
        with patch('byok.completion', side_effect=ValueError('unavailable')) as byok:
            self.assertEqual(model_selection.selected_completion('hermes', 'default')([]), 'answer')
            byok.assert_not_called()
            for model in (None, '', 'auto', 'named-model'):
                with self.assertRaises(ValueError): model_selection.selected_completion('hermes', model)
            with self.assertRaises(ValueError): model_selection.selected_completion('openai', 'exact-model')
        self.assertEqual(len(self.facade.calls), 1)

    def test_unavailable_default_rejected_before_answer_worker_or_retrieval(self):
        import server
        from unittest.mock import Mock
        factory = Mock()
        state = server.MeetingState(engine_factory=factory)
        try:
            with self.assertRaises(server._AnswerRejected) as error:
                state.assist('synthetic question', answer_provider='hermes', answer_model='default')
            self.assertEqual(error.exception.status_code, 503)
            factory.assert_not_called()
            self.assertEqual(self.facade.calls, [])
        finally:
            state.pause()

    def test_answer_reports_host_metadata_and_current_default_retry_policy(self):
        from knowledge import KnowledgeEngine
        from unittest.mock import Mock
        host_default.register(self.ctx)
        engine = KnowledgeEngine(_completion=Mock(side_effect=AssertionError('no injected fallback')))
        engine.search = Mock(return_value=[])
        engine.projects = Mock(return_value=[])
        result = engine.answer('synthetic question', answer_provider='hermes', answer_model='default')
        self.assertEqual(result['answer'], 'answer')
        self.assertEqual(result['model_route'], 'provider/model')
        self.assertTrue(result['completed_only'])
        self.assertTrue(any('Retrying uses the current default' in row for row in result['warnings']))
        self.assertEqual(len(self.facade.calls), 1)

    def test_runtime_name_root_version_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a', Path(tmp)/'b'
            a.mkdir(); b.mkdir()
            for root in (a,b): (root/'__init__.py').write_text('')
            first = runtime_loader.runtime_name(a)
            self.assertEqual(first, runtime_loader.runtime_name(a))
            self.assertNotEqual(first, runtime_loader.runtime_name(b))
            (a/'new.py').write_text('x = 1')
            self.assertNotEqual(first, runtime_loader.runtime_name(a))

if __name__ == '__main__': unittest.main()
