"""Per-request answer routing, with Hermes boundaries faked (no credentials/network)."""
import importlib
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class ModelSelectionTests(unittest.TestCase):
    def module(self):
        try:
            mod = importlib.import_module('model_selection')
        except ModuleNotFoundError:
            self.fail('model_selection must provide the safe Hermes model catalog')
        try:
            mod.clear_catalog_cache()
        except Exception:
            pass
        return mod

    def test_catalog_lists_fixed_routes_without_oauth_or_secret_values(self):
        with patch('byok.secret',return_value='synthetic-secret'), patch.dict(sys.modules, {'hermes_cli.models':None,'agent.auxiliary_client':None}):
            result=self.module().available_models()
        self.assertEqual({p['id'] for p in result['providers']},{'hermes','openai','openrouter'})
        self.assertTrue(all(p['custom_model'] and p['configured'] for p in result['providers'] if p['id'] != 'hermes'))
        self.assertNotIn('synthetic-secret',json.dumps(result))

    def test_validation_accepts_exact_user_id_without_catalog_or_aliases(self):
        module=self.module()
        with patch.object(module,'available_models',side_effect=AssertionError('model discovery forbidden')):
            self.assertEqual(module.validate_selection('openrouter','vendor/exact:model-1'),('openrouter','vendor/exact:model-1'))
            for pair in [(None,None),('auto','model'),([], 'model'),({},'model'),('openai','auto'),('openai','https://host'),('openai','sk-private'),('openai','x'*201),('openai',None)]:
                with self.subTest(pair=pair),self.assertRaises(ValueError):module.validate_selection(*pair)

    def test_missing_scoped_keys_are_reported_unconfigured_without_secret_details(self):
        with patch('byok.secret',return_value=''), patch('byok._scope_available',return_value=True):
            result=self.module().available_models()
        self.assertTrue(all(not p['configured'] for p in result['providers']))
        self.assertTrue(all('Hermes Settings' in p['status'] for p in result['providers'] if p['id'] != 'hermes'))

    def test_missing_credentials_fail_before_transport(self):
        with patch('byok.secret',return_value=''),patch('urllib.request.build_opener',side_effect=AssertionError('no transport')):
            with self.assertRaises(ValueError):self.module().selected_completion('openai','exact-model')

    def test_explicit_completion_delegates_only_to_bounded_byok_route(self):
        route=Mock(return_value='synthetic answer')
        with patch('byok.completion',return_value=route) as build,patch.dict(sys.modules,{'agent.auxiliary_client':None}):
            complete=self.module().selected_completion('openai','exact-model')
            self.assertEqual(complete([{'role':'user','content':'synthetic'}]),'synthetic answer')
        build.assert_called_once_with('openai','exact-model')
        self.assertEqual(route.call_count,1)


class KnowledgeSelectionTests(unittest.TestCase):
    def engine(self):
        from knowledge import KnowledgeEngine
        engine = KnowledgeEngine()
        engine.search = Mock(return_value=[])
        engine.projects = Mock(return_value=[])
        engine._complete = Mock(return_value='default answer')
        engine._completion_override = engine._complete
        return engine

    def test_default_keeps_one_argument_injected_completion_for_planner_and_answer(self):
        engine = self.engine()
        def plan(question, transcript, sources, projects, complete):
            self.assertIs(complete, engine._complete)
            complete([{'role': 'user', 'content': 'planner'}])
            return {'needed': False, 'reason': 'not_needed'}
        with patch('knowledge.plan_web', side_effect=plan), patch('model_selection.selected_completion') as selected:
            result = engine.answer('public', web_fallback=True, answer_provider=None, answer_model=None)
        self.assertEqual(result['answer'], 'default answer')
        self.assertEqual(engine._complete.call_count, 2)
        selected.assert_not_called()

    _aux = None
    _calls = None

    def test_concurrent_selected_and_injected_routes_never_share_state(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        engine=self.engine()
        original=engine._complete
        barrier=threading.Barrier(3)
        def selected(provider,model):
            return lambda messages:f'{provider}/{model}'
        def planner(question,transcript,sources,projects,complete):
            barrier.wait(timeout=2)
            return {'needed':False,'reason':'not_needed'}
        with patch('model_selection.selected_completion',side_effect=selected),patch('knowledge.plan_web',side_effect=planner):
            with ThreadPoolExecutor(max_workers=3) as pool:
                a=pool.submit(engine.answer,'a',web_fallback=True,answer_provider='openai',answer_model='exact-a')
                b=pool.submit(engine.answer,'b',web_fallback=True,answer_provider='openrouter',answer_model='vendor/exact-b')
                c=pool.submit(engine.answer,'c',web_fallback=True)
                answers=[f.result(timeout=5)['answer'] for f in (a,b,c)]
        self.assertEqual(answers,['openai/exact-a','openrouter/vendor/exact-b','default answer'])
        self.assertIs(engine._complete,original)

    def test_invalid_selection_is_rejected_before_retrieval_and_failure_never_uses_default(self):
        engine = self.engine()
        with patch('model_selection.available_models', return_value={'providers': []}):
            with self.assertRaises(ValueError):
                engine.answer('public', answer_provider='openai-codex', answer_model='unknown')
        engine.search.assert_not_called()
        with patch('model_selection.selected_completion', return_value=Mock(side_effect=RuntimeError('sanitized'))):
            result = engine.answer('public', answer_provider='openai-codex', answer_model='gpt-public')
        self.assertIn('unavailable', result['answer'])
        self.assertTrue(any(w == 'Model request failed.' for w in result['warnings']))
        engine._complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class StreamingSelectionTests(ModelSelectionTests):
    def _catalog(self, module):
        real = module.available_models
        module.available_models = lambda: {'providers': [
            {'id': 'provider-one', 'label': 'One', 'models': [{'id': 'model-one', 'label': 'One'}]}]}
        # Permanent assignment would leak the fake catalog into other test
        # modules sharing this module object (order-dependent failures).
        self.addCleanup(setattr, module, 'available_models', real)

    def _hermes(self, frames=None, completed=None, reject_stream=False):
        import sys
        from types import SimpleNamespace
        calls = []
        frames = frames or []
        completed = completed or {'text': 'X'}

        def resolver(provider, model):
            def create(**kwargs):
                calls.append(dict(kwargs))
                if kwargs.get('stream') and not reject_stream:
                    return list(frames)
                if kwargs.get('stream') and reject_stream:
                    raise TypeError('stream unsupported')
                text = completed['text']
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), model
        def builder(provider, model, messages, **kwargs):
            return {'model': model, 'messages': messages}
        auxiliary = SimpleNamespace(resolve_provider_client=resolver, _build_call_kwargs=builder,
                                    call_llm=Mock(side_effect=AssertionError('no default routing')))
        self._aux, self._calls = auxiliary, calls
        return auxiliary

    def test_sse_frames_yield_distinct_ordered_deltas(self):
        # BYOK deliberately exposes completed-only; it cannot probe or retry a stream.
        with patch('byok.secret',return_value='synthetic-key'), patch('urllib.request.build_opener',side_effect=AssertionError('construction must not submit')):
            complete=self.module().selected_completion('openai','exact-model')
        self.assertFalse(hasattr(complete,'stream_chunks'))

    def test_completed_only_response_reports_honestly(self):
        # BYOK deliberately exposes completed-only; it cannot probe or retry a stream.
        with patch('byok.secret',return_value='synthetic-key'), patch('urllib.request.build_opener',side_effect=AssertionError('construction must not submit')):
            complete=self.module().selected_completion('openai','exact-model')
        self.assertFalse(hasattr(complete,'stream_chunks'))

    def test_stream_rejecting_signature_reports_honestly(self):
        # BYOK deliberately exposes completed-only; it cannot probe or retry a stream.
        with patch('byok.secret',return_value='synthetic-key'), patch('urllib.request.build_opener',side_effect=AssertionError('construction must not submit')):
            complete=self.module().selected_completion('openai','exact-model')
        self.assertFalse(hasattr(complete,'stream_chunks'))

    def test_extract_rejects_malformed_and_chunked_completed_strings(self):
        from types import SimpleNamespace
        with self.assertRaises(RuntimeError):
            self.module().extract_openai_sse_deltas('data: not-json\n\n')
        completed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Final'))])
        self.assertIsNone(self.module().extract_openai_sse_deltas(completed))

    def test_extract_handles_sdk_object_chunks_and_malformed_shapes(self):
        from types import SimpleNamespace
        module = self.module()

        def delta_chunk(text=None, role=None, content=None, finish=None):
            delta_kwargs = {}
            if text is not None:
                delta_kwargs['content'] = text
            if role is not None:
                delta_kwargs['role'] = role
            delta = SimpleNamespace(**delta_kwargs) if delta_kwargs else SimpleNamespace(content=None)
            choice_kwargs = {'delta': delta}
            if finish is not None:
                choice_kwargs['finish_reason'] = finish
            return SimpleNamespace(choices=[SimpleNamespace(**choice_kwargs)])

        def completed_chunk(text):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

        stop = delta_chunk(finish='stop')
        list_chunks = [delta_chunk(text='Two '), delta_chunk(text='reviewers.'), stop]
        self.assertEqual(module.extract_openai_sse_deltas(list_chunks), ['Two ', 'reviewers.'])
        # One-shot iterables must survive a single pass: no re-read, no list()
        # re-consumption, and order preserved (representative of SDK iterators).
        one_shot = iter([delta_chunk(text='Two '), delta_chunk(text='reviewers.'), stop])
        self.assertEqual(module.extract_openai_sse_deltas(one_shot), ['Two ', 'reviewers.'])
        # Dict-shaped delta chunks (decoded JSON objects) take the wire path.
        wire_dicts = iter([{'choices': [{'delta': {'content': 'Hi'}}]},
                           {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])
        self.assertEqual(module.extract_openai_sse_deltas(wire_dicts), ['Hi'])
        # Mixed SDK shapes: object delta, role-only delta (no text), and an
        # empty-delta chunk; only real text survives, in order.
        mixed = iter([delta_chunk(text='A'), delta_chunk(role='assistant'),
                      SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace())]),
                      delta_chunk(text='B'), stop])
        self.assertEqual(module.extract_openai_sse_deltas(mixed), ['A', 'B'])
        # Completed-only objects and malformed shapes carry no incremental text.
        self.assertIsNone(module.extract_openai_sse_deltas(completed_chunk('Final answer.')))
        self.assertIsNone(module.extract_openai_sse_deltas(iter([])))
        self.assertIsNone(module.extract_openai_sse_deltas([object(), object()]))
        # Incremental delivery: a timing generator yields deltas as they arrive
        # (no buffering the full stream before the first delta is returned).
        import time as _time
        arrivals = []

        def timing_stream():
            arrivals.append(('first', _time.monotonic()))
            yield delta_chunk(text='one ')
            _time.sleep(0.05)
            arrivals.append(('second', _time.monotonic()))
            yield delta_chunk(text='two')
            yield delta_chunk(finish='stop')

        received = module.extract_openai_sse_deltas(timing_stream())
        self.assertEqual(received, ['one ', 'two'])
        self.assertEqual([name for name, _ in arrivals], ['first', 'second'])

    def test_lazy_sdk_chunks_arrive_before_provider_finishes(self):
        # Barrier test (TDD): an SDK stream that blocks after its first
        # token must deliver that token while still blocked. An eager
        # list(iterator) upstream would time this test out.
        import sys
        import threading
        import queue as _queue
        from types import SimpleNamespace
        module = self.module()
        self._catalog(module)
        calls = []

        def delta_chunk(text):
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

        entered = threading.Event()
        release = threading.Event()

        def finish_chunk():
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(), finish_reason='stop')])
        def gated_stream():
            calls.append({'synthetic_stream':True})
            entered.set()
            yield delta_chunk('first ')
            release.wait(5)  # stay unfinished until the consumer proves arrival
            yield delta_chunk('second')
            yield finish_chunk()

        def resolver(provider, model):
            def create(**kwargs):
                calls.append(dict(kwargs))
                self.assertTrue(kwargs.get('stream'))
                return gated_stream()
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), model
        def builder(provider, model, messages, **kwargs):
            return {'model': model, 'messages': messages}
        auxiliary = SimpleNamespace(resolve_provider_client=resolver, _build_call_kwargs=builder,
                                    call_llm=Mock(side_effect=AssertionError('no default routing')))
        received = _queue.Queue()
        errors = _queue.Queue()
        with patch.dict(sys.modules, {'agent.auxiliary_client': auxiliary}):
            stream = module.iter_openai_sse_deltas(gated_stream())
            first_holder = {}
            def consume():
                try:
                    first_holder['first'] = next(stream)
                    received.put('arrived')
                except Exception as exc:  # pragma: no cover - failure path
                    errors.put(exc)
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            # The first token must arrive while the provider is still gated.
            arrived = received.get(timeout=5)
            self.assertEqual(arrived, 'arrived')
            self.assertEqual(first_holder.get('first'), 'first ')
            self.assertFalse(release.is_set(), 'provider must still be gated')
            release.set()
            rest = list(stream)
            worker.join(timeout=5)
            self.assertTrue(errors.empty(), f'consumer failed: {errors.get() if not errors.empty() else None}')
        self.assertEqual(rest, ['second'])
        self.assertEqual(len(calls), 1)  # exactly one paid streaming call

    def test_lazy_sse_chunks_reassemble_splits_and_report_errors(self):
        # Barrier test (TDD): raw SSE pieces split mid-frame must deliver
        # the reassembled delta while the provider is still gated; errors
        # surface as RuntimeError; one-shot iterators are consumed once.
        import queue as _queue
        import threading
        from types import SimpleNamespace
        module = self.module()
        self._catalog(module)
        release = threading.Event()

        def split_stream():
            # First frame is complete (deliverable now); the provider then
            # blocks before the split second frame, proving the first delta
            # arrives while the stream is still gated.
            yield 'data: {"choices":[{"delta":{"content":"One "}}]}\n\n'
            release.wait(10)
            yield 'data: {"choices":[{"delta":{"content":"Two'
            yield ' reviewers."}}]}\n\n'
            yield 'data: [DONE]\n\n'

        seen = {'polls': 0}

        class LineSource:
            def iter_lines(self):
                seen['polls'] += 1
                yield 'data: {"choices":[{"delta":{"content":"A"}}]}\n'
                yield 'data: [DONE]\n'

        one_shot = split_stream()
        received = _queue.Queue()
        failed = _queue.Queue()
        def consume():
            try:
                received.put(next(module.iter_openai_sse_deltas(one_shot)))
            except Exception as exc:  # pragma: no cover - failure path
                failed.put(exc)
        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        self.assertEqual(received.get(timeout=10), 'One ')
        self.assertFalse(release.is_set(), 'provider must still be gated')
        release.set()
        worker.join(timeout=10)
        self.assertTrue(failed.empty(), f'consumer failed: {failed.get() if not failed.empty() else None}')
        self.assertFalse(worker.is_alive(), 'consumer must finish once released')
        self.assertEqual(list(module.iter_openai_sse_deltas(one_shot)), ['Two reviewers.'])
        # Line source reassembles and stops at DONE.
        out = list(module.iter_openai_sse_deltas(LineSource()))
        self.assertEqual(out, ['A'])
        self.assertEqual(seen['polls'], 1)
        # Malformed SSE JSON raises (sanitized), failures never synthesize.
        with self.assertRaises(RuntimeError):
            list(module.iter_openai_sse_deltas(iter(['data: not-json\n\n'])))
        # One-shot iterator: single pass, never re-read.
        consumed = iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='x'))]),
                           SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(), finish_reason='stop')])])
        self.assertEqual(list(module.iter_openai_sse_deltas(consumed)), ['x'])
        self.assertEqual(list(consumed), [])
        self.assertIsNone(module.extract_openai_sse_deltas(iter([])))

    def test_wire_feed_split_utf8_reassembles_incrementally(self):
        # Bug-report repro shape: a multi-byte char split between chunks must
        # decode intact via the incremental decoder, not replacement chars,
        # and the first delta must arrive before the second chunk is fed.
        import queue as _queue
        import threading
        module = self.module()
        raw = ('data: {"choices":[{"delta":{"content":"✓"}}]}\n\ndata: [DONE]\n\n').encode('utf-8')
        boundary = raw.index(b'\xe2') + 1
        self.assertEqual(list(module.iter_openai_sse_deltas([raw[:boundary], raw[boundary:]])), ['✓'])
        # Every split position across the multi-byte sequence reassembles.
        for split in range(1, len(raw)):
            with self.subTest(split=split):
                self.assertEqual(list(module.iter_openai_sse_deltas([raw[:split], raw[split:]])), ['✓'])
        release = threading.Event()
        received = _queue.Queue()
        failed = _queue.Queue()
        frame = 'data: {"choices":[{"delta":{"content":"One "}}]}\n\n'
        # First chunk holds a complete frame (deliverable now) plus a partial
        # second frame whose bytes are still gated; the reassembled second
        # delta must follow only after release.
        first = (frame + 'data: {"choices":[{"delta":{"content":"Tw').encode('utf-8')
        second = b'o reviewers."}}]}\n\ndata: [DONE]\n\n'
        def slow_pieces():
            yield first
            release.wait(10)
            yield second
        gen = module.iter_openai_sse_deltas(slow_pieces())
        def consume():
            try:
                received.put(next(gen))
            except Exception as exc:  # pragma: no cover - failure path
                failed.put(exc)
        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        self.assertEqual(received.get(timeout=10), 'One ')
        self.assertFalse(release.is_set(), 'first delta must arrive while gated')
        release.set()
        worker.join(timeout=10)
        self.assertTrue(failed.empty())
        self.assertEqual(list(gen), ['Two reviewers.'])

    def test_wire_feed_provider_error_is_sanitized_failure(self):
        # data: {"error": {...}} must raise a sanitized failure that never
        # exposes the provider message/keys and never reports completed-only.
        module = self.module()
        error_only = iter(['data: {"error":{"message":"quota super-secret-key"}}\n\ndata: [DONE]\n\n'])
        with self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
            list(module.iter_openai_sse_deltas(error_only))
        error_after_partial = iter([
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            'data: {"error":{"message":"quota super-secret-key"}}\n\ndata: [DONE]\n\n'])
        try:
            list(module.iter_openai_sse_deltas(error_after_partial))
        except RuntimeError as exc:
            self.assertEqual(str(exc), 'Selected answer model failed')
            self.assertNotIn('quota', str(exc))
            self.assertNotIn('secret', str(exc))
        else:
            self.fail('provider error after partial delta must raise')
        # Same honest failure through the eager collector.
        with self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
            module.extract_openai_sse_deltas(
                iter(['data: {"error":{"message":"quota"}}\n\ndata: [DONE]\n\n']))

    def test_wire_feed_bounds_partial_line_but_allows_many_valid_frames(self):
        # Many small valid frames in one chunk must all parse (only the
        # trailing partial line and each individual complete line are
        # bounded); one oversized line must raise without unbounded buffers.
        module = self.module()
        frame = 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        many = [frame * 3000 + 'data: [DONE]\n\n']
        out = list(module.iter_openai_sse_deltas(iter(many)))
        self.assertEqual(len(out), 3000)
        self.assertTrue(all(delta == 'x' for delta in out))
        oversized = 'data: {"choices":[{"delta":{"content":"' + 'y' * 70000 + '"}}]}\n\n'
        with self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
            list(module.iter_openai_sse_deltas(iter([oversized])))
        oversized_partial = 'data: {"choices":[{"delta":{"content":"' + 'z' * 70000
        with self.assertRaisesRegex(RuntimeError, '^Selected answer model failed$'):
            list(module.iter_openai_sse_deltas(iter([oversized_partial])))

    def test_iter_lines_parses_stripped_lines_without_reassembly(self):
        # requests/httpx iter_lines strips newlines, so each item already
        # IS one complete line: no buffering across items. Covers the exact
        # bug-report repro, blank separators, bytes + UTF-8, [DONE], a real
        # barrier proving the first yield arrives before the provider
        # releases its second line, malformed/error paths, and generator
        # close releasing the provider stream. Raw split-frame reassembly
        # stays on the separate raw-chunk path (see the split_stream test).
        import queue as _queue
        import threading
        module = self.module()

        # Exact bug-report repro shape.
        class Repro:
            def iter_lines(self):
                return iter([b'data: {"choices":[{"delta":{"content":"hello"}}]}',
                             b'', b'data: [DONE]', b''])
        self.assertEqual(list(module.iter_openai_sse_deltas(Repro())), ['hello'])

        release = threading.Event()
        closed = []

        def lines():
            try:
                yield b''
                yield b'data: {"choices":[{"delta":{"content":"h\xc3\xa9llo "}}]}'
                yield b''
                release.wait(10)
                yield 'data: {"choices":[{"delta":{"content":"w\u00f6rld \u2713"}}]}'.encode('utf-8')
                yield b''
                yield b'data: [DONE]'
                yield b'unreached-after-done'
            finally:
                closed.append(True)

        class LineResponse:
            def iter_lines(self):
                return lines()

        gen = module.iter_openai_sse_deltas(LineResponse())
        received = _queue.Queue()
        failed = _queue.Queue()

        def consume():
            try:
                received.put(next(gen))
            except Exception as exc:  # pragma: no cover - failure path
                failed.put(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        self.assertEqual(received.get(timeout=10), 'h\u00e9llo ')
        self.assertFalse(release.is_set(), 'first delta must arrive while provider is gated')
        release.set()
        worker.join(timeout=10)
        self.assertTrue(failed.empty(), f'consumer failed: {failed.get() if not failed.empty() else None}')
        self.assertFalse(worker.is_alive(), 'consumer must finish once released')
        self.assertEqual(list(gen), ['w\u00f6rld \u2713'])
        self.assertEqual(closed, [True])

        # Early consumer close releases the provider stream.
        release2 = threading.Event()
        closed2 = []

        def lines2():
            try:
                yield b'data: {"choices":[{"delta":{"content":"early"}}]}'
                release2.wait(10)
                yield b'data: [DONE]'
            finally:
                closed2.append(True)

        class EarlyClose:
            def iter_lines(self):
                return lines2()

        gen2 = module.iter_openai_sse_deltas(EarlyClose())
        self.assertEqual(next(gen2), 'early')
        gen2.close()
        release2.set()
        self.assertEqual(closed2, [True])

        # Malformed SSE JSON and a failing iter_lines() both surface as
        # sanitized RuntimeError, never synthesized chunks.
        class BadJson:
            def iter_lines(self):
                yield b'data: not-json'
        with self.assertRaises(RuntimeError):
            list(module.iter_openai_sse_deltas(BadJson()))

        class BrokenSource:
            def iter_lines(self):
                raise OSError('transport down')
        with self.assertRaises(RuntimeError):
            list(module.iter_openai_sse_deltas(BrokenSource()))

    def test_sdk_object_stream_yields_incremental_deltas_without_double_paid_call(self):
        # BYOK deliberately exposes completed-only; it cannot probe or retry a stream.
        with patch('byok.secret',return_value='synthetic-key'), patch('urllib.request.build_opener',side_effect=AssertionError('construction must not submit')):
            complete=self.module().selected_completion('openai','exact-model')
        self.assertFalse(hasattr(complete,'stream_chunks'))


class CatalogSingleFlightTests(unittest.TestCase):
    def test_readiness_is_request_local_instead_of_singleflight_cached(self):
        from concurrent.futures import ThreadPoolExecutor
        import contextvars
        flag=contextvars.ContextVar('synthetic_readiness',default='')
        def catalog():
            import model_selection
            return model_selection.available_models()
        contexts=[]
        for value in ('synthetic-key',''):
            token=flag.set(value);contexts.append(contextvars.copy_context());flag.reset(token)
        with patch('byok.secret',side_effect=lambda name:flag.get()),ThreadPoolExecutor(max_workers=2) as pool:
            rows=[f.result(timeout=2) for f in [pool.submit(c.run,catalog) for c in contexts]]
        self.assertTrue(all(p['configured'] for p in rows[0]['providers'] if p['id'] != 'hermes'))
        self.assertTrue(all(not p['configured'] for p in rows[1]['providers']))


class TruncatedStreamTests(unittest.TestCase):
    def _module(self):
        import sys
        sys.path.insert(0, '.')
        import model_selection as module
        return module

    def test_eof_without_done_is_truncation_not_success(self):
        from types import SimpleNamespace
        module = self._module()
        frames = iter(['data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'])
        gen = module.iter_openai_sse_deltas(frames)
        seen = []
        with self.assertRaisesRegex(NotImplementedError, 'truncated'):
            for chunk in gen:
                seen.append(chunk)
        self.assertEqual(seen, ['partial'])

    def test_sdk_stream_without_finish_reason_is_truncation(self):
        from types import SimpleNamespace
        module = self._module()
        chunks = iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='incomplete'))])])
        gen = module.iter_openai_sse_deltas(chunks)
        seen = []
        with self.assertRaisesRegex(NotImplementedError, 'truncated'):
            for chunk in gen:
                seen.append(chunk)
        self.assertEqual(seen, ['incomplete'])


class LengthLimitTests(unittest.TestCase):
    def _module(self):
        import sys
        sys.path.insert(0, '.')
        import model_selection as module
        return module

    def test_length_finish_is_truncation_not_success_sdk(self):
        from types import SimpleNamespace
        module = self._module()
        chunks = iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='cut off'), finish_reason='length')])])
        gen = module.iter_openai_sse_deltas(chunks)
        seen = []
        with self.assertRaisesRegex(NotImplementedError, 'truncated'):
            for chunk in gen:
                seen.append(chunk)
        self.assertEqual(seen, ['cut off'])

    def test_length_finish_is_truncation_not_success_wire(self):
        module = self._module()
        frames = iter(['data: {"choices":[{"delta":{"content":"cut off"},"finish_reason":"length"}]}\n\n'])
        gen = module.iter_openai_sse_deltas(frames)
        seen = []
        with self.assertRaisesRegex(NotImplementedError, 'length-limit'):
            for chunk in gen:
                seen.append(chunk)
        self.assertEqual(seen, ['cut off'])

    def test_stop_finish_without_done_terminates_cleanly(self):
        module = self._module()
        frames = iter(['data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'])
        self.assertEqual(list(module.iter_openai_sse_deltas(frames)), ['answer'])


class TerminalEdgeTests(unittest.TestCase):
    def _module(self):
        import sys
        sys.path.insert(0, '.')
        import model_selection as module
        return module

    def test_post_stop_iterator_failure_does_not_fail_answer(self):
        from types import SimpleNamespace
        module = self._module()
        def bad_tail():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='done'), finish_reason='stop')])
            raise RuntimeError('stalled tail')
        self.assertEqual(list(module.iter_openai_sse_deltas(bad_tail())), ['done'])

    def test_unterminated_final_stop_frame_completes(self):
        module = self._module()
        frames = iter(['data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}'])
        self.assertEqual(list(module.iter_openai_sse_deltas(frames)), ['answer'])

    def test_unterminated_final_done_completes(self):
        module = self._module()
        self.assertEqual(list(module.iter_openai_sse_deltas(iter(['data: [DONE]']))), [])

    def test_unterminated_final_length_frame_is_truncation(self):
        module = self._module()
        frames = iter(['data: {"choices":[{"delta":{"content":"cut"},"finish_reason":"length"}]}'])
        gen = module.iter_openai_sse_deltas(frames)
        seen = []
        with self.assertRaisesRegex(NotImplementedError, 'length-limit'):
            for chunk in gen:
                seen.append(chunk)
        self.assertEqual(seen, ['cut'])
