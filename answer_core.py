"""Standalone answer core: grounded Q&A without a Hermes host.

The core answers from project files, an optional transcript, and injected
capabilities. It never imports Hermes packages, reads a Hermes home, or
touches the network on its own — every host capability arrives as an
explicit constructor argument:

- completion: callable ``complete(messages) -> nonempty text`` (or an
  object with a ``.complete`` method). Required.
- search: callable ``search(public_query) -> raw web rows`` (or an object
  with ``.search``). ``DisabledSearch`` performs zero network access.
- history: callable ``search(terms) -> evidence records`` (or an object
  with ``.search``). ``NoHistory`` (the default) contributes nothing.

Compatibility: ``KnowledgeEngine`` keeps its exact public behavior when
constructed without overrides; the engine delegates to these same seams.
See hermes_adapters.py for the host-backed factories.
"""

try:
    from .knowledge import KnowledgeEngine
except ImportError:  # repo-root / installed-core layout
    from knowledge import KnowledgeEngine


class NoHistory:
    """History provider that contributes nothing. Missing history is valid."""
    def search(self, terms):
        return []


class DisabledSearch:
    """Search provider that performs no network access of any kind."""
    def search(self, query):
        return []


def _as_completion(value):
    if value is None:
        raise ValueError('A completion callable or provider is required')
    complete = getattr(value, 'complete', value)
    if not callable(complete):
        raise ValueError('Completion provider must be callable or expose .complete(messages)')
    return complete


def _as_search(value):
    if value is None:
        return DisabledSearch()
    search = getattr(value, 'search', value)
    if not callable(search):
        raise ValueError('Search provider must be callable or expose .search(query)')
    return search


def _as_history(value):
    if value is None:
        return NoHistory()
    search = getattr(value, 'search', value)
    if not callable(search):
        raise ValueError('History provider must be callable or expose .search(terms)')
    return search


class AnswerCore:
    """Host-independent grounded answering over project files + transcript."""

    def __init__(self, project_root=None, completion=None, search=None, history=None):
        if project_root is not None:
            from pathlib import Path as _Path
            root = _Path(project_root)
            if not root.is_dir():
                raise ValueError(f'Project root not found: {root}')
        self.engine = KnowledgeEngine(
            project_root=project_root,
            hermes_home=None,
            _completion=_as_completion(completion),
            _search=_as_search(search),
            _history=_as_history(history),
            _standalone=True,
        )

    def answer(self, question, transcript='', project=None, web_query=None,
               all_projects=False, web_fallback=False):
        """Same contract as KnowledgeEngine.answer: a dict with answer,
        sources, elapsed_seconds, warnings, web_search (plus routing fields)."""
        return self.engine.answer(
            question, transcript=transcript, project=project,
            web_query=web_query, all_projects=all_projects,
            web_fallback=web_fallback)

    def answer_stream(self, *args, **kwargs):
        return self.engine.answer_stream(*args, **kwargs)

    def search(self, question, project=None, limit=6, all_projects=False):
        return self.engine.search(question, project=project, limit=limit,
                                  all_projects=all_projects)

    def projects(self):
        return self.engine.projects()
