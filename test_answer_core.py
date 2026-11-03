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

    def test_provider_not_implemented_errors_are_sanitized(self):
        def setup_boom(messages):
            raise NotImplementedError('secret=example-secret-123')
        core = self._core(completion=setup_boom)
        core.engine._completion_override = setup_boom
        def stream_setup(messages):
            raise NotImplementedError('secret=example-secret-123')
        setattr(setup_boom, 'stream_chunks', stream_setup)
        with self.assertRaises(NotImplementedError) as ctx:
            core.engine.answer_stream('Price?', transcript='t', project='acme')
        self.assertNotIn('example-secret-123', str(ctx.exception))
        self.assertIn('streaming_unsupported', str(ctx.exception))

    def test_provider_object_keeps_stream_chunks(self):
        class Provider:
            def complete(self, messages):
                return 'Full [1].'
            def stream_chunks(self, messages):
                yield 'Streamed [1].'
        core = self._core(completion=Provider())
        self.assertTrue(core.engine.supports_streaming())
        _, deltas = core.engine.answer_stream('Price?', transcript='t', project='acme')
        self.assertEqual(list(deltas), ['Streamed [1].'])

    def test_url_cleaning_preserves_ordinary_urls(self):
        import knowledge
        ordinary = 'https://example.com/x?q=hello%20world&flag'
        self.assertEqual(knowledge._clean_url(ordinary), ordinary)
        cleaned = knowledge._clean_url('https://example.com/x?access_token=sekret99&ok=1')
        self.assertNotIn('sekret99', cleaned)
        self.assertIn('ok=1', cleaned)
        self.assertEqual(knowledge._clean('Bearer identification documents'),
                         'Bearer identification documents')

    def test_url_encoded_and_fragment_keys_are_redacted(self):
        import knowledge
        self.assertNotIn('secret123', knowledge._clean_url(
            'https://example.com/x?access%5Ftoken=secret123'))
        self.assertNotIn('secret123', knowledge._clean_url(
            'https://example.com/x#access_token=secret123'))
        self.assertNotIn('secret123', knowledge._clean_url(
            'https://example.com/x#refresh_token=secret123'))
        self.assertNotIn('secret123', knowledge._clean_url(
            'https://example.com/x?refresh_token=secret123'))
        self.assertNotIn('secret123', knowledge._clean_url(
            'https://example.com/x?refresh%5Ftoken=secret123'))

    def test_round8_credential_and_benign_shapes(self):
        import knowledge
        secrets = [
            knowledge._clean('api key: secretA123'),
            knowledge._clean('Cookie: csrftoken=secretB123; theme=dark'),
            knowledge._clean('{"token":"secretE"}'),
            knowledge._clean('password: "abc def ghi"'),
            knowledge._clean_url('https://host/x?sessionid=secretC123&X-Amz-Credential=secretD123'),
            knowledge._clean_url('https://host/x?api%5Fkey%3Dabc12345'),
            knowledge._clean_url('https://host/x?ok=1%26refresh_token=abc12345'),
        ]
        blob = ' '.join(secrets)
        for sentinel in ('secretA123', 'secretB123', 'secretE', 'abc def',
                         'secretC123', 'secretD123', 'abc12345'):
            self.assertNotIn(sentinel, blob)
        # Benign content survives byte-for-byte.
        self.assertEqual(knowledge._clean('session 2026-11-02 planning'),
                         'session 2026-11-02 planning')
        self.assertEqual(knowledge._clean('Meeting at 10:30@home'),
                         'Meeting at 10:30@home')
        self.assertEqual(knowledge._clean_url('https://example.com/?q=dogs%26cats'),
                         'https://example.com/?q=dogs%26cats')

    def test_round23b_stream_and_api_key_boundaries(self):
        import knowledge
        self.assertEqual(knowledge._clean('api_key=x'), 'api_key=[REDACTED]')
        self.assertEqual(knowledge._clean('api key: zebra'), 'api key: [REDACTED]')
        self.assertEqual(knowledge._clean('API key: architecture'), 'API key: architecture')
        # Distinct iterator from iter() is closed; throwing close attrs
        # cannot escape; cancellation lands between items.
        events = []
        class DistinctIter:
            def __iter__(self):
                return self
            def __next__(self):
                return 'chunk [1].'
            def close(self):
                events.append('iter')
        class Iterable:
            def __iter__(self):
                return DistinctIter()
            def close(self):
                events.append('chunks')
        class Provider:
            def __call__(self, messages):
                return 'x [1].'
            def stream_chunks(self, messages):
                return Iterable()
        core = self._core(completion=Provider())
        _header, stream = core.answer_stream('What is the price?', project='acme')
        next(stream)
        stream.close()
        self.assertEqual(events, ['iter', 'chunks'])

        state = {'abort': False}
        class Provider2:
            def __call__(self, messages):
                return 'x [1].'
            def stream_chunks(self, messages):
                return iter(['one [1].', 'two [1].'])
        core2 = self._core(completion=Provider2())
        _header, stream2 = core2.answer_stream(
            'What is the price?', project='acme',
            should_abort=lambda: state['abort'])
        self.assertEqual(next(stream2), 'one [1].')
        state['abort'] = True
        with self.assertRaises(knowledge._Aborted):
            next(stream2)

    def test_fake_response_must_follow_the_question(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'What is alpha beta?',
                   'transcript': 'Yes, the budget was approved.\nWhat is alpha beta?',
                   'evidence': []})
        self.assertNotIn('budget was approved', out)
        out = run({'question': 'What is alpha beta?',
                   'transcript': 'What is alpha beta?\nYes, it shipped today.',
                   'evidence': []})
        self.assertIn('shipped today', out)

    def test_round23_spaced_labels_and_stream_iteration(self):
        import knowledge
        self.assertEqual(knowledge._clean('refresh token "SessionSecret-42"'),
                         'refresh token [REDACTED]')
        self.assertNotIn('SessionSecret-42', knowledge._clean(
            'refresh token SessionSecret-42'))
        self.assertEqual(knowledge._clean('refresh token is required'),
                         'refresh token is required')
        self.assertEqual(knowledge._clean('session id=alpha beta'),
                         'session id=alpha beta')
        self.assertNotIn('SessionSecret-42', knowledge._clean(
            'session id=SessionSecret-42'))
        self.assertNotIn('SECRET123456', knowledge._clean(
            'abcdefghijklmnopqrstuvwxyzabcdefghij_token=SECRET123456'))
        # Iterator creation failures are sanitized and close the provider.
        closed = []
        class Broken:
            def __iter__(self):
                raise RuntimeError('provider SECRET123')
            def close(self):
                closed.append(True)
        class Provider:
            def __call__(self, messages):
                return 'x [1].'
            def stream_chunks(self, messages):
                return Broken()
        core = self._core(completion=Provider())
        _header, stream = core.answer_stream('What is the price?', project='acme')
        with self.assertRaises(RuntimeError) as ctx:
            next(stream)
        self.assertNotIn('SECRET123', str(ctx.exception))
        self.assertEqual(closed, [True])

    def test_round22_cleaner_and_stream_boundaries(self):
        import knowledge, time
        self.assertEqual(knowledge._clean('api_key: zebra'), 'api_key: [REDACTED]')
        self.assertNotIn('SECRET123456', knowledge._clean(
            'https://example.com/?q=token%2525252525252525253DSECRET123456'))
        for sample in ('authorization' + ' ' * 16384 + 'X',
                       'Cookie: ' + 'a' * 16384, 'a-' * 8192 + '!'):
            started = time.perf_counter()
            knowledge._clean(sample)
            self.assertLess(time.perf_counter() - started, 2.0)

    def test_stream_close_before_iteration_closes_provider(self):
        closed = []
        class ProviderIter:
            def __iter__(self):
                return iter(['chunk [1].'])
            def close(self):
                closed.append(True)
        class Streaming:
            def __call__(self, messages):
                raise AssertionError('non-stream path must not be used')
            def stream_chunks(self, messages):
                return ProviderIter()
        core = self._core(completion=Streaming())
        _header, stream = core.answer_stream('What is the price?', project='acme')
        stream.close()
        self.assertEqual(closed, [True])

    def test_fake_response_marker_boundaries(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'What is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'Is alpha beta approved? The budget discussion is tomorrow.'}]})
        self.assertIn('does not directly answer', out)
        out = run({'question': 'What is alpha beta?',
                   'transcript': 'Is alpha beta approved? Yes, it shipped today.',
                   'evidence': []})
        self.assertIn('shipped today', out)
        out = run({'question': 'What is alpha beta?',
                   'transcript': 'Alpha beta launched Monday. Is alpha beta approved?',
                   'evidence': []})
        self.assertIn('launched Monday', out)
        self.assertNotIn('Is alpha beta approved?', out)
        out = run({'question': 'What is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'Is alpha beta approved? Alice: Yes, it shipped today.'}]})
        self.assertIn('shipped today', out)

    def test_round21_cleaner_shapes_and_perf(self):
        import knowledge, time
        self.assertNotIn('SENSITIVE123', knowledge._clean(
            "Explain password='abc\\' SENSITIVE123' launch"))
        self.assertEqual(knowledge._clean('API key: architecture'), 'API key: architecture')
        self.assertNotIn('secretA123', knowledge._clean('API key: secretA123'))
        self.assertNotIn('abc12345', knowledge._clean('api_key=abc12345'))
        out = knowledge._clean('https://example.com/?q=%' + '25' * 1000 + '41')
        self.assertIsInstance(out, str)
        started = time.perf_counter()
        knowledge._clean('%22' * 1000 + 'token')
        knowledge._clean('-' * 16000 + 'a')
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_stream_close_closes_provider_iterator(self):
        closed = []
        class ProviderIter:
            def __iter__(self):
                return iter(['chunk [1].'])
            def close(self):
                closed.append(True)
        class Streaming:
            def __call__(self, messages):
                raise AssertionError('non-stream path must not be used')
            def stream_chunks(self, messages):
                return ProviderIter()
        core = self._core(completion=Streaming())
        _header, stream = core.answer_stream('What is the price?', project='acme')
        iterator = iter(stream)
        next(iterator)
        iterator.close()
        self.assertEqual(closed, [True])

    def test_fake_sentence_level_question_filtering(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'What is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'We discussed other things. Is alpha beta approved or still waiting for legal signoff?'}]})
        self.assertIn('does not directly answer', out)
        out = run({'question': 'What is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'Is alpha beta approved? Yes, it shipped today.'}]})
        self.assertIn('shipped today', out)

    def test_round20_terms_come_from_cleaned_question(self):
        import knowledge
        seen = []
        class Hist:
            def search(self, terms):
                seen.extend(terms)
                return []
        core = self._core(completion=lambda messages: 'x [1].', history=Hist())
        for query in ('password=hunter2 price', 'jwt=ab price',
                      'token="super secret phrase" price', 'jwt%3DalphaSecret price'):
            seen.clear()
            core.search(query, project='acme')
            joined = ' '.join(seen)
            for sentinel in ('hunter2', 'alphasecret', 'super', 'phrase'):
                self.assertNotIn(sentinel, joined, (query, seen))
        seen.clear()
        core.search('JWT: authentication workflow', project='acme')
        self.assertIn('authentication', seen)
        # Cleaner performance stays linear on unbroken alphanumeric text.
        import time
        started = time.perf_counter()
        self.assertEqual(knowledge._clean('a' * 16000), 'a' * 16000)
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_fake_unpunctuated_question_and_many_repetitions(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'Is alpha beta approved?',
                   'transcript': 'Is alpha beta approved or still awaiting review',
                   'evidence': []})
        self.assertNotIn('transcript itself says', out)
        out = run({'question': 'What is alpha beta?',
                   'transcript': 'Alice: ' + 'What is alpha beta? ' * 101 + 'alpha beta shipped',
                   'evidence': []})
        self.assertIn('alpha beta shipped', out)

    def test_round19_credential_and_benign_shapes(self):
        import knowledge
        self.assertEqual(knowledge._clean('jwt: alphaSecret'), 'jwt: [REDACTED]')
        self.assertEqual(knowledge._clean('jwt=ab'), 'jwt=[REDACTED]')
        self.assertEqual(knowledge._clean('JWT: authentication'), 'JWT: authentication')
        self.assertEqual(knowledge._clean('Token: launch-plan.md'), 'Token: launch-plan.md')
        self.assertEqual(knowledge._clean('Token: "release 2026"'), 'Token: "release 2026"')
        self.assertEqual(knowledge._clean_url('https://example.com/?signature=design&sessionid=42'),
                         'https://example.com/?signature=design&sessionid=42')
        self.assertNotIn('abcDEF123456', knowledge._clean_url(
            'https://example.com/?signature=abcDEF123456'))

    def test_benign_token_label_terms_still_retrieve(self):
        seen = []
        class Hist:
            def search(self, terms):
                seen.extend(terms)
                return []
        core = self._core(completion=lambda messages: 'x [1].', history=Hist())
        core.search('Token: Q3-planning', project='acme')
        self.assertIn('q3', seen)
        self.assertIn('planning', seen)

    def test_fake_question_forms_are_not_answers(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'What is alpha beta?',
                   'transcript': 'Alice: ' + 'What is alpha beta? ' * 5, 'evidence': []})
        self.assertNotIn('What is alpha beta?', out)
        out = run({'question': 'What is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'What is alpha beta? Pending.'}]})
        self.assertIn('does not directly answer', out)
        out = run({'question': 'What is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'Alpha beta launched Monday. Any questions?'}]})
        self.assertIn('launched Monday', out)

    def test_round18_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('{"jwt":"SENTINEL123xyz"}'),
            knowledge._clean('{%22jwt%22%3A%22SENTINEL123xyz%22}'),
            knowledge._clean('{"jwt":"SENTINEL alpha beta"}'),
            knowledge._clean('{%22private_key%22%3A%22SENTINEL123xyz%22}'),
        ])
        self.assertNotIn('SENTINEL', blob)
        self.assertEqual(knowledge._clean('session_id=42'), 'session_id=42')
        self.assertEqual(knowledge._clean('session_id=abc'), 'session_id=[REDACTED]')
        self.assertEqual(knowledge._clean('JWT: authentication'), 'JWT: authentication')
        self.assertEqual(knowledge._clean('Session: Q3-planning'), 'Session: Q3-planning')
        self.assertEqual(knowledge._clean('Token: 2024/03/01'), 'Token: 2024/03/01')

    def test_history_bare_secret_exception_is_generic(self):
        class BareHist:
            def search(self, terms):
                raise RuntimeError('SENTINELBARESECRET')
        core = self._core(history=BareHist())
        with self.assertRaises(RuntimeError) as ctx:
            core.search('price', project='acme')
        self.assertNotIn('SENTINELBARESECRET', str(ctx.exception))
        self.assertIn('History provider failed', str(ctx.exception))

    def test_fake_repeated_and_extended_questions_are_not_answers(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'What is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'What is alpha beta and when will it launch?'}]})
        self.assertIn('does not directly answer', out)
        out = run({'question': 'What is alpha beta?',
                   'transcript': 'Alice: What is alpha beta? What is alpha beta?',
                   'evidence': []})
        self.assertNotIn('What is alpha beta?', out)

    def test_round17_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('jwt=abcDEF123456'),
            knowledge._clean('private_key%3DabcDEF123456'),
            knowledge._clean_url('https://example.com/?private_key=abcDEF123456&ok=1'),
            knowledge._clean('Authorization: ApiKey abcDEF123456'),
            knowledge._clean('Cookie: auth="abc def ghi"; theme=dark'),
            knowledge._clean('token: ABCDEF123456'),
            knowledge._clean('session: abcdef123456'),
        ])
        for sentinel in ('abcDEF123456', 'abc def', 'ABCDEF123456', 'abcdef123456'):
            self.assertNotIn(sentinel, blob)
        self.assertEqual(knowledge._clean('Session: "2024/01/01"'), 'Session: "2024/01/01"')
        self.assertEqual(knowledge._clean('Token: "ERC-20"'), 'Token: "ERC-20"')
        # Cookie-context session values are credentials even when
        # date-shaped; the bare label form stays preserved.
        self.assertEqual(knowledge._clean('Cookie: session=2024Q3; theme=dark'),
                         'Cookie: session=[REDACTED]; theme=dark')
        self.assertEqual(knowledge._clean('session=2024Q3'), 'session=2024Q3')
        self.assertEqual(knowledge._clean_url('https://example.com/docs#key=overview'),
                         'https://example.com/docs#key=overview')

    def test_fake_same_line_and_timestamped_echoes(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'what is alpha beta?',
                   'transcript': 'Alice: what is alpha beta? approved for launch',
                   'evidence': []})
        # Same-line response remainder after the stripped question counts.
        self.assertIn('approved for launch', out)
        self.assertNotIn('what is alpha beta?', out.split('says:')[-1])
        out = run({'question': 'what is alpha beta?',
                   'transcript': '[10:00] Alice: what is alpha beta?\nBob: alpha beta approved',
                   'evidence': []})
        self.assertIn('alpha beta approved', out)
        self.assertNotIn('what is alpha beta?', out)
        out = run({'question': 'what is alpha beta?', 'transcript': '',
                   'evidence': [{'citation': 1, 'title': 'd', 'text': 'What is alpha beta?'}]})
        self.assertIn('does not directly answer', out)

    def test_round16_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('session=AbCd1234/zzzz'),
            knowledge._clean('Cookie: AWSALB=abcdefghijk; theme=dark'),
            knowledge._clean('Cookie: auth = secretValue; theme = dark'),
            knowledge._clean('{"private_key":"abcDEF123456"}'),
            knowledge._clean('token: abcDEF123456 is active'),
            knowledge._clean('session: AbCd1234 for production'),
            knowledge._clean_url('https://example.com/?jwt=abc123xyz&ok=1'),
        ])
        for sentinel in ('AbCd1234', 'abcdefghijk', 'secretValue', 'abcDEF123456', 'abc123xyz'):
            self.assertNotIn(sentinel, blob)
        self.assertEqual(knowledge._clean('Cookie: color=blue2026'), 'Cookie: color=blue2026')
        self.assertEqual(knowledge._clean('Token: cost-per-token'), 'Token: cost-per-token')
        self.assertEqual(knowledge._clean('Session: 2026/11/02'), 'Session: 2026/11/02')

    def test_round15_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('phpsessid%3DabcDEF123456'),
            knowledge._clean('%2522phpsessid%2522%253A%2522abcDEF123456%2522'),
            knowledge._clean('Cookie: AWSALB=abcDEF123456; theme=dark'),
            knowledge._clean('token: abcdef123456'),
            knowledge._clean('session: AbCd1234'),
        ])
        self.assertNotIn('abcDEF123456', blob)
        self.assertNotIn('abcdef123456', blob)
        self.assertNotIn('AbCd1234', blob)
        self.assertIn('theme=dark', blob)
        self.assertEqual(knowledge._clean('session=planning/2027Q1'),
                         'session=planning/2027Q1')
        self.assertEqual(knowledge._clean('Session: planning/2027Q1'),
                         'Session: planning/2027Q1')
        self.assertEqual(knowledge._clean('Token: LongTerm2027 roadmap'),
                         'Token: LongTerm2027 roadmap')

    def test_fake_exact_tokens_question_echo_and_line_windows(self):
        import ask
        complete = ask.fake_completion_factory()
        def run(payload):
            return complete([{'role': 'user', 'content': json.dumps(payload)}])
        out = run({'question': 'alpha beta', 'transcript': 'alpha beta now approved',
                   'evidence': [{'citation': 1, 'title': 'd',
                                 'text': 'alphabet betamax unrelated'}]})
        self.assertIn('transcript itself says', out)
        # A response-word line after an echoed question ('approved ...')
        # counts as the transcript's answer; the question itself does not.
        out = run({'question': 'what is alpha beta?',
                   'transcript': 'what is alpha beta?\napproved for launch',
                   'evidence': []})
        self.assertIn('approved for launch', out)
        self.assertNotIn('what is alpha beta?', out.split('says:')[-1])
        # With speaker labels the echo is still ignored, and a following
        # matching line is used.
        out = run({'question': 'what is alpha beta?',
                   'transcript': 'Alice: what is alpha beta?\nBob: alpha beta approved',
                   'evidence': []})
        self.assertIn('alpha beta approved', out)
        self.assertNotIn('Alice: what is', out)
        out = run({'question': 'when is alpha beta approved',
                   'transcript': 'Alice: alpha\nBob: beta is approved',
                   'evidence': []})
        self.assertIn('beta is approved', out)

    def test_round14_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('Cookie: PHPSESSID=abc123; theme=dark'),
            knowledge._clean_url('https://example.com/?jsessionid=abcXYZ123'),
            knowledge._clean('password%253DabcXYZ123'),
            knowledge._clean('%257B%2522session_id%2522%253A%2522abcXYZ123%2522%257D'),
            knowledge._clean('token: abcXYZ123'),
            knowledge._clean('token%3Dabc%26public%3Dhello'),
            knowledge._clean('token=abcXYZ123&public=hello'),
        ])
        self.assertNotIn('abcXYZ123', blob)
        self.assertNotIn('abc123', blob.replace('REDACTED', ''))
        self.assertIn('public%3Dhello', blob)
        self.assertIn('public=hello', blob)
        self.assertEqual(knowledge._clean('Session: 2027Q1'), 'Session: 2027Q1')
        self.assertEqual(knowledge._clean('Session: product-roadmap'),
                         'Session: product-roadmap')
        self.assertEqual(knowledge._clean_url('https://example.com/?session=2027Q1'),
                         'https://example.com/?session=2027Q1')

    def test_fake_separated_doc_terms_lose_to_adjacent_transcript(self):
        import ask
        complete = ask.fake_completion_factory()
        sep_doc = 'alpha ' + ('filler ' * 80) + ' beta ' + ('filler ' * 80) + ' gamma'
        payload = {'question': 'alpha beta gamma',
                   'transcript': 'alpha beta are current',
                   'evidence': [{'citation': 1, 'title': 'doc', 'text': sep_doc}]}
        out = complete([{'role': 'user', 'content': json.dumps(payload)}])
        self.assertIn('transcript itself says', out)

    def test_round13_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('session_id=abc'),
            knowledge._clean('credential=abc'),
            knowledge._clean('token%3Dxyz password%3Dabc'),
            knowledge._clean('Cookie: JSESSIONID=abc; auth=xyz; theme=dark'),
            knowledge._clean('%7B%22password%22%3A%22a%2522b%22%7D'),
            knowledge._clean_url('https://example.com/?q=https%3A%2F%2Fu%3Ap%40example.com'),
        ])
        for sentinel in ('xyz', 'a%2522b', ':p@'):
            self.assertNotIn(sentinel, blob)
        self.assertEqual(knowledge._clean('Session 2027Q1 planning'),
                         'Session 2027Q1 planning')
        self.assertEqual(knowledge._clean('Token: roadmap2027'), 'Token: roadmap2027')
        self.assertEqual(knowledge._clean_url('https://example.com/report?session=planning'),
                         'https://example.com/report?session=planning')

    def test_history_iteration_is_bounded(self):
        consumed = []
        def many(terms):
            for i in range(10000):
                consumed.append(i)
                yield {'id': str(i), 'title': 't', 'source': 's', 'text': 'price'}
        class ManyHist:
            def search(self, terms):
                return many(terms)
        core = self._core(history=ManyHist())
        core.search('price', project='acme')
        self.assertLessEqual(len(consumed), 100)

    def test_round12_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('%22token%22%3A%22abc%2Fdef%22'),
            knowledge._clean('Cookie: session=x; session_id=y; theme=dark'),
        ])
        self.assertNotIn('abc%2Fdef', blob)
        self.assertEqual(blob.count('REDACTED'), 3, blob)
        self.assertEqual(knowledge._clean('session=planning launch'),
                         'session=planning launch')
        self.assertEqual(knowledge._clean('Credential: hiring process'),
                         'Credential: hiring process')

    def test_lazy_history_exception_is_sanitized(self):
        def bad_records(terms):
            yield {'id': '1', 'title': 't', 'source': 's', 'text': 'x'}
            raise RuntimeError('password=sentinelLAZY123')
        class LazyHist:
            def search(self, terms):
                return bad_records(terms)
        core = self._core(history=LazyHist())
        with self.assertRaises(RuntimeError) as ctx:
            core.search('price', project='acme')
        self.assertNotIn('sentinelLAZY123', str(ctx.exception))

    def test_round11_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('Cookie: session=abc'),
            knowledge._clean('%257B%2522password%2522%253A%2522sentinelABC123%2522%257D'),
            knowledge._clean('%22auth_token%22%3A%22abc12345%22'),
            knowledge._clean('Find %22launch plan%22 and %22token%22%3A%22abc123%22'),
        ])
        for sentinel in ('sentinelABC123', 'abc12345', 'abc123'):
            self.assertNotIn(sentinel, blob)
        self.assertIn('%22launch plan%22', blob)
        self.assertEqual(knowledge._clean('session: "2024-01-01"'), 'session: "2024-01-01"')
        self.assertEqual(knowledge._clean('Token: "economics" is a topic'),
                         'Token: "economics" is a topic')

    def test_history_provider_exception_is_sanitized(self):
        class BadHist:
            def search(self, terms):
                raise RuntimeError('password=sentinelEXCEPTION123')
        core = self._core(history=BadHist())
        with self.assertRaises(RuntimeError) as ctx:
            core.search('price', project='acme')
        self.assertNotIn('sentinelEXCEPTION123', str(ctx.exception))

    def test_round10_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean('session=12345678901234567890'),
            knowledge._clean('token=abc'),
            knowledge._clean_url('https://host/x#access_token%253DsecretSENTINEL42'),
            knowledge._clean_url('https://host/x?next=https%3A%2F%2Finner%2F%3Faccess_token%3DsecretSENTINEL42'),
        ])
        self.assertNotIn('12345678901234567890', blob)
        self.assertNotIn('secretSENTINEL42', blob)
        self.assertEqual(knowledge._clean('https://example.com/?q=%22tokenization%22'),
                         'https://example.com/?q=%22tokenization%22')
        self.assertEqual(knowledge._clean('session: 2026'), 'session: 2026')

    def test_history_provider_never_sees_credential_terms(self):
        seen = []
        class Hist:
            def search(self, terms):
                seen.extend(terms)
                return []
        core = self._core(completion=lambda messages: 'x [1].', history=Hist())
        core.search('what is the token=secretSENTINEL42 price', project='acme')
        self.assertNotIn('secretsentinel42', seen)
        seen.clear()
        core.search('session_id=42 price', project='acme')
        self.assertIn('42', seen)

    def test_round9_credential_and_benign_shapes(self):
        import knowledge
        blob = ' '.join([
            knowledge._clean("password: 'alpha bravo'"),
            knowledge._clean('{"token":"abc\\"def123"}'),
            knowledge._clean('token=alphabetical'),
            knowledge._clean_url('https://host/x#access_token%3Dalpha123456'),
            knowledge._clean_url('https://host/x?x=ok%2526access_token%253Dalpha123456'),
        ])
        for sentinel in ('alpha bravo', 'def123', 'alphabetical', 'alpha123456'):
            self.assertNotIn(sentinel, blob)
        self.assertEqual(knowledge._clean('session: 2026-11-02'), 'session: 2026-11-02')
        self.assertEqual(knowledge._clean('Token: meeting agenda'), 'Token: meeting agenda')

    def test_rank_index_cache_invalidates_on_content_change(self):
        engine = self._core().engine
        docs = [dict(id=f'd{i}', title=f'doc{i}', source=f's{i}',
                     text=('alpha beta gamma ' * 20) + f' unique{i}',
                     updated_at=None) for i in range(50)]
        first = engine._rank(docs, ['alpha', 'unique7'], 3)
        again = engine._rank(docs, ['alpha', 'unique7'], 3)
        self.assertEqual([d['source'] for d in first], [d['source'] for d in again])
        docs[7] = dict(docs[7], text='zeta omega replaced content ' * 10)
        changed = engine._rank(docs, ['zeta'], 3)
        self.assertTrue(changed and changed[0]['source'] == 's7')

    def test_semicolon_params_and_ordinary_sort_key(self):
        import knowledge
        self.assertNotIn('SENTINEL1', knowledge._clean_url(
            'https://example.test/?page=one;access_token=SENTINEL1'))
        self.assertEqual(knowledge._clean_url('https://example.com/?sort_key=price'),
                         'https://example.com/?sort_key=price')
        self.assertNotIn('SENTINEL2', knowledge._clean_url(
            'https://example.com/?api_key=SENTINEL2&ok=1'))

    def test_deeply_nested_fragment_urls_do_not_recurse(self):
        import knowledge
        nested = 'https://example.com/#a'
        for _ in range(400):
            nested = 'https://example.com/#' + nested
        self.assertIsInstance(knowledge._clean(nested), str)

    def test_round7_credential_shapes_are_scrubbed(self):
        import knowledge
        samples = [
            knowledge._clean('{"auth_token":"abc12345"}'),
            knowledge._clean('%22auth_token%22%3A%22abc12345%22'),
            knowledge._clean('Cookie: sid=x; session=abc12345; theme=dark'),
            knowledge._clean('MCP_API_KEY=abc12345'),
            knowledge._clean('user:passw0rd@example.com'),
            knowledge._clean('Auth: session 123e4567-e89b-12d3-a456-426614174000'),
            knowledge._clean_url('https://example.com/x?auth=abc12345&ok=1'),
            knowledge._clean_url('https://example.com/x?ok=1%26refresh_token=abc12345'),
        ]
        blob = ' '.join(samples)
        self.assertNotIn('abc12345', blob)
        self.assertNotIn('passw0rd', blob)
        self.assertNotIn('123e4567', blob)
        self.assertEqual(knowledge._clean_url('https://example.com/\x01x'), '[removed URL]')

    def test_transcript_urls_and_basic_auth_are_scrubbed(self):
        captured = []
        def complete(messages):
            captured.extend(messages)
            return 'Cited [1].'
        core = self._core(completion=complete)
        core.answer('Price?', transcript='see https://ex.com/x?access%5Ftoken=secret123 '
                                         'Authorization: Basic dXNlcjpwYXNz',
                    project='acme')
        blob = json.dumps(captured)
        self.assertNotIn('secret123', blob)
        self.assertNotIn('dXNlcjpwYXNz', blob)

    def test_authorization_bearer_alphabetic_token_redacted(self):
        class History:
            def search(self, terms):
                return [{'id': 'h1', 'title': 'note', 'source': 'history:test',
                         'text': 'quasar Authorization: Bearer abcdefghijklmnop',
                         'updated_at': None}]
        core = self._core(history=History())
        result = core.answer('quasar price?', transcript='t', project='acme')
        self.assertNotIn('abcdefghijklmnop', json.dumps(result, default=str))

    def test_lazy_not_implemented_errors_are_sanitized(self):
        def complete(messages):
            return 'Full [1].'
        def chunks(messages):
            yield 'Part '
            raise NotImplementedError('secret=example-secret-123')
        setattr(complete, 'stream_chunks', chunks)
        core = self._core(completion=complete)
        _, deltas = core.engine.answer_stream('Price?', transcript='t', project='acme')
        with self.assertRaises(NotImplementedError) as ctx:
            list(deltas)
        self.assertNotIn('example-secret-123', str(ctx.exception))
        self.assertIn('streaming_unsupported', str(ctx.exception))


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
        err = self._run('--docs', self.docs, '--question', 'Q?', '--project', 'acme', '--all')
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

    def test_doctor_reports_searchable_content(self):
        code, out, _ = self._capture(['--docs', self.docs, '--doctor'])
        self.assertEqual(code, 0)
        self.assertIn('acme: 1 file(s), 1 searchable chunk(s)', out)
        self.assertIn('Ready.', out)
        code, out, _ = self._capture(['--docs', self.docs, '--doctor', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['projects'][0]['chunks'], 1)

    def test_doctor_empty_docs_is_actionable_failure(self):
        empty = Path(self.docs) / 'elsewhere'
        empty.mkdir()
        code, out, _ = self._capture(['--docs', str(empty), '--doctor', '--json'])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['projects'], [])

    def test_doctor_explains_missing_project_folders_in_json_and_text(self):
        empty = Path(self.docs) / 'elsewhere'
        empty.mkdir()
        (empty / 'notes.md').write_text('Meeting notes', encoding='utf-8')
        code, out, _ = self._capture(['--docs', str(empty), '--doctor', '--json'])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload['ok'])
        self.assertIn('no project folders', ' '.join(payload['problems']).lower())
        self.assertIn('subfolder', ' '.join(payload['problems']).lower())
        code, out, _ = self._capture(['--docs', str(empty), '--doctor'])
        self.assertEqual(code, 2)
        self.assertIn('subfolder', out.lower())

    def test_doctor_rejects_mixed_modes(self):
        err = self._run('--docs', self.docs, '--doctor', '--search', 'x')
        self.assertIn('Choose one mode', str(err.code))

    def test_list_projects_excludes_ineligible_folders(self):
        (Path(self.docs) / '__pycache__').mkdir()
        (Path(self.docs) / 'node_modules').mkdir()
        code, out, _ = self._capture(['--docs', self.docs, '--list-projects', '--json'])
        self.assertEqual(code, 0)
        names = [p['name'] for p in json.loads(out)['projects']]
        self.assertEqual(names, ['acme'])

    def test_json_payloads_carry_warnings(self):
        (Path(self.docs) / 'root.md').write_text('root notes', encoding='utf-8')
        code, out, _ = self._capture(['--docs', self.docs, '--search', 'Acme',
                                      '--transcript', 'ignored.txt', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        blob = ' '.join(payload.get('warnings', []))
        self.assertIn('are not searched', blob)
        self.assertIn('--transcript is ignored', blob)

    def test_fake_outdated_doc_loses_to_current_transcript(self):
        import ask
        complete = ask.fake_completion_factory()
        payload = {'question': 'alpha beta gamma',
                   'transcript': 'alpha beta gamma are current',
                   'evidence': [{'citation': 1, 'title': 'gamma',
                                 'text': 'alpha beta are outdated'}]}
        out = complete([{'role': 'user', 'content': json.dumps(payload)}])
        self.assertIn('transcript itself says', out)
        self.assertIn('current', out)

    def test_fake_transcript_not_suppressed_by_title_only_doc(self):
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            'The unit handbook covers shipping windows.\n', encoding='utf-8')
        transcript = Path(self.docs) / 'input.txt'
        transcript.write_text('Bob: The unit price is $99.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'What is the unit price?',
                                      '--docs', self.docs, '--provider', 'fake',
                                      '--transcript', str(transcript)])
        self.assertEqual(code, 0)
        self.assertIn('transcript itself says', out)
        self.assertIn('$99', out)

    def test_fake_considers_second_document(self):
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            'The unit handbook covers shipping windows.\n', encoding='utf-8')
        (Path(self.docs) / 'acme' / 'pricing.txt').write_text(
            'The unit price is $84 each.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'What is the unit price?',
                                      '--docs', self.docs, '--provider', 'fake'])
        self.assertEqual(code, 0)
        answer = out.split('\n\nSources')[0]
        self.assertIn('$84', answer)

    def test_fake_passage_prefers_term_cluster(self):
        filler = ' '.join(['filler'] * 60)
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            f'unit mentioned once. {filler} The unit price is $84 each.\n',
            encoding='utf-8')
        code, out, _ = self._capture(['--question', 'What is the unit price?',
                                      '--docs', self.docs, '--provider', 'fake'])
        self.assertEqual(code, 0)
        self.assertIn('$84', out.split('\n\nSources')[0])

    def test_fake_rejects_title_only_match(self):
        (Path(self.docs) / 'acme' / 'unit price notes.md').write_text(
            'The unit handbook covers shipping windows and warehouses.\n',
            encoding='utf-8')
        (Path(self.docs) / 'acme' / 'notes.md').unlink()
        code, out, _ = self._capture(['--question', 'What is the unit price?',
                                      '--docs', self.docs, '--provider', 'fake'])
        self.assertEqual(code, 0)
        self.assertIn('does not directly answer', out.split('\n\nSources')[0])

    def test_fake_quotes_matching_part_of_long_transcript_line(self):
        transcript = Path(self.docs) / 'input.txt'
        filler = ' '.join(['filler'] * 60)
        transcript.write_text(f'Alice: {filler} the quasar deadline is Friday.\n',
                              encoding='utf-8')
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            'Unrelated pricing notes.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'When is the quasar deadline?',
                                      '--docs', self.docs, '--provider', 'fake',
                                      '--transcript', str(transcript)])
        self.assertEqual(code, 0)
        self.assertIn('quasar deadline is Friday', out)

    def test_fake_rejects_weak_transcript_match(self):
        transcript = Path(self.docs) / 'input.txt'
        transcript.write_text('Alice: quasar launch is delayed.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'When is the quasar deadline?',
                                      '--docs', self.docs, '--provider', 'fake',
                                      '--transcript', str(transcript)])
        self.assertEqual(code, 0)
        self.assertNotIn('transcript itself says', out)

    def test_fake_quotes_passage_around_matching_terms(self):
        intro = ' '.join(['filler'] * 80)
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            f'{intro} The unit price is $84 each for the renewal.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'What is the unit price?',
                                      '--docs', self.docs, '--provider', 'fake'])
        self.assertEqual(code, 0)
        self.assertIn('$84', out.split('\n\nSources')[0])

    def test_json_version_and_option_termination(self):
        code, out, _ = self._capture(['--json', '--version'])
        self.assertEqual(code, 0)
        self.assertIn('0.2.0', json.loads(out)['version'])
        # After --, --help is positional: no JSON help shortcut.
        import ask
        with self.assertRaises(SystemExit):
            ask.main(['--json', '--', '--help'])

    def test_fake_uses_transcript_when_no_documents_match(self):
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            'Unrelated pricing and renewal notes.\n', encoding='utf-8')
        transcript = Path(self.docs) / 'input.txt'
        transcript.write_text('Alice: The quasar deadline is November 17.\n',
                              encoding='utf-8')
        code, out, _ = self._capture(['--question', 'When is the quasar deadline?',
                                      '--docs', self.docs, '--provider', 'fake',
                                      '--transcript', str(transcript)])
        self.assertEqual(code, 0)
        self.assertIn('transcript itself says', out)
        self.assertIn('November 17', out)

    def test_fake_answers_one_term_question(self):
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            'Quoted unit price is $84 each.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'Price?',
                                      '--docs', self.docs, '--provider', 'fake'])
        self.assertEqual(code, 0)
        answer = out.split('\n\nSources')[0]
        self.assertIn('$84', answer)
        self.assertNotIn('does not directly answer', answer)

    def test_json_help_is_json(self):
        code, out, _ = self._capture(['--json', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('Standalone grounded', json.loads(out)['description'])

    def test_fake_prefers_transcript_over_unrelated_document(self):
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            'The launch plan requires renewal and budget approval.\n', encoding='utf-8')
        transcript = Path(self.docs) / 'input.txt'
        transcript.write_text('Alice: the launch date is November 17.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'When is the launch date?',
                                      '--docs', self.docs, '--provider', 'fake',
                                      '--transcript', str(transcript)])
        self.assertEqual(code, 0)
        answer = out.split('\n\nSources')[0]
        self.assertIn('transcript itself says', answer)
        self.assertIn('November 17', answer)

    def test_fake_does_not_quote_unrelated_document_as_answer(self):
        (Path(self.docs) / 'acme' / 'notes.md').write_text(
            'The launch plan requires renewal and budget approval.\n', encoding='utf-8')
        code, out, _ = self._capture(['--question', 'When is the launch date?',
                                      '--docs', self.docs, '--provider', 'fake'])
        self.assertEqual(code, 0)
        answer = out.split('\n\nSources')[0]
        self.assertIn('does not directly answer', answer)
        self.assertNotIn('requires renewal and budget approval', answer)

    def test_root_documents_warn_they_are_not_searched(self):
        (Path(self.docs) / 'root.md').write_text('root price notes', encoding='utf-8')
        code, _, err = self._capture(['--docs', self.docs, '--search', 'Acme renews'])
        self.assertEqual(code, 0)
        self.assertIn('are not searched', err)
        code, _, err = self._capture(['--docs', self.docs, '--list-projects'])
        self.assertEqual(code, 0)
        self.assertIn('are not searched', err)

    def test_default_fake_quotes_retrieved_evidence(self):
        code, out, _ = self._capture(['--question', 'When does Acme renew?',
                                      '--docs', self.docs, '--provider', 'fake'])
        self.assertEqual(code, 0)
        self.assertIn('Acme renews in September.', out)
        self.assertIn('From notes.md:', out)
        self.assertIn('no model was called', out)

    def test_list_nonblank_question_is_rejected(self):
        err = self._run('--docs', self.docs, '--list-projects', '--question', 'ignored')
        self.assertIn('Choose one mode', str(err.code))

    def test_docs_pointed_at_project_folder_explains_layout(self):
        flat = Path(self.tmp) / 'flat'
        flat.mkdir()
        (flat / 'notes.md').write_text('price notes', encoding='utf-8')
        err = self._run('--docs', str(flat), '--all', '--search', 'price')
        self.assertIn('No project folders', str(err.code))
        self.assertIn('move them into a subfolder', str(err.code))

    def test_search_blank_question_warns_not_rejects(self):
        code, _, err = self._capture(['--docs', self.docs, '--search', 'Acme renews',
                                      '--question', ' '])
        self.assertEqual(code, 0)
        self.assertIn('--question is ignored', err)

    def test_list_project_all_warns_and_lists(self):
        code, out, err = self._capture(['--docs', self.docs, '--list-projects',
                                        '--project', 'acme', '--all'])
        self.assertEqual(code, 0)
        self.assertIn('acme', out)
        self.assertIn('--all is ignored', err)

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

    def test_list_project_all_warns_and_returns_json(self):
        import ask
        import io
        import json as _json
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ask.main(['--docs', self.docs, '--list-projects', '--project',
                             'acme', '--all', '--json'])
        self.assertEqual(code, 0)
        self.assertIn('ignored', err.getvalue())
        self.assertEqual(_json.loads(out.getvalue())['projects'][0]['name'], 'acme')


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
