"""Standalone core acceptance: no Hermes modules, home, or network.

Every test runs with `agent.*` and `tools.*` imports blocked, Hermes
environment variables scrubbed, and a bogus Hermes home — anything reaching
for the host fails loudly instead of passing quietly.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BLOCKED = ('agent', 'agent.auxiliary_client', 'tools', 'tools.web_tools')


class NoHermesTestCase(unittest.TestCase):
    def setUp(self):
        self.saved_env = dict(os.environ)
        for key in ('HERMES_HOME', 'HERMES_PROFILE', 'HERMES_PROFILE_NAME',
                    'MEETING_COPILOT_PROJECT_ROOT', 'MODEL_API_KEY'):
            os.environ.pop(key, None)
        os.environ['HERMES_HOME'] = str(Path(self._scratch()) / 'nope-hermes-home')
        self._import_patcher = mock.patch.dict(sys.modules, {name: None for name in BLOCKED})
        self._import_patcher.start()
        self.addCleanup(self._import_patcher.stop)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    def _scratch(self):
        if not hasattr(self, '_tmp'):
            self._tmp = tempfile.mkdtemp(prefix='core-test-')
            self.addCleanup(__import__('shutil').rmtree, self._tmp, True)
        return self._tmp

    def _docs(self):
        docs = Path(self._scratch()) / 'docs'
        acme = docs / 'acme'
        acme.mkdir(parents=True, exist_ok=True)
        (acme / 'launch.md').write_text(
            '# Launch\nSupplier renews in September. Finance sign-off before August.\n')
        (acme / 'pricing.txt').write_text('Price $84 each, $12,000 minimum.\n')
        return docs

    def _core(self, **kwargs):
        from answer_core import AnswerCore, DisabledSearch, NoHistory
        kwargs.setdefault('completion', lambda messages: 'Cited answer [1].')
        kwargs.setdefault('search', DisabledSearch())
        kwargs.setdefault('history', NoHistory())
        return AnswerCore(project_root=self._docs(), **kwargs)


class CoreAcceptanceTests(NoHermesTestCase):
    def test_recorded_transcript_plus_docs_yields_cited_answer(self):
        core = self._core()
        result = core.answer('What is the price?',
                             transcript='you: What is the price?\nmeeting: See pricing.',
                             project='acme')
        self.assertIn('[1]', result['answer'])
        self.assertTrue(result['sources'])
        self.assertGreaterEqual(result['elapsed_seconds'], 0)

    def test_no_history_is_valid(self):
        core = self._core()
        result = core.answer('What is the price?', transcript='t', project='acme')
        self.assertIn('answer', result)

    def test_search_disabled_makes_zero_web_calls(self):
        calls = []

        def guard(query):
            calls.append(query)
            raise AssertionError('web must never run')
        from answer_core import AnswerCore, NoHistory
        core = AnswerCore(project_root=self._docs(),
                          completion=lambda messages: 'Cited [1].',
                          search=guard, history=NoHistory())
        core.answer('What is the price?', transcript='t', project='acme',
                    web_fallback=True)
        self.assertEqual(calls, [])

    def test_explicit_query_bypasses_planning(self):
        seen = []

        def planning_detector(messages):
            seen.extend(messages)
            return 'Direct [1].'
        core = self._core(completion=planning_detector)
        core.answer('Q?', transcript='t', project='acme', web_query='supplier quote')
        blob = json.dumps(seen)
        self.assertNotIn('needs_web', blob)

    def test_provider_failure_warns_without_credentials(self):
        def boom(messages):
            raise RuntimeError('down:sekret')
        core = self._core(completion=boom)
        result = core.answer('What?', transcript='t', project='acme')
        blob = json.dumps(result, default=str)
        self.assertNotIn('sekret', blob)
        self.assertTrue(result['warnings'])

    def test_out_of_scope_and_symlinks_rejected(self):
        core = self._core()
        with self.assertRaises(ValueError):
            core.search('anything at all', project='../outside')
        link = self._docs() / 'evil'
        try:
            link.symlink_to('/etc')
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError):
            core.search('anything at all', project='evil')

    def test_host_imports_fail_loudly_not_silently(self):
        import hermes_adapters
        with self.assertRaisesRegex(RuntimeError, 'agent.auxiliary_client'):
            hermes_adapters.default_completion()([])
        with self.assertRaisesRegex(RuntimeError, 'tools.web_tools'):
            hermes_adapters.default_search()('q')
        # Missing state.db is simply no history.
        self.assertEqual(hermes_adapters.default_history('/nonexistent-home')(['x']), [])

    def test_standalone_ignores_host_environment(self):
        os.environ['HERMES_HOME'] = '/nonexistent'
        os.environ['HERMES_PROFILE'] = 'other'
        from answer_core import AnswerCore
        core = AnswerCore(project_root=self._docs(),
                          completion=lambda messages: 'Cited [1].')
        result = core.answer('Price?', transcript='t', project='acme')
        self.assertIn('[1]', result['answer'])

    def test_injected_completion_wins_over_env_configured_meta(self):
        import json as _json
        called = []
        def injected(messages):
            called.append(messages)
            return 'Injected [1].'
        with unittest.mock.patch.dict(os.environ, {'MODEL_API_KEY': '0123456789abcdef'}):
            from answer_core import AnswerCore, DisabledSearch, NoHistory
            core = AnswerCore(project_root=self._docs(), completion=injected,
                              search=DisabledSearch(), history=NoHistory())
            result = core.answer('What is the price?', transcript='t', project='acme')
        self.assertTrue(called, 'injected completion must run')
        self.assertEqual(result['answer'], 'Injected [1].')
        self.assertEqual(result['model_route'], 'injected')
        self.assertNotIn('meta', json.dumps(result.get('model_route')))

    def test_no_home_access_with_explicit_root(self):
        import knowledge
        def no_home():
            raise AssertionError('home directory must not be touched')
        with unittest.mock.patch.object(knowledge.Path, 'home', no_home):
            from answer_core import AnswerCore, DisabledSearch, NoHistory
            core = AnswerCore(project_root=self._docs(),
                              completion=lambda messages: 'Cited [1].',
                              search=DisabledSearch(), history=NoHistory())
            result = core.answer('Price?', transcript='t', project='acme')
        self.assertIn('[1]', result['answer'])

class AskCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ask-cli-')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        docs = Path(self.tmp) / 'docs' / 'acme'
        docs.mkdir(parents=True)
        (docs / 'notes.md').write_text('Acme renews in September.', encoding='utf-8')
        self.docs = str(Path(self.tmp) / 'docs')

    def _run(self, *argv):
        import ask
        with self.assertRaises(SystemExit) as ctx:
            ask.main(list(argv))
        return ctx.exception

    def test_missing_docs_dir_is_clean_error(self):
        err = self._run('--question', 'Q?', '--docs', str(Path(self.tmp) / 'nope'),
                        '--provider', 'fake')
        self.assertNotEqual(err.code, 0)
        self.assertIn('Docs directory not found', str(err.code))

    def test_unknown_project_lists_available(self):
        err = self._run('--question', 'Q?', '--docs', self.docs,
                        '--project', 'nope', '--provider', 'fake')
        self.assertIn('acme', str(err.code))

    def test_project_plus_all_rejected(self):
        err = self._run('--question', 'Q?', '--docs', self.docs,
                        '--project', 'acme', '--all', '--provider', 'fake')
        self.assertIn('not both', str(err.code))

    def test_missing_transcript_is_clean_error(self):
        err = self._run('--question', 'Q?', '--docs', self.docs,
                        '--provider', 'fake',
                        '--transcript', str(Path(self.tmp) / 'missing.json'))
        self.assertIn('Cannot read transcript', str(err.code))

    def test_fake_answer_cites_only_retrieved_evidence(self):
        import ask
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ask.main(['--question', 'zzzznomatch', '--docs', self.docs,
                             '--provider', 'fake'])
        self.assertEqual(code, 0)
        self.assertNotIn('[1]', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
