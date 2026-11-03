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
import unittest.mock
from pathlib import Path

BLOCKED = ('agent', 'agent.auxiliary_client', 'tools', 'tools.web_tools')


class NoHermesTestCase(unittest.TestCase):
    def setUp(self):
        self.saved_env = dict(os.environ)
        for key in ('HERMES_HOME', 'HERMES_PROFILE', 'HERMES_PROFILE_NAME',
                    'MEETING_COPILOT_PROJECT_ROOT', 'MODEL_API_KEY'):
            os.environ.pop(key, None)
        os.environ['HERMES_HOME'] = str(Path(self._scratch()) / 'nope-hermes-home')
        self._import_patcher = unittest.mock.patch.dict(sys.modules, {name: None for name in BLOCKED})
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

    def test_injected_streaming_completion_streams(self):
        def complete(messages):
            return 'Full [1].'
        def chunks(messages):
            yield 'Part '
            yield 'one [1].'
        complete.stream_chunks = chunks
        core = self._core(completion=complete)
        self.assertTrue(core.engine.supports_streaming())
        header, deltas = core.engine.answer_stream('What is the price?', transcript='t', project='acme')
        self.assertEqual(list(deltas), ['Part ', 'one [1].'])
        self.assertEqual(header['model_route'], 'injected')

    def test_streaming_probe_follows_override_despite_env_key(self):
        import os as _os
        def plain(messages):
            return 'Cited [1].'
        def streamer(messages):
            return 'Full [1].'
        def chunks(messages):
            yield 'Part [1].'
        streamer.stream_chunks = chunks
        with unittest.mock.patch.dict(_os.environ, {'MODEL_API_KEY': '0123456789abcdef'}):
            plain_core = self._core(completion=plain)
            stream_core = self._core(completion=streamer)
            self.assertFalse(plain_core.engine.supports_streaming())
            self.assertTrue(stream_core.engine.supports_streaming())

    def test_injected_plain_completion_reports_completed_only(self):
        core = self._core(completion=lambda messages: 'Cited [1].')
        self.assertFalse(core.engine.supports_streaming())
        with self.assertRaises(NotImplementedError):
            core.engine.answer_stream('What is the price?', transcript='t', project='acme')

    def test_explicit_missing_project_root_fails_clearly(self):
        from answer_core import AnswerCore
        with self.assertRaisesRegex(ValueError, 'Project root not found'):
            AnswerCore(project_root=str(Path(self._scratch()) / 'missing'),
                       completion=lambda messages: 'Cited [1].')

    def test_empty_injected_completion_is_a_safe_provider_failure(self):
        core = self._core(completion=lambda messages: '   ')
        result = core.answer('Price?', transcript='t', project='acme')
        self.assertTrue(result['provider_error'])
        self.assertTrue(result['warnings'])
        self.assertNotIn('Cited', result['answer'])

    def test_history_provider_returning_none_is_no_history(self):
        class NoneHistory:
            def search(self, terms):
                return None
        core = self._core(history=NoneHistory())
        result = core.answer('Price?', transcript='t', project='acme')
        self.assertIn('[1]', result['answer'])

    def test_omitted_project_root_never_falls_back_to_home(self):
        import knowledge
        def no_home():
            raise AssertionError('home must not be touched')
        with unittest.mock.patch.object(knowledge.Path, 'home', no_home):
            from answer_core import AnswerCore
            with self.assertRaisesRegex(ValueError, 'project_root is required'):
                AnswerCore(completion=lambda messages: 'Cited [1].')

    def test_demo_import_does_not_mutate_environment(self):
        import subprocess
        import sys as _sys
        env = dict(os.environ)
        env['MODEL_API_KEY'] = 'sentinel-value'
        result = subprocess.run(
            [_sys.executable, '-c',
             'import os, demo_standalone; print(os.environ.get("MODEL_API_KEY"))'],
            cwd=str(Path(__file__).resolve().parent), env=env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stdout.strip(), 'sentinel-value')

    def test_injected_history_is_redacted_like_host_history(self):
        captured = []
        def complete(messages):
            captured.extend(messages)
            return 'Cited [1].'
        class SecretHistory:
            def search(self, terms):
                return [{'id': 'h1 password=hunter2', 'title': 'note password=hunter2',
                         'source': 'history:test token: hunter2',
                         'text': 'quasar password=hunter2 token: hunter2 '
                                 'Authorization: Bearer sentinel987',
                         'updated_at': None, 'extra': 'password=hunter2'}]
        core = self._core(completion=complete, history=SecretHistory())
        result = core.answer('quasar price?', transcript='t', project='acme')
        self.assertNotIn('hunter2', json.dumps(captured))
        self.assertNotIn('hunter2', json.dumps(result, default=str))
        self.assertNotIn('sentinel987', json.dumps(result, default=str))
        self.assertNotIn('extra', json.dumps(result, default=str))

    def test_answer_stream_rejects_provider_override(self):
        core = self._core()
        with self.assertRaisesRegex(ValueError, 'not supported by AnswerCore'):
            core.answer_stream('Price?', answer_provider='meta')
        with self.assertRaisesRegex(ValueError, 'not supported by AnswerCore'):
            core.answer_stream('Price?', answer_model='anything')
        with self.assertRaisesRegex(ValueError, 'not supported by AnswerCore'):
            core.answer_stream('Price?', 't', 'acme', None, False, False, 'meta')

    def test_ordinary_token_wording_is_not_redacted(self):
        import knowledge
        self.assertEqual(knowledge._clean('Token: meeting agenda'), 'Token: meeting agenda')
        self.assertEqual(knowledge._clean('Bearer investment strategy discussion'),
                         'Bearer investment strategy discussion')
        self.assertIn('[REDACTED]', knowledge._clean('Authorization: Bearer sentinel987'))

    def test_injected_history_updated_at_is_scrubbed(self):
        class History:
            def search(self, terms):
                return [{'id': 'h1', 'title': 'note', 'source': 'history:test',
                         'text': 'quasar note', 'updated_at': 'Bearer abcdefghijklmnop'}]
        core = self._core(history=History())
        result = core.answer('quasar price?', transcript='t', project='acme')
        self.assertNotIn('abcdefghijklmnop', json.dumps(result, default=str))

    def test_web_result_urls_are_sanitized(self):
        def search(query):
            return [{'url': 'https://user:passw0rd@example.com/x?api_key=sekret99&ok=1',
                     'title': 'Result', 'description': 'quasar result'}]
        core = self._core(search=search)
        result = core.answer('quasar price?', transcript='t', project='acme',
                             web_query='quasar public')
        blob = json.dumps(result, default=str)
        self.assertNotIn('sekret99', blob)
        self.assertNotIn('passw0rd', blob)
        self.assertIn('ok=1', blob)

    def test_lazy_stream_errors_are_sanitized(self):
        def complete(messages):
            return 'Full [1].'
        def chunks(messages):
            yield 'Part '
            raise RuntimeError('boom password=sentinel987')
        setattr(complete, 'stream_chunks', chunks)
        core = self._core(completion=complete)
        _, deltas = core.engine.answer_stream('Price?', transcript='t', project='acme')
        with self.assertRaises(RuntimeError) as ctx:
            list(deltas)
        self.assertNotIn('sentinel987', str(ctx.exception))
        self.assertIn('Model request failed', str(ctx.exception))


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

    def test_provider_failure_exits_nonzero_without_leaking(self):
        import ask
        import io
        from contextlib import redirect_stdout, redirect_stderr
        def boom(messages):
            raise RuntimeError('down:sekret-token')
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(ask, 'build_completion', return_value=boom):
            with redirect_stdout(out), redirect_stderr(err):
                code = ask.main(['--question', 'Q?', '--docs', self.docs,
                                 '--provider', 'meta'])
        self.assertEqual(code, 2)
        self.assertNotIn('sekret-token', out.getvalue() + err.getvalue())

    def test_meta_without_key_is_clean_error(self):
        import ask
        with unittest.mock.patch.dict('os.environ', {}, clear=False):
            import os as _os
            _os.environ.pop('MODEL_API_KEY', None)
            with self.assertRaises(SystemExit) as ctx:
                ask.main(['--question', 'Q?', '--docs', self.docs, '--provider', 'meta'])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn('MODEL_API_KEY', str(ctx.exception.code))

    def _capture(self, argv):
        import ask
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ask.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_projects_needs_no_question(self):
        code, out, _ = self._capture(['--docs', self.docs, '--list-projects'])
        self.assertEqual(code, 0)
        self.assertIn('acme', out)
        self.assertIn('Ask one:', out)

    def test_search_mode_returns_evidence_without_model(self):
        code, out, _ = self._capture(['--docs', self.docs, '--search', 'Acme renews'])
        self.assertEqual(code, 0)
        self.assertIn('notes.md', out)
        self.assertIn('Acme renews in September.', out)

    def test_search_json_shape(self):
        import json as _json
        code, out, _ = self._capture(['--docs', self.docs, '--search', 'Acme renews', '--json'])
        self.assertEqual(code, 0)
        payload = _json.loads(out)
        self.assertEqual(payload['query'], 'Acme renews')
        self.assertEqual(payload['count'], len(payload['sources']))
        self.assertGreaterEqual(payload['count'], 1)

    def test_no_action_shows_three_choices(self):
        err = self._run('--docs', self.docs)
        self.assertIn('Ask:', str(err.code))
        self.assertIn('Search docs:', str(err.code))
        self.assertIn('List projects:', str(err.code))

    def test_missing_docs_points_to_quickstart(self):
        err = self._run('--list-projects')
        self.assertIn('Missing --docs', str(err.code))
        self.assertIn('--list-projects', str(err.code))

    def test_version_flag_works_without_docs_or_question(self):
        import ask
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                ask.main(['--version'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('meeting-copilot standalone', out.getvalue())

    def test_transcript_dash_reads_stdin(self):
        import ask
        import io
        payload = io.StringIO('[{"speaker": "you", "text": "Hello there"}]')
        with unittest.mock.patch.object(ask.sys, 'stdin', payload):
            self.assertEqual(ask.load_transcript('-'), 'you: Hello there')

    def test_json_provider_failure_exits_nonzero(self):
        import ask
        import io
        from contextlib import redirect_stdout, redirect_stderr
        def boom(messages):
            raise RuntimeError('down:sekret-token')
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(ask, 'build_completion', return_value=boom):
            with redirect_stdout(out), redirect_stderr(err):
                code = ask.main(['--question', 'Q?', '--docs', self.docs,
                                 '--provider', 'meta', '--json'])
        self.assertEqual(code, 2)
        self.assertNotIn('sekret-token', out.getvalue() + err.getvalue())

    def test_model_flag_warns_when_fake(self):
        code, _, err = self._capture(['--question', 'Q?', '--docs', self.docs,
                                      '--provider', 'fake', '--model', 'anything'])
        self.assertEqual(code, 0)
        self.assertIn('--model is ignored', err)

    def test_human_output_explains_empty_sources(self):
        code, out, _ = self._capture(['--question', 'zzzznomatch', '--docs', self.docs,
                                      '--provider', 'fake'])
        self.assertEqual(code, 0)
        self.assertIn('Sources: none found', out)

    def test_ambiguous_modes_are_rejected(self):
        err = self._run('--docs', self.docs, '--list-projects', '--search', 'Acme')
        self.assertIn('Choose one mode', str(err.code))
        err = self._run('--docs', self.docs, '--list-projects', '--project', 'acme', '--all')
        self.assertIn('not both', str(err.code))

    def test_json_preflight_errors_are_json(self):
        import ask
        import io
        import json as _json
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict('os.environ', {}, clear=False):
            import os as _os
            _os.environ.pop('MODEL_API_KEY', None)
            with redirect_stdout(out), redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    ask.main(['--question', 'hi', '--docs', self.docs,
                              '--provider', 'meta', '--json'])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn('MODEL_API_KEY', _json.loads(out.getvalue())['error'])

    def test_search_warns_transcript_is_ignored(self):
        code, _, err = self._capture(['--docs', self.docs, '--search', 'Acme renews',
                                      '--transcript', '/nonexistent'])
        self.assertEqual(code, 0)
        self.assertIn('--transcript is ignored', err)

    def test_empty_search_terms_rejected(self):
        err = self._run('--docs', self.docs, '--search', '')
        self.assertIn('cannot be empty', str(err.code))

    def test_list_projects_warns_project_is_ignored(self):
        code, _, err = self._capture(['--docs', self.docs, '--list-projects',
                                      '--project', 'nonsense'])
        self.assertEqual(code, 0)
        self.assertIn('--project is ignored', err)

    def test_fake_mode_restores_caller_environment(self):
        import ask
        import io
        from contextlib import redirect_stdout
        with unittest.mock.patch.dict('os.environ', {'MODEL_API_KEY': 'sentinel-value'}):
            with redirect_stdout(io.StringIO()):
                code = ask.main(['--question', 'Q?', '--docs', self.docs,
                                 '--provider', 'fake'])
            self.assertEqual(code, 0)
            self.assertEqual(os.environ.get('MODEL_API_KEY'), 'sentinel-value')

    def test_parser_errors_are_json_when_requested(self):
        import ask
        import io
        import json as _json
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                ask.main(['--docs', self.docs, '--question', 'hi',
                          '--json', '--limit', 'oops'])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('invalid int value', _json.loads(out.getvalue())['error'])

    def test_limit_warns_outside_search_mode(self):
        code, _, err = self._capture(['--question', 'Q?', '--docs', self.docs,
                                      '--limit', '1'])
        self.assertEqual(code, 0)
        self.assertIn('--limit applies only to --search', err)

    def test_abbreviated_json_flag_still_gets_json_parser_errors(self):
        import ask
        import io
        import json as _json
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                ask.main(['--docs', self.docs, '--question', 'hi',
                          '--j', '--limit', 'oops'])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('invalid int value', _json.loads(out.getvalue())['error'])

    def test_list_conflict_has_no_ignored_warning(self):
        import ask
        import io
        import json as _json
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit):
                ask.main(['--docs', self.docs, '--list-projects', '--project',
                          'acme', '--all', '--json'])
        self.assertNotIn('ignored', err.getvalue())
        self.assertIn('not both', _json.loads(out.getvalue())['error'])


class RetrievalEvalTests(unittest.TestCase):
    def test_synthetic_benchmark_reports_recall_and_latency(self):
        import eval_retrieval
        report = eval_retrieval.run_benchmark(projects=3, docs_per_project=2, runs=2)
        self.assertEqual(report['recall_at_k']['exact_reference'], 1.0)
        self.assertEqual(report['recall_at_k']['natural_topic'], 1.0)
        self.assertEqual(report['recall_at_k']['marker_free_detail'], 1.0)
        self.assertEqual(report['ranking_parity']['all_vs_single_top1'], 1.0)
        self.assertEqual(report['search_latency_ms']['count'], 3)
        self.assertIn('p95', report['answer_latency_ms_fake_provider'])


if __name__ == '__main__':
    unittest.main()
