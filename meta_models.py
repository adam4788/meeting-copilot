"""Direct Meta Model API route (Muse Spark) for the meeting copilot.

The hackathon requires the app to actually use Meta models. This module calls
the Meta Model API's OpenAI-compatible Chat Completions endpoint directly, so
Meta usage never depends on Hermes provider configuration. The API key comes
only from the MODEL_API_KEY environment variable of the gateway process --
never from request bodies -- and nothing here reads or writes Hermes global
configuration or credentials.
"""
import json
import os
import urllib.request

META_BASE_URL = 'https://api.meta.ai/v1'
META_MODELS = ('muse-spark-1.3', 'muse-spark-1.2', 'muse-spark-1.1')
DEFAULT_META_MODEL = 'muse-spark-1.3'
META_PROVIDER_ID = 'meta'


def api_key_present():
    key = os.environ.get('MODEL_API_KEY', '')
    return isinstance(key, str) and len(key.strip()) >= 8


def validate_meta_model(model):
    """Allowlisted Meta model IDs only; None selects the default."""
    if model is None:
        return DEFAULT_META_MODEL
    if not isinstance(model, str) or model not in META_MODELS:
        raise ValueError('Answer model selection is unavailable')
    return model


def meta_completion(model=None):
    """Request-local completion closure over the Meta Model API."""
    model = validate_meta_model(model)
    key = os.environ.get('MODEL_API_KEY', '')
    if not isinstance(key, str) or len(key.strip()) < 8:
        raise ValueError('Meta Model API key is not configured')

    def complete(messages):
        body = json.dumps({'model': model, 'messages': messages, 'max_tokens': 600}).encode('utf-8')
        request = urllib.request.Request(
            META_BASE_URL + '/chat/completions', data=body,
            headers={'Authorization': 'Bearer ' + key.strip(), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode('utf-8', errors='replace'))
        except Exception as exc:
            raise RuntimeError(f'Meta Model API request failed ({type(exc).__name__})') from None
        try:
            text = payload['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise RuntimeError('Meta Model API returned no text answer') from None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError('Meta Model API returned no text answer')
        return text.strip()

    return complete
