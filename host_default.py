"""Default answers via a genuine, locally supplied Hermes LLM facade only."""
import re
import threading
from pathlib import Path

_UNAVAILABLE = 'Hermes default is unavailable in this host mode. Reopen the plugin after Hermes loads it.'
_FAILURE = 'Hermes default answer failed. Check Hermes model setup and retry explicitly.'
_LOCK = threading.RLock()
_BINDING = None
_ROUTE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,199}\Z')


def _scope_home():
    try:
        from .byok import _host_scope
    except ImportError:
        from byok import _host_scope
    scope, home = _host_scope()
    if scope is None or not home:
        raise RuntimeError(_UNAVAILABLE)
    return Path(home).resolve()


def register(ctx):
    """Accept only the public in-process facade; remote detached calls are unsafe."""
    global _BINDING
    try:
        from agent.plugin_llm import PluginLlm
        from hermes_constants import get_hermes_home
        facade = ctx.llm
        if type(facade) is not PluginLlm:
            return
        home = Path(get_hermes_home()).resolve()
        token = object()
        retired = False
        def cleanup():
            global _BINDING
            nonlocal retired
            with _LOCK:
                retired = True
                if _BINDING is not None and _BINDING[0] is token:
                    _BINDING = None
        handle = ctx.on_unload(cleanup)
        if handle is None:
            return
        with _LOCK:
            if not retired:
                _BINDING = (token, facade, home)
    except Exception:
        return


def _admit(expected=None):
    try:
        from agent.plugin_llm import PluginLlm
        home = _scope_home()
        with _LOCK:
            binding = _BINDING
            if (binding is None or type(binding[1]) is not PluginLlm
                    or binding[2] != home or (expected is not None and binding is not expected)):
                raise RuntimeError(_UNAVAILABLE)
            return binding
    except Exception:
        raise RuntimeError(_UNAVAILABLE) from None


def catalog_entry():
    try:
        _admit()
        ready = True
    except Exception:
        ready = False
    return {'id': 'hermes', 'label': 'Follow Hermes default', 'models': ['default'],
            'custom_model': False, 'configured': ready,
            'status': ('Uses your current Hermes model and its configured fallback.' if ready else _UNAVAILABLE)}


def _route(provider, model):
    values = (provider, model)
    if any(not isinstance(v, str) or not _ROUTE.fullmatch(v) or '://' in v
           or re.search(r'(?i)(sk-|ghp_|github_pat_|bearer|api.key|password|token|eyJ[A-Za-z0-9_-]{10,})', v) for v in values):
        return 'hermes/default (host route unreported)'
    return provider + '/' + model


def completion():
    binding = _admit()
    def complete(messages):
        _admit(binding)
        try:
            result = binding[1].complete(messages=messages, max_tokens=2048,
                                         timeout=30, purpose='meeting_copilot')
            text = result.text
            if not isinstance(text, str) or not text.strip() or len(text) > 64000:
                raise ValueError()
            complete.model_route = _route(getattr(result, 'provider', None), getattr(result, 'model', None))
            return text.strip()
        except Exception:
            raise RuntimeError(_FAILURE) from None
    complete.model_route = 'hermes/default (host route unreported)'
    return complete
