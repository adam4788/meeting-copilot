"""Hermes host adapters for the standalone answer core.

All Hermes-specific imports live here and are lazy: importing this module
( or the core) never requires Hermes packages, environment variables, or a
real Hermes home. Anything that needs the host goes through one of the
factory functions below, which raise a clear error naming the missing
capability instead of failing deep inside answer assembly.

Meeting Copilot's BYOK runtime uses explicit provider selection. Embedders may
inject the public ctx.llm facade or their own completion, search and history
implementations. Dashboard search uses the profile-scoped public tool registry.
"""

_MISSING = object()


class WebSearchUnavailable(RuntimeError):
    """The host has no safely scoped public search dispatch available."""


def default_completion(llm=None):
    """Adapt an explicitly supplied public ``ctx.llm`` sync facade.

    This optional embedding helper follows the host's configured route and
    fallback policy. It is not used for Copilot's fixed-route BYOK answers.
    No host lookup, private resolver, model override or credential extraction.
    """
    def complete(messages):
        if not callable(getattr(llm, 'complete', None)):
            raise RuntimeError(
                'Hermes completion is unavailable: explicitly supply public '
                'ctx.llm or inject a standalone completion provider.')
        try:
            response = llm.complete(messages=messages, max_tokens=2048,
                                    timeout=30, purpose='meeting_copilot')
            text = response.text
        except Exception:
            raise RuntimeError('Hermes public completion failed') from None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError('No text answer returned')
        return text.strip()
    return complete


def completion_supports_streaming(complete):
    """Best-effort streaming probe for any completion callable."""
    try:
        probe = complete(messages=[{'role': 'user', 'content': 'ping'}], stream=True)
        iterator = iter(probe)
        first = next(iterator, None)
        close = getattr(iterator, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return first is not None
    except Exception:
        return False


def default_search(dispatch_tool=None):
    """Adapt an explicitly supplied public ``ctx.dispatch_tool`` callable.

    No implicit host context lookup. Dashboard callers must supply a dispatcher
    bound to their authenticated profile; this optional embedding helper cannot
    manufacture one.
    """
    def search(query):
        if not callable(dispatch_tool):
            raise RuntimeError('Hermes web search requires explicit public ctx.dispatch_tool; inject a SearchProvider or disable search.')
        try:
            import json as _json
            data = _json.loads(dispatch_tool('web_search', {'query': query, 'limit': 3}))
            if not isinstance(data, dict) or data.get('success') is not True:
                raise ValueError('dispatch failed')
            payload = data.get('data')
            if not isinstance(payload, dict):
                raise ValueError('invalid web results')
            rows = payload.get('web')
            if not isinstance(rows, list):
                raise ValueError('invalid web results')
            return rows[:3]
        except Exception:
            raise RuntimeError('Hermes public web search failed') from None
    return search


def registry_search(query):
    """Dispatch once through an already registered, profile-scoped host tool.

    Do not import tool implementations to populate the registry. A dashboard
    without a bound host scope or registered public tool reports unavailable.
    """
    try:
        from agent.secret_scope import current_secret_scope, current_secret_scope_home
        from hermes_constants import hermes_home_key
        from tools.registry import registry
        scope = hermes_home_key()
        bound = current_secret_scope_home()
        if (current_secret_scope() is None or not bound or not scope
                or hermes_home_key(bound) != scope
                or registry.get_entry('web_search', scope=scope) is None):
            raise WebSearchUnavailable()
    except Exception:
        raise WebSearchUnavailable('Hermes public web search is unavailable.') from None
    return default_search(lambda name, args: registry.dispatch(name, args, scope=scope))(query)


def default_history(hermes_home):
    """History callable reading the host session store (stdlib sqlite3).

    `hermes_home` is the resolved Hermes home directory. A missing or
    unreadable state.db simply yields no history — standalone runs treat a
    missing provider the same way.
    """
    from pathlib import Path
    import sqlite3
    import time
    from contextlib import closing

    def search(terms):
        path = Path(hermes_home) / 'state.db'
        if not path.is_file():
            return []
        terms = [t for t in (terms or []) if t]
        if not terms:
            return []
        docs = []
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.2)) as db:
                db.execute('PRAGMA query_only=ON')
                deadline = time.monotonic() + 0.4
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
                if not {'messages', 'sessions'}.issubset(tables):
                    return []
                mc = {r[1] for r in db.execute('PRAGMA table_info(messages)')}
                sc = {r[1] for r in db.execute('PRAGMA table_info(sessions)')}
                if not {'id', 'session_id', 'role', 'content', 'timestamp'}.issubset(mc) or 'id' not in sc:
                    return []
                filters = ["m.role IN ('user','assistant')", 'm.content IS NOT NULL']
                if 'tool_calls' in mc:
                    filters.append('m.tool_calls IS NULL')
                if 'profile_name' in sc:
                    filters.append("(s.profile_name IS NULL OR s.profile_name IN ('','default'))")
                if 'active' in mc:
                    filters.append('m.active=1')
                if 'hidden' in sc:
                    filters.append('COALESCE(s.hidden,0)=0')
                params = []
                if 'messages_fts' in tables:
                    filters.append('m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? LIMIT 100)')
                    params.append(' OR '.join('"' + t + '"' for t in terms))
                else:
                    filters.append('m.id IN (SELECT id FROM messages ORDER BY id DESC LIMIT 1000)')
                title = "COALESCE(s.title,'Prior session')" if 'title' in sc else "'Prior session'"
                sql = (f"SELECT m.id,s.id,{title},m.role,substr(m.content,1,6000),m.timestamp "
                       f"FROM messages m JOIN sessions s ON s.id=m.session_id "
                       f"WHERE {' AND '.join(filters)} ORDER BY m.id DESC LIMIT 100")
                for mid, sid, title, role, text, timestamp in db.execute(sql, params):
                    if not any(t in text.lower() for t in terms):
                        continue
                    if text.lstrip().startswith(('{', '[')):
                        try:
                            import json as _json
                            if isinstance(_json.loads(text), (dict, list)):
                                continue
                        except ValueError:
                            pass
                    label = 'unverified assistant statement' if role == 'assistant' else 'prior user statement'
                    docs.append({'id': f'session:{sid}#{mid}',
                                 'title': title.strip()[:160] if isinstance(title, str) else 'Prior session',
                                 'source': f'session:{sid}',
                                 'text': f'[{label}] ' + text.strip()[:2000],
                                 'updated_at': timestamp})
        except sqlite3.Error:
            return []
        return docs
    return search
