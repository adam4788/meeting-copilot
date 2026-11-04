"""Direct Meta Model API route (Muse Spark) for the meeting copilot.

The hackathon requires the app to actually use Meta models. This module calls
the Meta Model API's OpenAI-compatible Chat Completions endpoint directly, so
Meta usage never depends on Hermes provider configuration. MODEL_API_KEY comes
from Hermes' trusted secret scope, or the standalone process environment. The
launch owner's startup bridge may seed its saved dotenv value when the process
key is empty; routed scopes never borrow that key. No request body supplies keys
and nothing here writes Hermes configuration or credential files.
"""
import codecs
import json
import os
from pathlib import Path
import urllib.request


def _secret_scope():
    """Only standalone installs without Hermes may use the plain environment."""
    try:
        from agent import secret_scope
        return secret_scope
    except ModuleNotFoundError as exc:
        if exc.name == 'agent':
            return None
        raise


def _load_dotenv_key_once():
    """Seed the launch owner's key once; never publish routed profile secrets.

    A genuinely nonempty service environment wins. Empty/whitespace inherited
    placeholders do not block a saved key. Editing and startup reject multiline
    files (last assignment wins, no expansion). This is startup/unscoped
    precedence ONLY; a host-built request scope can prefer its saved file.
    """
    if os.environ.get('MODEL_API_KEY', '').strip():
        return
    if not os.environ.get('HERMES_LAUNCHD_LABEL', '').startswith('ai.hermes.gateway'):
        return
    try:
        scope = _secret_scope()
        if scope is not None and scope.serves_routed_profile():
            return
        if __package__:
            from .credential_env import bindings, saved
        else:
            from credential_env import bindings, saved
        home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
        with (home / '.env').open('rb') as file:
            raw = file.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return
        value = saved(bindings(raw)).strip()
        if value and not os.environ.get('MODEL_API_KEY', '').strip():
            os.environ['MODEL_API_KEY'] = value
    except Exception:
        # No secret, file content or host exception may escape startup.
        return


_load_dotenv_key_once()


def resolve_model_api_key():
    """Use the host's snapshot, never a foreign launch key.

    New host-scoped requests may see file edits immediately and prefer the file
    over inherited keys. Existing closures/workers keep their captured value.
    Unscoped owner code reads its process key; edits do not mutate that key.
    """
    try:
        scope = _secret_scope()
        if scope is None:
            key = os.environ.get('MODEL_API_KEY', '')
        else:
            # get_secret on some hosts still falls back to the launch env for
            # an unbound home override outside multiplex mode. Close that gap.
            if scope.serves_routed_profile() and scope.current_secret_scope() is None:
                return ''
            key = scope.get_secret('MODEL_API_KEY', '')
        return key.strip() if isinstance(key, str) else ''
    except Exception:
        return ''

META_BASE_URL = 'https://api.meta.ai/v1'
META_MODELS = ('muse-spark-1.3', 'muse-spark-1.2', 'muse-spark-1.1')
DEFAULT_META_MODEL = 'muse-spark-1.3'
META_PROVIDER_ID = 'meta'


def api_key_present():
    return len(resolve_model_api_key()) >= 8


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
    key = resolve_model_api_key()
    if len(key) < 8:
        raise ValueError('Meta Model API key is not configured')

    def complete(messages):
        body = json.dumps({'model': model, 'messages': messages, 'max_tokens': 600}).encode('utf-8')
        request = urllib.request.Request(
            META_BASE_URL + '/chat/completions', data=body,
            headers={'Authorization': 'Bearer ' + key.strip(), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(262144 + 1)
        except Exception as exc:
            raise RuntimeError(f'Meta Model API request failed ({type(exc).__name__})') from None
        if len(raw) > 262144:
            raise RuntimeError('Meta Model API response exceeded size bound') from None
        try:
            payload = json.loads(raw.decode('utf-8', errors='replace'))
        except Exception as exc:
            raise RuntimeError(f'Meta Model API request failed ({type(exc).__name__})') from None
        try:
            text = payload['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise RuntimeError('Meta Model API returned no text answer') from None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError('Meta Model API returned no text answer')
        return text.strip()

    def stream_chunks(messages):
        """Yield provider-native incremental SSE content deltas in arrival order.

        Uses the Meta OpenAI-compatible SSE route (`stream: true`). Responses
        that only carry a completed payload raise
        NotImplementedError('streaming_unsupported...') so callers show
        completed-only honestly instead of faking streaming.
        """
        body = json.dumps({'model': model, 'messages': messages, 'max_tokens': 600,
                           'stream': True}).encode('utf-8')
        request = urllib.request.Request(
            META_BASE_URL + '/chat/completions', data=body,
            headers={'Authorization': 'Bearer ' + key.strip(), 'Content-Type': 'application/json',
                     'Accept': 'text/event-stream'})
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except Exception as exc:
            raise RuntimeError(f'Meta Model API request failed ({type(exc).__name__})') from None
        deltas = []
        total_chars = 0
        leftover = ''
        decoder = codecs.getincrementaldecoder('utf-8')()
        # Prefer read1: HTTPResponse.read returns only when the full amount
        # arrives (or EOF), which blocks first-token latency on real SSE.
        # read1 returns after at most one underlying syscall and stays
        # chunked-encoding aware (HTTPResponse._read1_chunked). Fall back to
        # read for file-like test doubles without read1.
        read1 = getattr(response, 'read1', None)
        read_piece = read1 if callable(read1) else response.read
        try:
            while True:
                piece = read_piece(4096)
                if not isinstance(piece, (bytes, bytearray)):
                    raise RuntimeError('Meta Model API returned no text answer') from None
                if not piece:
                    try:
                        chunk = leftover + decoder.decode(b'', True)
                    except UnicodeError:
                        raise RuntimeError('Meta Model API returned no text answer') from None
                    leftover = ''
                else:
                    try:
                        chunk = leftover + decoder.decode(piece, False)
                    except UnicodeError:
                        raise RuntimeError('Meta Model API returned no text answer') from None
                    leftover = ''
                    if not chunk.endswith('\n'):
                        split = chunk.rfind('\n')
                        if split == -1:
                            leftover = chunk
                            if len(leftover) > 65536:
                                raise RuntimeError('Meta Model API returned no text answer') from None
                            continue
                        leftover, chunk = chunk[split + 1:], chunk[:split + 1]
                done = False
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line or line.startswith(':'):
                        continue
                    if line.startswith('data:'):
                        line = line[5:].strip()
                    if line == '[DONE]':
                        done = True
                        break
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        raise RuntimeError('Meta Model API returned no text answer') from None
                    if not isinstance(payload, dict):
                        continue
                    if 'error' in payload:
                        # Provider failure frame: sanitized RuntimeError, never
                        # silent completed-only, without echoing payload text.
                        raise RuntimeError('Meta Model API request failed (provider-error)') from None
                    choices = payload.get('choices')
                    if not isinstance(choices, list):
                        continue
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get('delta')
                        content = delta.get('content') if isinstance(delta, dict) else None
                        if isinstance(content, str) and content:
                            deltas.append(content)
                            yield content
                            total_chars += len(content)
                            if total_chars > 262144:
                                # Bounded accumulation: the server keeps the
                                # partial answer with an interruption note
                                # instead of growing without limit.
                                raise NotImplementedError('streaming_truncated:size-bound')
                        reason = choice.get('finish_reason')
                        if reason == 'length':
                            # Cut off by the output limit: partial text only,
                            # never a complete answer — even with [DONE].
                            raise NotImplementedError('streaming_truncated:length-limit')
                        if reason == 'stop':
                            done = True
                if done or not piece:
                    break
            if not deltas:
                # No incremental frames arrived: honest completed-only signal.
                raise NotImplementedError('streaming_unsupported:completed-only-response')
            if not done:
                # Deltas without a terminal marker are a truncated stream, not
                # a successful answer: the server keeps the partial text with
                # an interruption note and offers retry.
                raise NotImplementedError('streaming_truncated:eof-without-done')
        finally:
            try:
                response.close()
            except Exception:
                pass

    complete.stream_chunks = stream_chunks  # type: ignore[attr-defined]
    return complete
