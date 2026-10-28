"""Safe Hermes model catalog and request-local routing; no credential/config writes."""
import re


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,199}\Z')
_SECRET = re.compile(r'(?i)(?:sk-|ghp_|github_pat_|bearer|api[_-]?key|password|access[_-]?token|refresh[_-]?token|eyJ[A-Za-z0-9_-]{10,})')
# These resolver aliases may auto-detect/fall through to a different backend.
_VIRTUAL = {'auto', 'custom', 'moa', 'lmstudio'}


def _safe_id(value):
    return (isinstance(value, str) and bool(_ID.fullmatch(value))
            and '://' not in value and not _SECRET.search(value))


def available_models():
    """JSON-only authenticated, concrete Hermes providers and authoritative catalogs.

    Generic custom/auto/MoA routes are omitted: they can auto-select another backend,
    or represent an endpoint without established authentication. Model availability
    remains subject to the provider/account at invocation time.
    """
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
    return {'providers': providers}


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
        """Yield provider-native incremental deltas. Completed chunks rejected.

        Responses are provider OpenAI-compatible SSE frames (`stream=True`)
        parsed incrementally; a response that only contains a completed payload
        raises NotImplementedError('streaming_unsupported...') so the caller
        renders completed-only honestly instead of chunking a finished string.
        """
        import json as _json
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
            deltas = extract_openai_sse_deltas(response)
            if deltas is None:
                raise NotImplementedError('streaming_unsupported:completed-only-response')
            for delta in deltas:
                yield delta
        except NotImplementedError:
            raise
        except Exception:
            raise RuntimeError('Selected answer model failed') from None

    complete.stream_chunks = stream_chunks  # type: ignore[attr-defined]
    return complete


def extract_openai_sse_deltas(response):
    """Pull incremental `content` deltas from an SSE stream, or None.

    Returns a list of non-empty text deltas in arrival order, or None when the
    payload only carries a completed answer (honest completed-only signal so
    callers never fake streaming by chunking a finished string). Raises
    RuntimeError for malformed/empty streams. Accepts: an iterable of SSE
    text chunks (requests-style `iter_lines`), a raw SSE text body, or an
    OpenAI-style object with `choices[0].message.content` (completed-only).
    """
    import json as _json

    def deltas_from_text(text):
        deltas = []
        for line in str(text).splitlines():
            line = line.strip()
            if not line or line.startswith(':'):
                continue
            if line.startswith('data:'):
                line = line[5:].strip()
            if line == '[DONE]':
                break
            try:
                payload = _json.loads(line)
            except ValueError:
                raise RuntimeError('Selected answer model failed')
            if not isinstance(payload, dict):
                continue
            for choice in payload.get('choices', []) if isinstance(payload.get('choices'), list) else []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta')
                if isinstance(delta, dict) and isinstance(delta.get('content'), str) and delta['content']:
                    deltas.append(delta['content'])
        return deltas

    if isinstance(response, (str, bytes)):
        raw = response.decode('utf-8', errors='replace') if isinstance(response, bytes) else response
        if 'data:' in raw or '[DONE]' in raw or '"delta"' in raw:
            deltas = deltas_from_text(raw)
            return deltas or None
        return None
    lines = None
    if hasattr(response, 'iter_lines'):
        try:
            lines = [c.decode('utf-8', errors='replace') if isinstance(c, bytes) else str(c)
                     for c in response.iter_lines()]
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        text = '\n'.join(lines)
        if 'data:' in text or '[DONE]' in text or '"delta"' in text:
            return deltas_from_text(text) or None
        return None
    if hasattr(response, '__iter__') and not hasattr(response, 'choices'):
        try:
            chunks = list(response)
        except Exception:
            raise RuntimeError('Selected answer model failed') from None
        joined = []
        for chunk in chunks:
            joined.append(chunk.decode('utf-8', errors='replace') if isinstance(chunk, bytes) else str(chunk))
        text = ''.join(joined)
        if 'data:' in text or '[DONE]' in text or '"delta"' in text:
            return deltas_from_text(text) or None
        return None
    choices = getattr(response, 'choices', None)
    if isinstance(choices, (list, tuple)) and choices:
        message = getattr(choices[0], 'message', None)
        content = getattr(message, 'content', None)
        if isinstance(content, str) and content.strip():
            return None  # completed-only object: report honestly, never chunk
    return None
