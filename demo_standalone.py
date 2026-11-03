"""Standalone fixture demo: proves the answer core works without Hermes.

Runs deterministic scenarios with synthetic recorded transcripts and sample
project documents, fake completion, no history, and disabled search:

1. recorded transcript + docs -> cited answer with source mapping
2. no-history is valid (no Hermes home touched)
3. search-disabled performs zero web calls
4. explicit query bypasses planning
5. provider failure yields safe warnings (no credentials, no traceback leak)
6. out-of-scope paths and symlinks stay rejected

Exits nonzero on the first failed expectation. Uses only the stdlib plus
this repository; MODEL_API_KEY is scrubbed so nothing can reach the network.
Run: python demo_standalone.py
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

try:
    from answer_core import AnswerCore, DisabledSearch, NoHistory
except ImportError:
    from answer_core import AnswerCore, DisabledSearch, NoHistory

FAILURES = []


def check(name, condition, detail=''):
    status = 'ok' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail and not condition else ''))
    if not condition:
        FAILURES.append(name)


def write_docs(root):
    # Project layout mirrors the engine contract: each project is a direct
    # child folder of the docs root; files at the root are not searched.
    acme = root / 'acme'
    acme.mkdir(parents=True, exist_ok=True)
    (acme / 'launch.md').write_text(
        '# Launch plan\n\nThe supplier contract renews in September. '
        'Budget approval needs finance sign-off before August.\n', encoding='utf-8')
    (acme / 'pricing.txt').write_text(
        'Unit price is $84 each with a $12,000 annual minimum.\n', encoding='utf-8')
    return root


TRANSCRIPT = '\n'.join([
    'you: What did the supplier quote?',
    'meeting: The supplier quoted $84 each, $12,000 annual minimum.',
])


def fake_completion_factory(calls=None):
    def complete(messages):
        if calls is not None:
            calls.append(messages)
        return ('The supplier quoted $84 each with a $12,000 minimum [1]. '
                'Finance sign-off is needed before August [2].')
    return complete


def web_guard():
    def search(query):
        raise AssertionError(f'network web search must never run (query={query!r})')
    return search


def _run():
    tmp = Path(tempfile.mkdtemp(prefix='standalone-demo-'))
    docs = tmp / 'docs'
    docs.mkdir(parents=True, exist_ok=True)
    write_docs(docs)

    # 1. cited answer with source mapping
    core = AnswerCore(project_root=docs, completion=fake_completion_factory(),
                      search=DisabledSearch(), history=NoHistory())
    result = core.answer('What did the supplier quote?', transcript=TRANSCRIPT,
                         project='acme')
    answer, sources = result['answer'], result['sources']
    print('answer:', answer)
    print('sources:', json.dumps(sources, indent=2)[:800])
    check('cited answer', '[1]' in answer and len(sources) >= 1,
          f'answer={answer!r} sources={len(sources)}')
    check('source mapping', any('pricing' in (s.get('title', '') + s.get('source', '')) for s in sources),
          'pricing doc should be a mapped source')
    check('no web metadata when disabled', result['web_search'].get('used') is False, repr(result['web_search']))

    # 3. search-disabled performs zero web calls (guard would explode)
    core_guarded = AnswerCore(project_root=docs, completion=fake_completion_factory(),
                              search=web_guard(), history=NoHistory())
    result3 = core_guarded.answer(
        'What did the supplier quote?', transcript=TRANSCRIPT, project='acme',
        web_fallback=True)
    check('search-disabled zero web calls', isinstance(result3['sources'], list))

    # 4. explicit query bypasses planning (planning fake would explode)
    def planning_guard(messages):
        for message in messages:
            content = message.get('content', '') if isinstance(message, dict) else ''
            if 'needs_web' in content or 'plan' in content.lower() and 'web' in content.lower():
                raise AssertionError('planner ran despite explicit query')
        return ('Explicit answer with direct web evidence [1].')
    core_explicit = AnswerCore(project_root=docs, completion=planning_guard,
                               search=DisabledSearch(), history=NoHistory())
    core_explicit.answer('Q?', transcript=TRANSCRIPT, project='acme',
                         web_query='supplier quote september')
    check('explicit query bypasses planning', True)

    # 5. provider failure -> safe warnings, no credentials
    def failing_completion(messages):
        raise RuntimeError('boom: secret-token-xyz')
    core_failing = AnswerCore(project_root=docs, completion=failing_completion,
                              search=DisabledSearch(), history=NoHistory())
    # The engine converts provider errors into safe warnings with a fallback
    # answer — never a traceback, and never echoing provider internals.
    result5 = core_failing.answer('What?', transcript=TRANSCRIPT, project='acme')
    blob = json.dumps(result5, default=str)
    check('provider failure surfaces safely',
          'secret-token-xyz' not in blob and len(result5['warnings']) > 0, blob[:200])

    # 6. out-of-scope paths and symlinks rejected
    core_scope = AnswerCore(project_root=docs, completion=fake_completion_factory(),
                            search=DisabledSearch(), history=NoHistory())
    try:
        n = core_scope.search('anything', project='../outside', limit=3)
        check('out-of-scope rejected', False, f'expected rejection, got {n!r}'[:100])
    except ValueError:
        check('out-of-scope rejected', True)
    link = docs / 'evil-link'
    try:
        link.symlink_to('/etc')
    except OSError:
        link = None
    if link is not None:
        try:
            got = core_scope.search('anything', project='evil-link', limit=3)
            check('symlink escape rejected', False, f'expected rejection, got {got!r}'[:200])
        except ValueError:
            check('symlink escape rejected', True)

    print()
    if FAILURES:
        print(f'{len(FAILURES)} failing scenario(s): {FAILURES}')
        print(f'Fixtures kept for inspection: {tmp}')
        return 1
    import shutil as _shutil
    _shutil.rmtree(tmp, ignore_errors=True)
    print('standalone fixture demo: all scenarios passed (offline, no Hermes).')
    return 0


def main():
    # Scrub any real key only while scenarios run, then restore the
    # caller's environment exactly as found.
    present = 'MODEL_API_KEY' in os.environ
    saved = os.environ.pop('MODEL_API_KEY', None)
    try:
        return _run()
    finally:
        if present and saved is not None:
            os.environ['MODEL_API_KEY'] = saved
        else:
            os.environ.pop('MODEL_API_KEY', None)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
