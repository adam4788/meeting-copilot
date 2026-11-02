"""Safe Hermes model catalog and request-local routing; no credential/config writes."""
import re


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,199}\Z')
_SECRET = re.compile(r'(?i)(?:sk-|ghp_|github_pat_|bearer|api[_-]?key|password|access[_-]?token|refresh[_-]?token|eyJ[A-Za-z0-9_-]{10,})')
# These resolver aliases may auto-detect/fall through to a different backend.
_VIRTUAL = {'auto', 'custom', 'moa', 'lmstudio'}

_CATALOG_CACHE = {'at': 0.0, 'value': None}
_CATALOG_TTL_S = 30.0

try:
    import threading as _threading
    _CATALOG_LOCK = _threading.Lock()
except Exception:  # pragma: no cover - lock is best effort
    _CATALOG_LOCK = None


def clear_catalog_cache():
    """Test/support hook: drop the short-lived provider catalog cache."""
    try:
        if _CATALOG_LOCK is not None:
            with _CATALOG_LOCK:
                _CATALOG_CACHE['value'] = None
                _CATALOG_CACHE['at'] = 0.0
        else:
            _CATALOG_CACHE['value'] = None
            _CATALOG_CACHE['at'] = 0.0
    except Exception:
        pass


def _safe_id(value):
    return (isinstance(value, str) and bool(_ID.fullmatch(value))
            and '://' not in value and not _SECRET.search(value))


def available_models():
    """JSON-only authenticated, concrete Hermes providers and authoritative catalogs.

    Generic custom/auto/MoA routes are omitted: they can auto-select another backend,
    or represent an endpoint without established authentication. Model availability
    remains subject to the provider/account at invocation time.
    """
    import time as _time
    try:
        if _CATALOG_CACHE['value'] is not None and (_time.monotonic() - _CATALOG_CACHE['at']) < _CATALOG_TTL_S:
            return _CATALOG_CACHE['value']
    except Exception:
        pass
    # Single-flight the slow discovery: concurrent misses share one result
    # instead of repeating provider catalog lookups.
    if _CATALOG_LOCK is not None:
        with _CATALOG_LOCK:
            try:
                if _CATALOG_CACHE['value'] is not None and (_time.monotonic() - _CATALOG_CACHE['at']) < _CATALOG_TTL_S:
                    return _CATALOG_CACHE['value']
            except Exception:
                pass
            return _discover_models()
    return _discover_models()


def _discover_models():
    """Run provider catalog discovery and cache the result."""
    import time as _time2
    try:
        from hermes_cli.models import list_available_providers, cached_provider_model_ids
        rows = list_available_providers()
    except Exception:
        return {'providers': []}
    providers = []
    seen = set()
    for row in rows:
        provider = row.get('id')
        if not row.get('authenticated') or not _safe_id(provider) or provider in _VIRTUAL or provider in seen:
            continue
        seen.add(provider)
        try:
            catalog = cached_provider_model_ids(provider)
        except Exception:
            continue
        models = []
        for model in dict.fromkeys(catalog):
            if _safe_id(model) and model.lower() != 'auto':
                models.append({'id': model, 'label': model})
        if models:
            label = row.get('label')
            label = label if isinstance(label, str) and len(label) <= 100 and not _SECRET.search(label) and '://' not in label else provider
            providers.append({'id': provider, 'label': label, 'models': models})
    try:
        from .meta_models import META_MODELS, META_PROVIDER_ID, api_key_present
    except ImportError:  # top-level use (tests, scripts)
        try:
            from meta_models import META_MODELS, META_PROVIDER_ID, api_key_present
        except ImportError:
            META_MODELS, META_PROVIDER_ID, api_key_present = (), 'meta', lambda: False
    if api_key_present():
        providers.append({'id': META_PROVIDER_ID, 'label': 'Meta Model API',
                          'models': [{'id': m, 'label': m} for m in META_MODELS]})
    result = {'providers': providers}
    try:
        _CATALOG_CACHE['value'] = result
        _CATALOG_CACHE['at'] = _time2.monotonic()
    except Exception:
        pass
    return result


def validate_selection(provider, model):
    """Return an exact allowed pair; None/None preserves the legacy default.

    Invalid/incomplete input and unavailable pairs raise sanitized ValueError.
    No aliases, endpoint overrides, credentials, or implicit provider inference.
    """
    if provider is None and model is None:
        return None, None
    if provider == 'meta':
        try:
            from .meta_models import validate_meta_model
        except ImportError:  # top-level use (tests, scripts)
            from meta_models import validate_meta_model
        return 'meta', validate_meta_model(model)
    if not _safe_id(provider) or not _safe_id(model) or provider in _VIRTUAL or model.lower() == 'auto':
        raise ValueError('Invalid answer model selection')
    catalog = available_models()
    if any(row['id'] == provider and any(item['id'] == model for item in row['models'])
           for row in catalog['providers']):
        return provider, model
    raise ValueError('Answer model selection is unavailable')


def selected_completion(provider, model):
    """Build a request-local completion function, never using Hermes fallback routing.

    call_llm has a recovery ladder (including first-resolution fallback), so explicit
    selections instead use its authoritative resolver and provider wire builder.
    Credentials/endpoints are resolved only by Hermes, never accepted from a request.

    Streaming: `stream_chunks(messages)` yields provider-native incremental text
    deltas where supported (SSE `stream=True` on OpenAI-compatible chat routes,
    SSE content deltas on the Meta Model API). Completed-only providers raise
    NotImplementedError with `streaming_unsupported` so callers can report
    completed-only honestly instead of faking streaming.
    """
    provider, model = validate_selection(provider, model)
    if provider == 'meta':
        try:
            from .meta_models import meta_completion
        except ImportError:  # top-level use (tests, scripts)
            from meta_models import meta_completion
        return meta_completion(model)
    if provider is None or model is None:
        raise ValueError('Invalid answer model selection')

    def complete(messages):
        try:
            from agent.auxiliary_client import resolve_provider_client, _build_call_kwargs
            client, resolved_model = resolve_provider_client(provider=provider, model=model)
            if client is None or resolved_model != model:
                raise RuntimeError('Unavailable exact route')
            kwargs = _build_call_kwargs(
                provider, model, messages, max_tokens=600, tools=None, timeout=30,
                reasoning_config={'effort': 'low'},
                base_url=str(getattr(client, 'base_url', '') or ''),
            )
            response = client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError('No text answer returned')
            return text.strip()
        except Exception:
            raise RuntimeError('Selected answer model failed') from None

    def stream_chunks(messages):
        """Yield provider-native incremental deltas as they arrive.

        Responses are provider OpenAI-compatible SSE frames (`stream=True`)
        parsed lazily: the first delta is yielded before the provider
        finishes, in arrival order, with only a partial trailing line
        buffered. A response that only contains a completed payload raises
        NotImplementedError('streaming_unsupported...') so the caller
        renders completed-only honestly instead of chunking a finished string.
        Exactly one provider call is made, on first consumption.
        """
        try:
            from agent.auxiliary_client import resolve_provider_client, _build_call_kwargs
            client, resolved_model = resolve_provider_client(provider=provider, model=model)
            if client is None or resolved_model != model:
                raise RuntimeError('Unavailable exact route')
            kwargs = _build_call_kwargs(
                provider, model, messages, max_tokens=600, tools=None, timeout=30,
                reasoning_config={'effort': 'low'},
                base_url=str(getattr(client, 'base_url', '') or ''),
            )
            kwargs['stream'] = True
            try:
                response = client.chat.completions.create(**kwargs)
            except TypeError as exc:
                # Explicit no-silent-fallback: kwargs shape is Hermes-owned; a
                # stream-rejecting signature is completed-only, not an error.
                raise NotImplementedError('streaming_unsupported:provider-rejects-stream') from exc
        except NotImplementedError:
            raise
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        yielded = False
        iterator = None
        try:
            iterator = iter_openai_sse_deltas(response)
            for delta in iterator:
                yielded = True
                yield delta
        except NotImplementedError:
            raise
        except RuntimeError:
            raise RuntimeError('Selected answer model failed') from None
        except GeneratorExit:
            raise
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        finally:
            # Release the provider stream whether it finished, failed, or the
            # consumer moved on (cancellation / retired row).
            try:
                close_iter = getattr(iterator, 'close', None)
                if callable(close_iter):
                    close_iter()
            except Exception:
                pass
            try:
                close = getattr(response, 'close', None)
                if callable(close):
                    close()
            except Exception:
                pass
        if not yielded:
            raise NotImplementedError('streaming_unsupported:completed-only-response')

    complete.stream_chunks = stream_chunks  # type: ignore[attr-defined]
    return complete


def _delta_text(choice):
    """One incremental text delta from an SDK object or wire-dict choice."""
    if choice is None:
        return None
    getter = getattr(choice, 'get', None)
    if callable(getter):
        payload = choice
        get = lambda key: payload.get(key)
    else:
        get = lambda key: getattr(choice, key, None)
    delta = get('delta')
    if delta is not None:
        if isinstance(delta, dict):
            content = delta.get('content')
        else:
            content = getattr(delta, 'content', None)
        if isinstance(content, str) and content:
            return content
        return None
    message = get('message')
    if message is not None:
        if isinstance(message, dict):
            content = message.get('content')
        else:
            content = getattr(message, 'content', None)
        if isinstance(content, str) and content:
            return None  # completed-only object: never chunk a finished string
    text = get('text')
    if isinstance(text, str) and text:
        return text
    return None


def _wire_deltas(payload):
    """Incremental content deltas from one decoded SSE payload dict."""
    deltas = []
    if not isinstance(payload, dict):
        return deltas
    if 'error' in payload:
        # Provider failure frame (e.g. {"error": {...}}): a sanitized
        # failure, never silent completed-only, and provider
        # message/keys never enter the raised text.
        raise RuntimeError('Selected answer model failed')
    choices = payload.get('choices')
    if not isinstance(choices, list):
        return deltas
    for choice in choices:
        text = _delta_text(choice)
        if isinstance(text, str) and text:
            deltas.append(text)
    return deltas


_SSE_LINE_MAX_CHARS = 65536  # bound on one buffered partial SSE line


def _sse_line_deltas(line, _json):
    """Incremental deltas from one complete SSE line.

    Returns (deltas, done, truncated). Blank lines and comments carry
    nothing; `[DONE]` or a stop finish_reason ends the stream. A length
    finish_reason means the model was cut off by the output limit: the
    deltas so far are partial, never complete. Lines without SSE framing
    are skipped so a completed payload is never mistaken for incremental
    text; malformed SSE JSON raises RuntimeError.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(':'):
        return [], False, False
    if stripped.startswith('data:'):
        stripped = stripped[5:].strip()
    if stripped == '[DONE]':
        return [], True, False
    if 'data:' not in line and '"delta"' not in line and '"error"' not in line and '"finish_reason"' not in line:
        return [], False, False  # not SSE framing: never fake streaming from it
    try:
        payload = _json.loads(stripped)
    except ValueError:
        raise RuntimeError('Selected answer model failed')
    if not isinstance(payload, dict):
        return [], False, False
    truncated = any(isinstance(c, dict) and c.get('finish_reason') == 'length'
                    for c in (payload.get('choices') or []))
    done = (not truncated) and any(isinstance(c, dict) and c.get('finish_reason') == 'stop'
                                   for c in (payload.get('choices') or []))
    return list(_wire_deltas(payload)), done, truncated


def _make_wire_feed(_json):
    """Stateful SSE line reassembler shared by every wire-text path.

    Returns (feed, flush): feed(piece) parses complete lines the moment
    they arrive and returns their deltas in order, stopping at `[DONE]`;
    only the trailing partial line stays buffered (bounded). flush()
    parses any trailing partial line at stream end. Raises RuntimeError
    on malformed SSE JSON or an unbounded partial line. Bytes fragments
    are decoded with an incremental UTF-8 decoder so a multi-byte
    character split across chunks decodes intact at the boundary.
    """
    pending = ['']
    decoder = [__import__('codecs').getincrementaldecoder('utf-8')()]

    def _decode(piece):
        try:
            return decoder[0].decode(piece, False)
        except Exception:
            raise RuntimeError('Selected answer model failed') from None

    def _finish_decoder():
        try:
            tail = decoder[0].decode(b'', True)
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        decoder[0] = __import__('codecs').getincrementaldecoder('utf-8')()
        return tail

    def feed(piece):
        out = []
        fin = False
        length = False
        if isinstance(piece, dict):
            # A decoded stop chunk terminates honestly just like [DONE];
            # a length chunk means the model was cut off by the output limit.
            for choice in (piece.get('choices') or []):
                if not isinstance(choice, dict):
                    continue
                if choice.get('finish_reason') == 'stop':
                    fin = True
                elif choice.get('finish_reason') == 'length':
                    length = True
            try:
                piece = _json.dumps(piece) + '\n'
            except (TypeError, ValueError):
                raise RuntimeError('Selected answer model failed') from None
        elif isinstance(piece, bytes):
            piece = _decode(piece)
        elif not isinstance(piece, str):
            piece = str(piece)
        else:
            # A str piece after bytes means no more byte fragments can be
            # pending; flush any decoded tail before appending text.
            piece = _finish_decoder() + piece
        pending[0] += piece
        lines = pending[0].split('\n')
        pending[0] = lines.pop()
        if len(pending[0]) > _SSE_LINE_MAX_CHARS:
            raise RuntimeError('Selected answer model failed')
        for line in lines:
            if len(line) > _SSE_LINE_MAX_CHARS:
                raise RuntimeError('Selected answer model failed')
            deltas, done, truncated = _sse_line_deltas(line, _json)
            out.extend(deltas)
            if truncated:
                length = True
            if done or fin:
                pending[0] = ''
                _finish_decoder()
                return out, True, length
        return out, False, length

    def flush():
        tail = _finish_decoder()
        if tail:
            pending[0] += tail
        if pending[0].strip():
            deltas, done, truncated = _sse_line_deltas(pending[0], _json)
            pending[0] = ''
            return deltas, done, truncated
        return [], False, False

    return feed, flush


def _feed_wire_chunks(pieces, _json):
    """Yield deltas from text/bytes/dict pieces, reassembling split lines.

    Only the trailing partial line is buffered (bounded by
    _SSE_LINE_MAX_CHARS); completed lines are parsed and released as they
    arrive, in order. Stops at `[DONE]`. Transport/decoding failures raise
    RuntimeError.
    """
    feed, flush = _make_wire_feed(_json)
    yielded = 0
    finished = False
    try:
        for piece in pieces:
            deltas, done, length_limited = feed(piece)
            for delta in deltas:
                yielded += 1
                yield delta
            if length_limited:
                raise NotImplementedError('streaming_truncated:length-limit')
            if done:
                finished = True
                return
        flush_deltas, flush_done, flush_truncated = flush()
        for delta in flush_deltas:
            yielded += 1
            yield delta
        if flush_truncated:
            raise NotImplementedError('streaming_truncated:length-limit')
        if flush_done:
            finished = True
            return
        if yielded and not finished:
            # Deltas without a terminal [DONE] marker are a truncated
            # stream, not a successful answer: callers keep the partial
            # text with an interruption note instead of marking it done.
            raise NotImplementedError('streaming_truncated:eof-without-done')
    except NotImplementedError:
        raise
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError('Selected answer model failed') from None


def iter_openai_sse_deltas(response):
    """Yield incremental `content` deltas lazily, in arrival order.

    Single-pass over one-shot iterators (no re-read, no list()); only a
    partial trailing line is buffered (bounded). Split SSE frames are
    reassembled across chunk boundaries. Accepts: an iterable of SDK chunk
    objects with `choices[i].delta.content` (OpenAI SDK streaming shape),
    an iterable of SSE text chunks (requests-style `iter_lines` or raw
    pieces), a raw SSE text body, or an OpenAI-style object with
    `choices[0].message.content` (completed-only: yields nothing).

    Raises RuntimeError for transport/decoding failures; yields nothing
    for completed-only or empty streams so callers report completed-only
    honestly instead of synthesizing chunks. Never performs a second
    provider call: parsing only consumes the given stream.
    """
    import itertools as _itertools
    import json as _json

    if isinstance(response, (str, bytes)):
        raw = response.decode('utf-8', errors='replace') if isinstance(response, bytes) else response
        if 'data:' in raw or '[DONE]' in raw or '"delta"' in raw:
            yield from _feed_wire_chunks([raw], _json)
        return
    if hasattr(response, 'iter_lines'):
        # requests/httpx-style line iterator: iter_lines strips newlines,
        # so every item already IS one complete line. Parse each the moment
        # it arrives (true streaming, not end-flush) with no buffering
        # across items. Raw chunk reassembly lives in _feed_wire_chunks;
        # the two paths must not be conflated.
        try:
            line_iter = response.iter_lines()
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        yielded = 0
        finished = False
        try:
            for raw_line in line_iter:
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode('utf-8', errors='replace')
                elif not isinstance(raw_line, str):
                    raw_line = str(raw_line)
                deltas, done, truncated = _sse_line_deltas(raw_line, _json)
                for delta in deltas:
                    yielded += 1
                    yield delta
                if truncated:
                    raise NotImplementedError('streaming_truncated:length-limit')
                if done:
                    finished = True
                    return
            if yielded and not finished:
                raise NotImplementedError('streaming_truncated:eof-without-done')
        except NotImplementedError:
            raise
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        finally:
            close_lines = getattr(line_iter, 'close', None)
            if callable(close_lines):
                try:
                    close_lines()
                except Exception:
                    pass
        return
    if hasattr(response, '__iter__') and not hasattr(response, 'choices'):
        try:
            iterator = iter(response)
            first = next(iterator)
        except StopIteration:
            return
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        if isinstance(first, (str, bytes, dict)):
            yield from _feed_wire_chunks(_itertools.chain([first], iterator), _json)
            return
        # SDK chunk objects: single pass, arrival order, no re-read. A shared
        # wire reassembler joins split text frames without buffering
        # completed lines. Completed-only object chunks (message content)
        # yield nothing, so callers report completed-only honestly.
        feed, flush = _make_wire_feed(_json)
        yielded = 0
        finished = False
        def _finished_choice(choice):
            if isinstance(choice, dict):
                return choice.get('finish_reason')
            return getattr(choice, 'finish_reason', None)
        try:
            for chunk in _itertools.chain([first], iterator):
                if isinstance(chunk, dict):
                    for delta in _wire_deltas(chunk):
                        yielded += 1
                        yield delta
                    for choice in (chunk.get('choices') or []):
                        if not isinstance(choice, dict):
                            continue
                        if choice.get('finish_reason') == 'stop':
                            finished = True
                            return
                        elif choice.get('finish_reason') == 'length':
                            raise NotImplementedError('streaming_truncated:length-limit')
                    continue
                if isinstance(chunk, (str, bytes)):
                    # Fell back to wire text mid-stream: parse this piece in
                    # place with the shared reassembler, without rewinding
                    # deltas already yielded.
                    deltas, done, length_limited = feed(chunk)
                    for delta in deltas:
                        yielded += 1
                        yield delta
                    if length_limited:
                        raise NotImplementedError('streaming_truncated:length-limit')
                    if done:
                        finished = True
                        return
                    continue
                choices = getattr(chunk, 'choices', None)
                if not isinstance(choices, (list, tuple)):
                    continue
                for choice in choices:
                    reason = _finished_choice(choice)
                    text = _delta_text(choice)
                    if isinstance(text, str) and text:
                        yielded += 1
                        yield text
                    if reason == 'stop':
                        # Terminal: stop consuming the iterator here. A
                        # stalled or failing tail must not turn an otherwise
                        # completed answer into a failure.
                        finished = True
                        return
                    if reason == 'length':
                        # Cut off by the output limit: partial text only.
                        raise NotImplementedError('streaming_truncated:length-limit')
            # Trailing partial wire frame, if any, completes the stream.
            flush_deltas, flush_done, flush_truncated = flush()
            for delta in flush_deltas:
                yielded += 1
                yield delta
            if flush_truncated:
                raise NotImplementedError('streaming_truncated:length-limit')
            if flush_done:
                finished = True
                return
            if yielded and not finished:
                # Content without a terminal finish_reason is a truncated
                # stream, not a successful answer.
                raise NotImplementedError('streaming_truncated:eof-without-done')
        except NotImplementedError:
            raise
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        return
    # Completed-only objects (choices[0].message.content) and anything else
    # carry no incremental text: yield nothing, never synthesize chunks.
    return


def extract_openai_sse_deltas(response):
    """Eagerly collect incremental `content` deltas from an SSE stream, or None.

    Consumes the whole stream before returning; prefer
    iter_openai_sse_deltas when tokens must reach the caller as they
    arrive. Returns a list of non-empty text deltas in arrival order, or
    None when the payload only carries a completed answer (honest
    completed-only signal so callers never fake streaming by chunking a
    finished string). Raises RuntimeError for malformed streams. Accepts:
    an iterable of SDK chunk objects with `choices[i].delta.content`
    (OpenAI SDK streaming shape), an iterable of SSE text chunks
    (requests-style `iter_lines`), a raw SSE text body, or an OpenAI-style
    object with `choices[0].message.content` (completed-only).
    """
    try:
        deltas = list(iter_openai_sse_deltas(response))
    except NotImplementedError:
        raise
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError('Selected answer model failed') from None
    return deltas or None
