"""Explicit fixed-origin Chat Completions routes. No credential files or fallback."""
import json
import re
import urllib.request
import urllib.error
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
