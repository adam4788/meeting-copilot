"""Standalone ask: grounded answers with no Hermes host required.

Reads a transcript file plus a project-docs directory, answers one
question with numbered citations, and prints sources, warnings, and
timing. Offline by default (fake provider); Meta provider via
MODEL_API_KEY for real answers. Web research and session history are
unavailable standalone — the tool says so instead of failing obscurely.

Usage:
  python ask.py --question "..." --docs ./sample-docs \
      [--transcript meeting.json] [--project NAME] [--provider fake|meta] \
      [--model meta-...] [--json]
  The default provider is fake (fully offline). Use --provider meta with a
  real MODEL_API_KEY for live answers.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from .answer_core import AnswerCore, DisabledSearch, NoHistory
except ImportError:
    from answer_core import AnswerCore, DisabledSearch, NoHistory


def fake_completion_factory():
    def complete(messages):
        # Cite only what the prompt actually contains: claiming [1] with no
        # retrieved evidence would be a fabricated citation.
        count = 0
        for message in messages or []:
            content = message.get('content', '') if isinstance(message, dict) else ''
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except ValueError:
                continue
            evidence = payload.get('evidence') if isinstance(payload, dict) else None
            if isinstance(evidence, list):
                count += len(evidence)
        if count:
            return ('Offline demo answer (fake provider — configure MODEL_API_KEY '
                    'and --provider meta for a real model). Grounded in the '
                    f'{count} retrieved source(s) below [1].')
        return ('Offline demo answer (fake provider). No local evidence '
                'matched, so nothing is cited.')
    return complete


def _transcript_from_text(text):
    """Parse transcript text: Meetily JSON, a segments list, or plain text."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    segments = None
    if isinstance(data, dict):
        segments = data.get('segments')
        if segments is None and isinstance(data.get('transcript'), list):
            segments = data['transcript']
    elif isinstance(data, list):
        segments = data
    if not isinstance(segments, list):
        return text
    lines = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        body = seg.get('text', '')
        if not isinstance(body, str) or not body.strip():
            continue
        speaker = seg.get('speaker', 'meeting')
        lines.append(f'{speaker}: {body.strip()}')
    return '\n'.join(lines)


def load_transcript(path):
    """Accept a Meetily transcripts.json, a segments list, or plain text.

    ``-`` reads the transcript from stdin.
    """
    if str(path) == '-':
        return _transcript_from_text(sys.stdin.read())
    return _transcript_from_text(Path(path).read_text(encoding='utf-8-sig'))


def build_completion(provider, model):
    if provider == 'fake':
        return fake_completion_factory()
    if provider == 'meta':
        try:
            from .meta_models import api_key_present, meta_completion
        except ImportError:
            from meta_models import api_key_present, meta_completion
        if not api_key_present():
            raise SystemExit(
                'Meta provider needs MODEL_API_KEY (min 8 chars) in the '
                'environment. Use --provider fake for an offline smoke test.')
        return meta_completion(model)
    raise SystemExit(f'Unknown provider {provider!r}: use fake|meta')


def _print_sources(sources):
    if not sources:
        print('Sources: none found — try broader terms, another --project, or --all.')
        return
    print('Sources:')
    for i, source in enumerate(sources, 1):
        title = source.get('title') or source.get('source') or 'source'
        print(f'  [{i}] {title} ({source.get("source", "")})')
        snippet = (source.get('text') or '').strip().replace('\n', ' ')
        if snippet:
            print(f'      {snippet[:200]}')


class _CliParser(argparse.ArgumentParser):
    """ArgumentParser that honors --json for its own errors too."""

    def error(self, message):
        argv = getattr(self, '_argv', [])
        json_requested = any(
            token == '--json' or token.startswith('--json=')
            or (token.startswith('--') and len(token) > 2 and '--json'.startswith(token))
            for token in argv)
        if json_requested:
            print(json.dumps({'error': message}, indent=2))
            raise SystemExit(2)
        super().error(message)


def main(argv=None):
    parser = _CliParser(description='Standalone grounded Q&A (no Hermes required).')
    parser._argv = list(argv) if argv is not None else sys.argv[1:]
    parser.add_argument('--question', default=None, help='Question to answer.')
    parser.add_argument('--docs', default=None, help='Directory of project documents (.md/.txt/.rst).')
    parser.add_argument('--transcript', default=None, help='Transcript file (Meetily JSON, segments JSON, or text); - reads stdin.')
    parser.add_argument('--project', default=None, help='Project subfolder under --docs '
                        '(default: the only subfolder, if exactly one).')
    parser.add_argument('--all', action='store_true', help='Search all projects under --docs.')
    parser.add_argument('--provider', default='fake', choices=('meta', 'fake'),
                        help='Completion backend (default fake: fully offline).')
    parser.add_argument('--model', default=None, help='Meta model override (meta provider only).')
    parser.add_argument('--web-query', default=None,
                        help='NOT YET SUPPORTED standalone: exits with guidance.')
    parser.add_argument('--search', default=None, metavar='TERMS',
                        help='Evidence-only search over docs; no model call is made.')
    parser.add_argument('--list-projects', action='store_true',
                        help='List project folders under --docs and exit.')
    parser.add_argument('--limit', type=int, default=None, help='Evidence limit for --search (1-20, default 6).')
    parser.add_argument('--json', action='store_true', help='Emit machine-readable JSON.')
    parser.add_argument('--version', action='version', version='meeting-copilot standalone 0.2.0')
    args = parser.parse_args(argv)

    def fail(message, code=1):
        # JSON callers get machine-readable errors on stdout, never a
        # plain-text SystemExit they cannot parse.
        if args.json:
            print(json.dumps({'error': message}, indent=2))
            raise SystemExit(code)
        raise SystemExit(message)

    if args.docs is None:
        fail('Missing --docs DIR. Quickstart: '
             'python ask.py --docs ./sample-docs --list-projects')
    docs = Path(args.docs)
    if not docs.is_dir():
        fail(f'Docs directory not found: {docs}')
    if args.web_query is not None:
        fail('Web research has no standalone provider yet (host search only). '
             'Run without --web-query for file-grounded answers.')
    if args.search is not None and args.question:
        fail('Choose --search TERMS or --question, not both.')
    if args.list_projects and (args.search is not None or (args.question or '').strip()):
        fail('Choose one mode: --list-projects, --search, or --question.')
    if args.search is not None and not args.search.strip():
        fail('Search terms cannot be empty.')
    if not args.list_projects and args.search is None and not (args.question or '').strip():
        fail('What do you want to do?\n'
             '  Ask:          python ask.py --docs ./sample-docs --question "What is the unit price?"\n'
             '  Search docs:  python ask.py --docs ./sample-docs --search "unit price"\n'
             '  List projects: python ask.py --docs ./sample-docs --list-projects')
    if args.project and args.all:
        fail('Choose --project NAME or --all, not both.')
    if args.limit is not None and args.search is None:
        print('Warning: --limit applies only to --search and is ignored here.', file=sys.stderr)
    if args.list_projects and args.all:
        print('Warning: --all is ignored with --list-projects.', file=sys.stderr)
    if (args.list_projects or args.search is not None) and args.question is not None and not args.question.strip():
        print('Warning: --question is ignored in this mode.', file=sys.stderr)
    if args.model and args.provider == 'fake':
        print('Warning: --model is ignored with --provider fake.', file=sys.stderr)
    if (args.list_projects or args.search is not None) and args.provider != 'fake':
        print('Warning: --provider/--model are ignored in this mode; no model is called.', file=sys.stderr)
    if args.search is not None and args.transcript is not None:
        print('Warning: --transcript is ignored with --search; evidence comes from docs only.', file=sys.stderr)
    if args.list_projects and args.project is not None:
        print('Warning: --project is ignored with --list-projects.', file=sys.stderr)
    if args.list_projects and args.transcript is not None:
        print('Warning: --transcript is ignored with --list-projects.', file=sys.stderr)

    subs = sorted(sub.name for sub in docs.iterdir()
                    if sub.is_dir() and not sub.name.startswith('.') and not sub.is_symlink())
    if args.list_projects:
        if args.json:
            print(json.dumps({'projects': [{'name': name} for name in subs]}, indent=2))
        elif subs:
            print('Projects:')
            for name in subs:
                print(f'  - {name}')
            print(f'\nAsk one: python ask.py --docs {docs} --project {subs[0]} --question "..."')
        else:
            print(f'No project folders under {docs}. Create one folder per project containing .md/.txt/.rst files.')
        return 0
    project = args.project
    if project is None and not args.all:
        if len(subs) == 1:
            project = subs[0]
        else:
            fail('Pick one project with --project NAME (or --all): '
                 + (', '.join(subs) if subs else '(no project folders under --docs)'))
    if project is not None and project not in subs:
        fail(f'No project {project!r} under {docs}.'
             + (f' Available: {", ".join(subs)}.' if subs else ' No project folders found.'))

    if args.search is not None:
        # Evidence-only mode never calls a model: a completion that raises
        # if invoked proves the point and keeps the mode fully offline.
        def _never_called(messages):
            raise AssertionError('search mode must not call a model')
        core = AnswerCore(project_root=docs, completion=_never_called,
                          search=DisabledSearch(), history=NoHistory())
        search_limit = args.limit if args.limit is not None else 6
        if search_limit < 1 or search_limit > 20:
            fail('--limit must be between 1 and 20.')
        try:
            found = core.search(args.search, project=project, limit=search_limit,
                                all_projects=args.all)
        except ValueError as exc:
            fail(f'Invalid request: {exc}')
        if args.json:
            print(json.dumps({'query': args.search, 'count': len(found), 'sources': found},
                             indent=2, default=str))
        else:
            _print_sources(found)
        return 0

    try:
        transcript = load_transcript(args.transcript) if args.transcript else None
    except (OSError, ValueError) as exc:
        fail(f'Cannot read transcript: {exc}')
    if transcript is None:
        transcript = ''
    saved_model_key = None
    model_key_present = False
    if args.provider == 'fake':
        # Fake means fully offline: the engine would otherwise try the Meta
        # default route whenever a key happens to be configured. Scrub it
        # only for the answer call and restore the caller's environment.
        model_key_present = 'MODEL_API_KEY' in os.environ
        saved_model_key = os.environ.pop('MODEL_API_KEY', None)
    started = time.monotonic()
    try:
        core = AnswerCore(project_root=docs, completion=build_completion(args.provider, args.model),
                          search=DisabledSearch(), history=NoHistory())
        result = core.answer(args.question, transcript=transcript, project=project,
                             all_projects=args.all)
    except SystemExit as exc:
        fail(str(exc.code))
    except (ValueError, OSError) as exc:
        fail(f'Invalid request: {exc}')
    finally:
        if model_key_present and saved_model_key is not None:
            os.environ['MODEL_API_KEY'] = saved_model_key
        elif saved_model_key is not None:
            os.environ.pop('MODEL_API_KEY', None)
    if args.provider == 'fake':
        # The static engine warnings assume a real provider and host
        # history; with a fake completion and no history they mislead.
        kept = [w for w in result['warnings']
                if 'may be sent to' not in w and 'prior-session' not in w]
        result['warnings'] = (['Offline demo: no data leaves this machine; web research '
                               'and session history are disabled.'] + kept)
    else:
        # ask.py never wires session history, so the engine's prior-session
        # caveat is stale here even for a live provider.
        result['warnings'] = [w for w in result['warnings'] if 'prior-session' not in w]
    total = time.monotonic() - started
    failed = bool(result.get('provider_error'))
    if args.json:
        print(json.dumps({**result, 'total_seconds': total}, indent=2, default=str))
        if failed:
            print(f"\n(request failed: {result['provider_error']}; see warnings in JSON)", file=sys.stderr)
        return 2 if failed else 0
    print(result['answer'])
    print()
    _print_sources(result['sources'])
    if result['warnings']:
        print()
        print('Warnings:')
        for warning in result['warnings']:
            print(f'  - {warning}')
    if result.get('provider_error'):
        # The provider call failed; the printed answer is an explicit
        # fallback. Signal scripting callers without hiding the payload.
        print(f"\n(request failed: {result['provider_error']}; see warnings above)", file=sys.stderr)
        if not args.json:
            print(f"\n({result['elapsed_seconds']:.2f}s engine, {total:.2f}s total; no answer produced)")
        return 2
    if args.provider == 'fake':
        print(f"\n({result['elapsed_seconds']:.2f}s engine, {total:.2f}s total; offline web/history disabled)")
    else:
        print(f"\n({result['elapsed_seconds']:.2f}s engine, {total:.2f}s total; web research and session history "
              f"disabled — question plus selected snippets went to {args.provider}.)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
