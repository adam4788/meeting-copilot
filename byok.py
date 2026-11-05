"""Explicit fixed-origin Chat Completions routes. No credential files or fallback."""
import json
import os
import re
import urllib.request
import urllib.error

PROVIDERS = {
    'openai': ('OpenAI API', 'OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'openrouter': ('OpenRouter', 'OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,199}\Z')

def validate(provider, model):
    if not isinstance(provider, str) or provider not in PROVIDERS or not isinstance(model, str) or not _ID.fullmatch(model) or '://' in model or model.lower() == 'auto' or re.search(r'(?i)(sk-|bearer|api.key|password|token)', model):
        raise ValueError('Choose a supported provider and exact model ID; never enter a key here.')
    return provider, model

def secret(name, *, standalone=False):
    if standalone:
        value = os.environ.get(name, '')
    else:
        try:
            from agent import secret_scope
            if secret_scope.current_secret_scope() is None:
                return ''
            value = secret_scope.get_secret(name, '')
        except Exception:
            return ''
    return value.strip() if isinstance(value, str) else ''

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def catalog():
    providers=[]
    for provider, (label, name, _) in PROVIDERS.items():
        configured=bool(secret(name))
        providers.append(dict(id=provider, label=label, models=[], custom_model=True,
            configured=configured, status='Key available; connection and model unverified' if configured else 'Set key in Hermes Settings → Plugins → meeting-copilot-live (default profile)'))
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
