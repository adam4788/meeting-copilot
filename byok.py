"""Explicit fixed-origin Chat Completions routes. No credential files or fallback."""
import json
import re
import urllib.request
import urllib.error
import threading
from pathlib import Path

PROVIDERS = {
    'openai': ('OpenAI API', 'OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'openrouter': ('OpenRouter', 'OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
}
_SECRET_NAMES = frozenset({'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'MODEL_API_KEY'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,199}\Z')

def validate(provider, model):
    if not isinstance(provider, str) or provider not in PROVIDERS or not isinstance(model, str) or not _ID.fullmatch(model) or '://' in model or model.lower() == 'auto' or re.search(r'(?i)(sk-|bearer|api.key|password|token)', model):
        raise ValueError('Choose a supported provider and exact model ID; never enter a key here.')
    return provider, model

def _host_scope():
    """Use only a host-stamped scope for the currently served profile."""
    from agent import secret_scope
    from hermes_constants import get_hermes_home
    scope = secret_scope.current_secret_scope()
    home = secret_scope.current_secret_scope_home()
    if scope is None or not home or Path(home).resolve() != get_hermes_home().resolve():
        return None, None
    return scope, home


def secret(name, *, standalone=False):
    # get_secret treats some deployment variables as global, so accept only
    # the declared profile-secret names before asking the host for a value.
    if name not in _SECRET_NAMES:
        return ''
    if standalone:
        return ''  # standalone execution has no authenticated host scope
    try:
        from agent import secret_scope
        scope, _ = _host_scope()
        if scope is None:
            return ''
        # Force strict scope semantics without replacing the host-issued
        # mapping. A concurrent change must never fall back to process env.
        token = secret_scope.set_multiplex_context(True)
        try:
            value = secret_scope.get_secret(name, '')
        finally:
            secret_scope.reset_multiplex_context(token)
    except Exception:
        return ''
    return value.strip() if isinstance(value, str) else ''


def _scope_available():
    try:
        scope, _ = _host_scope()
        return scope is not None
    except Exception:
        return False

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def catalog():
    providers=[]
    scoped = _scope_available()
    for provider, (label, name, _) in PROVIDERS.items():
        configured=bool(secret(name))
        status = ('Key available; connection and model unverified' if configured else
                  'Set key in Hermes Settings → Plugins → meeting-copilot-live (default profile)' if scoped else
                  'Hermes did not provide a profile-scoped credential context. BYOK is unavailable in this host mode.')
        providers.append(dict(id=provider, label=label, models=[], custom_model=True,
            configured=configured, status=status))
    return {'providers': providers}


def completion(provider, model, *, standalone=False):
    validate(provider, model)
    key = secret(PROVIDERS[provider][1], standalone=standalone)
    if not key:
        raise ValueError('Provider key unavailable. Configure Hermes plugin secrets and reopen the pane.')
    endpoint = PROVIDERS[provider][2]
    def complete(messages):
        # No SDK retries, redirects, model inference or secondary route.
        payload = {'model': model, 'messages': messages, 'max_completion_tokens': 2048} if provider == 'openai' else {'model': model, 'messages': messages, 'max_tokens': 2048}
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+key, 'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(262145)
            if len(raw)>262144:
                raise ValueError()
            data=json.loads(raw)
            choice=data['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise ValueError()
            text=choice['message']['content']
            if not isinstance(text,str) or not text.strip():
                raise ValueError()
            return text.strip()
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise RuntimeError(f'Provider request failed (HTTP {status}). Check key, credits, model ID and Chat Completions support; retry explicitly.') from None
        except Exception:
            raise RuntimeError('Provider returned no complete answer or connection failed. Check model compatibility; retry explicitly.') from None
    return complete


_MODEL_ENDPOINTS = {
    'openai': 'https://api.openai.com/v1/models',
    'openrouter': 'https://openrouter.ai/api/v1/models',
}
_MODEL_BYTES = 2 * 1024 * 1024
_MODEL_COUNT = 2000
_MODEL_TIMEOUT = 10
# One bounded outstanding listing per process. A timed-out daemon may finish its
# already admitted request, but cannot accumulate abandoned credential workers.
_DISCOVERY_LOCK = threading.Lock()


def _fetch_model_listing(request):
    if not _DISCOVERY_LOCK.acquire(blocking=False):
        raise RuntimeError('A model listing is still in progress. Wait for it to finish before retrying explicitly.')
    done, outcome = threading.Event(), {}
    def read():
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=_MODEL_TIMEOUT) as response:
                outcome['raw'] = response.read(_MODEL_BYTES + 1)
        except urllib.error.HTTPError as exc:
            outcome['status'] = exc.code
            exc.close()
        except Exception:
            outcome['failed'] = True
        finally:
            _DISCOVERY_LOCK.release()
            done.set()
    worker = threading.Thread(target=read, name='meeting-model-list', daemon=True)
    try:
        worker.start()
    except Exception:
        _DISCOVERY_LOCK.release()
        raise RuntimeError('Model listing could not start. Retry explicitly.') from None
    if not done.wait(_MODEL_TIMEOUT):
        raise RuntimeError('Model listing timed out. Its already submitted request may still finish; wait before retrying explicitly.')
    if 'status' in outcome:
        raise RuntimeError(f"Model listing failed (HTTP {outcome['status']}). Check the provider key and connection, then retry explicitly.")
    if outcome.get('failed') or 'raw' not in outcome:
        raise RuntimeError('Model listing unavailable. Check the connection and retry explicitly.')
    return outcome['raw']


def discover_models(provider):
    """Explicit, request-scoped listing only. Never called by readiness checks.

    Hermes' generic model picker can probe other providers, merge curated
    fallbacks and persist caches. Do not reuse it as proof of BYOK access.
    This GET sends only this provider's scoped key, never meeting content.
    """
    if provider not in PROVIDERS:
        raise ValueError('Choose OpenAI or OpenRouter before loading models.')
    if not _scope_available():
        raise ValueError('Profile-scoped credentials are unavailable. Reopen the pane in a supported Hermes host.')
    key = secret(PROVIDERS[provider][1])
    if not key:
        raise ValueError('Provider key unavailable. Configure Hermes plugin secrets and reopen the pane.')
    request = urllib.request.Request(_MODEL_ENDPOINTS[provider], headers={
        'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    raw = _fetch_model_listing(request)
    try:
        if len(raw) > _MODEL_BYTES:
            raise ValueError()
        payload = json.loads(raw)
        rows = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError()
        ids = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            model = row.get('id')
            try:
                validate(provider, model)
            except ValueError:
                continue
            # OpenRouter identifies modalities. Exclude non-text output when
            # specified, but do not infer account entitlement or compatibility.
            architecture = row.get('architecture')
            if provider == 'openrouter' and isinstance(architecture, dict):
                outputs = architecture.get('output_modalities')
                if isinstance(outputs, list) and 'text' not in outputs:
                    continue
            # OpenAI's list includes audio/image/embedding models. Conservative
            # chat-family filtering is a UI aid, not compatibility verification.
            if provider == 'openai' and (not re.match(r'^(gpt-|chatgpt-|o[1-9](?:-|$)|ft:(?:gpt-|o[1-9]))', model)
                    or re.search(r'(?i)(realtime|audio|transcrib|tts|image|search)', model)):
                continue
            if key in model or re.search(r'(?i)(ghp_|github_pat_|eyJ[A-Za-z0-9_-]{10,})', model):
                continue
            ids.add(model)
        if len(ids) > _MODEL_COUNT:
            raise ValueError()
        return {'provider': provider, 'models': sorted(ids),
                'source': 'account-list' if provider == 'openai' else 'provider-catalog',
                'status': ('Models returned for this OpenAI key; Chat Completions support, credits and access are unverified.'
                           if provider == 'openai' else
                           'OpenRouter public catalog, filtered for text output. Your account access, provider preferences, credits and compatibility are unverified.')}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise RuntimeError(f'Model listing failed (HTTP {status}). Check the selected provider key and connection, then retry explicitly.') from None
    except Exception:
        raise RuntimeError('Model listing unavailable or invalid. Check the connection or enter an exact model ID manually; retry explicitly.') from None
