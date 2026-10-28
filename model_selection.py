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

    return complete
