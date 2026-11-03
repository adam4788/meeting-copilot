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


_STOPWORDS = {'what', 'when', 'where', 'which', 'who', 'whom', 'whose', 'why',
              'how', 'does', 'did', 'the', 'a', 'an', 'of', 'for', 'to', 'in',
              'on', 'about', 'give', 'me', 'tell', 'please', 'according',
              'meeting', 'say', 'said', 'is', 'are', 'was', 'were', 'do'}


def _content_terms(value):
    import re as _re
    return {tok for tok in _re.findall(r'[a-z0-9]+', str(value).lower())
            if len(tok) >= 3 and tok not in _STOPWORDS}


def _term_hits(terms, value):
    tokens = _content_terms(value)
    hits = set()
    for term in terms:
        for tok in tokens:
            if tok == term or tok in (term + 's', term + 'es') or term in (tok + 's', tok + 'es'):
                hits.add(term)
                break
    return hits


def _passage(text, terms):
    import re as _re
    flat = ' '.join(str(text).split())
    if not flat or not terms:
        return flat[:220]
    lowered = flat.lower()
    positions = {}
    for match in _re.finditer(r'[a-z0-9]+', lowered):
        tok = match.group(0)
        for term in terms:
            if tok == term or tok in (term + 's', term + 'es') or term in (tok + 's', tok + 'es'):
                positions.setdefault(term, []).append(match.start())
                break
    if not positions:
        return flat[:220]
    # Choose the 220-char window containing the most distinct query terms,
    # not merely the window around the earliest stray mention.
    import bisect as _bisect
    candidates = ({0} | {max(0, pos - 40) for found in positions.values() for pos in found}
                  | {pos for found in positions.values() for pos in found}
                  | {max(0, pos - 219) for found in positions.values() for pos in found})
    start, best_score = 0, -1
    for cand in sorted(candidates):
        score = 0
        for term, found in positions.items():
            i = _bisect.bisect_left(found, cand)
            if i < len(found) and found[i] + len(term) <= cand + 220:
                score += 1
        if score > best_score:
            start, best_score = cand, score
    snippet = flat[start:start + 220]
    if start > 0:
        snippet = '…' + snippet
    if start + 220 < len(flat):
        snippet += '…'
    return snippet


def fake_completion_factory():
    def complete(messages):
        # Extractive offline answer: quote the top retrieved snippet only
        # when it actually shares content terms with the question. An
        # unrelated snippet is evidence, not an answer; if the transcript
        # itself matches better, say so (uncited) instead of dressing the
        # document up as the answer.
        evidence, question, transcript = [], '', ''
        for message in messages or []:
            content = message.get('content', '') if isinstance(message, dict) else ''
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get('evidence'), list):
                evidence = payload['evidence']
            if isinstance(payload.get('question'), str):
                question = payload['question']
            if isinstance(payload.get('transcript'), str):
                transcript = payload['transcript']
        terms = _content_terms(question)
        # Score what will actually be quoted: the best 220-char passage of
        # each transcript line / document, not whole bodies with widely
        # separated term mentions.
        def _speaker_body(body):
            body = body.strip()
            import re as _re2
            body = _re2.sub(r'^\[[^\]]*\]\s*', '', body)
            for _ in range(3):
                head, sep, tail = body.partition(':')
                if sep and len(head.split()) <= 4 and len(head) <= 40:
                    body = tail.strip()
                else:
                    break
            return body
        def _is_echo(line):
            body = _speaker_body(line)
            if body.endswith('?'):
                return bool(terms) and _content_terms(body) <= terms
            # Unpunctuated echo: an interrogative restatement of the
            # question ('what is alpha beta' without '?'). Declarative
            # short answers ('The unit price is $99') are not echoes.
            first = body.split()[0].strip('?:.,').lower() if body.split() else ''
            return (first in ('what', 'when', 'where', 'which', 'who', 'whom',
                              'whose', 'why', 'how', 'is', 'are', 'was', 'were',
                              'do', 'does', 'did', 'can', 'could', 'should',
                              'would', 'will')
                    and bool(terms) and _content_terms(body) <= terms)
        def _strip_echo_prefix(line):
            # 'Alice: what is alpha beta? approved' -> 'approved': the
            # question segment never scores, even on the same line.
            if '?' not in line:
                return '' if _is_echo(line) else line
            head, _, tail = line.partition('?')
            if _content_terms(_speaker_body(head)) <= terms and tail.strip():
                return tail.strip()
            return '' if _is_echo(line) else line
        best_line, best_hits = '', set()
        lines = str(transcript).splitlines()
        for i in range(len(lines)):
            kept = [part for part in (_strip_echo_prefix(part) for part in lines[i:i + 3])
                    if part and part.strip()]
            window = ' '.join(kept)
            if not window:
                continue
            passage = _passage(window, terms)
            hits = _term_hits(terms, passage)
            if len(hits) > len(best_hits):
                best_line, best_hits = passage, hits
        doc_hits, body_hits, passage_hits = set(), set(), set()
        snippet = ''
        if evidence:
            # Consider every retrieved document, not only the first: a
            # title-heavy top hit must not suppress a relevant later one
            # (or a relevant transcript).
            top, citation = None, 1
            for idx, item in enumerate(evidence):
                item_body = _term_hits(terms, item.get('text', ''))
                item_all = _term_hits(terms, f"{item.get('title', '')} {item.get('text', '')}")
                item_passage = _passage(item.get('text', ''), terms)
                item_passage_hits = (set() if _is_echo(item_passage)
                                     else _term_hits(terms, item_passage))
                if (len(item_passage_hits), len(item_body)) > (len(passage_hits), len(body_hits)):
                    top, body_hits, doc_hits = item, item_body, item_all
                    passage_hits, snippet = item_passage_hits, item_passage
                    citation = item.get('citation', idx + 1)
            # A one-term question ('Price?') can only ever score one hit;
            # multi-term questions need two so a shared generic word in an
            # unrelated document is not presented as the answer.
            required = 1 if len(terms) <= 1 else 2
            if snippet and len(passage_hits) >= required and len(passage_hits) >= len(best_hits):
                title = ' '.join(str(top.get('title', '')).split())[:80]
                redacted = ' (searched snippet redacted)' if 'REDACTED' in snippet else ''
                from_line = f' From {title}:' if title else ''
                return (f'Offline extractive answer (fake provider — no model was '
                        f'called){redacted}.{from_line} "{snippet}" [{citation}]. Configure '
                        f'MODEL_API_KEY with --provider meta for a synthesized answer.')
        transcript_required = 1 if len(terms) <= 1 else 2
        if best_line and len(best_hits) >= transcript_required and len(best_hits) > len(passage_hits):
            quote = _passage(best_line, terms)
            # Never present the question itself as the answer: if the
            # passage opens with the echoed question, quote what follows.
            head, sep, tail = quote.partition('?')
            head_body = head.split(':', 1)[1] if ':' in head[:60] else head
            if sep and tail.strip() and _content_terms(head_body) <= terms:
                quote = tail.strip()
            lead = ('the retrieved documents do not directly answer this; '
                    if evidence else 'no documents matched; ')
            return ('Offline extractive answer (fake provider — no model was '
                    f'called): {lead}the transcript itself says: "{quote}" '
                    '(transcript, uncited). Configure MODEL_API_KEY with '
                    '--provider meta for a synthesized answer.')
        if evidence:
            return ('Offline extractive answer (fake provider — no model was '
                    'called): retrieved evidence does not directly answer this '
                    'question, so no snippet is presented as an answer. See the '
                    'sources below, or configure MODEL_API_KEY with --provider '
                    'meta for a synthesized answer.')
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
        json_requested = False
        for token in argv:
            if token == '--':
                break  # end of options: later tokens are positional values
            if (token == '--json' or token.startswith('--json=')
                    or (token.startswith('--') and len(token) > 2
                        and '--json'.startswith(token))):
                json_requested = True
                break
        if json_requested:
            print(json.dumps({'error': message}, indent=2))
            raise SystemExit(2)
        super().error(message)


def main(argv=None):
    parser = _CliParser(description='Standalone grounded Q&A (no Hermes required).',
                        epilog='Examples: ask.py --docs ./sample-docs --list-projects | '
                               'ask.py --docs ./sample-docs --search "unit price" | '
                               'ask.py --docs ./sample-docs --question "What is the unit price?"')
    parser._argv = list(argv) if argv is not None else sys.argv[1:]
    parser.add_argument('--question', default=None, help='Question to answer.')
    parser.add_argument('--docs', default=None,
                        help='Docs root: one subfolder per project, with .md/.txt/.rst files inside each.')
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
    parser.add_argument('--json', action='store_true',
                        help='Emit one machine-readable JSON object on stdout (supported by all modes).')
    parser.add_argument('--version', action='version', version='meeting-copilot standalone 0.2.0')
    # Options after -- are positional, never flags: detect help/version
    # only before it, and honor --json for both outputs.
    pre = []
    for token in parser._argv:
        if token == '--':
            break
        pre.append(token)
    json_requested = any(token == '--json'
                         or (token.startswith('--j') and '=' not in token
                             and '--json'.startswith(token))
                         for token in pre)
    version_requested = any(token.startswith('--v') and '--version'.startswith(token)
                            for token in pre)
    if json_requested and version_requested:
        print(json.dumps({'version': 'meeting-copilot standalone 0.2.0'}, indent=2))
        return 0
    if json_requested and any(token in ('-h', '--help') for token in pre):
        print(json.dumps({'description': parser.description,
                          'usage': parser.format_usage().strip(),
                          'help': parser.format_help()}, indent=2))
        return 0
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
    if args.search is not None and (args.question or '').strip():
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
    if args.project and args.all and not args.list_projects:
        fail('Choose --project NAME or --all, not both.')
    # Warnings go to stderr for humans and into JSON payloads for machines.
    cli_warnings = []
    def warn(message):
        cli_warnings.append(message)
        print(f'Warning: {message}', file=sys.stderr)
    if args.limit is not None and args.search is None:
        warn('--limit applies only to --search and is ignored here.')
    if args.list_projects and args.all:
        warn('--all is ignored with --list-projects.')
    if (args.list_projects or args.search is not None) and args.question is not None and not args.question.strip():
        warn('--question is ignored in this mode.')
    if args.model and args.provider == 'fake':
        warn('--model is ignored with --provider fake.')
    if (args.list_projects or args.search is not None) and args.provider != 'fake':
        warn('--provider/--model are ignored in this mode; no model is called.')
    if args.search is not None and args.transcript is not None:
        warn('--transcript is ignored with --search; evidence comes from docs only.')
    if args.list_projects and args.project is not None:
        warn('--project is ignored with --list-projects.')
    if args.list_projects and args.transcript is not None:
        warn('--transcript is ignored with --list-projects.')

    import re as _re
    _denied = _re.compile(r'(auth|credential|secret|password|token|config|\.env)', _re.I)
    _skip = {'node_modules', 'venv', 'env', 'build', 'dist', '__pycache__', 'profiles'}
    subs = sorted(sub.name for sub in docs.iterdir()
                  if sub.is_dir() and not sub.name.startswith('.')
                  and not sub.is_symlink() and not _denied.search(sub.name)
                  and sub.name.lower() not in _skip)
    if subs:
        root_files = [entry.name for entry in docs.iterdir()
                      if entry.is_file() and entry.suffix.lower() in ('.md', '.txt', '.rst')]
        if root_files:
            warn(f'document files at the docs root ({", ".join(root_files[:3])}) '
                 'are not searched; only project subfolders are.')
    if not subs and not args.list_projects:
        # A docs directory full of files but no project subfolders is the
        # most common layout mistake: say exactly what to change instead
        # of silently returning no sources.
        root_files = [entry.name for entry in docs.iterdir()
                      if entry.is_file() and entry.suffix.lower() in ('.md', '.txt', '.rst')]
        detail = (f' Found document files at its root ({", ".join(root_files[:3])}); '
                  'move them into a subfolder per project.' if root_files else '')
        fail(f'No project folders under {docs}.{detail} '
             'Expected layout: <docs>/<project>/*.md|txt|rst — '
             'or pass the parent directory as --docs.')
    if args.list_projects:
        if args.json:
            payload = {'projects': [{'name': name} for name in subs]}
            if not subs:
                payload['warning'] = (f'No project folders under {docs}. Create one folder '
                                      'per project containing .md/.txt/.rst files, or pass '
                                      'the parent directory as --docs.')
                cli_warnings.append(payload['warning'])
            if cli_warnings:
                payload['warnings'] = list(cli_warnings)
            print(json.dumps(payload, indent=2))
        elif subs:
            print('Projects:')
            for name in subs:
                print(f'  - {name}')
            import shlex as _shlex
            print(f'\nAsk one: python ask.py --docs {_shlex.quote(str(docs))} --project {_shlex.quote(subs[0])} --question "..."')
        else:
            print(f'No project folders under {docs}. Create one folder per project '
                  'containing .md/.txt/.rst files, or pass the parent directory as --docs.')
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
        if len(found) >= search_limit:
            warn(f'results hit the --limit cap ({search_limit}); more evidence may exist. '
                 'Raise --limit (max 20) or narrow the terms.')
        if args.json:
            payload = {'query': args.search, 'count': len(found), 'sources': found}
            if cli_warnings:
                payload['warnings'] = list(cli_warnings)
            print(json.dumps(payload, indent=2, default=str))
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
        result['warnings'] = (['Offline extractive demo (fake provider — no model call): '
                               'no data leaves this machine; web research and session '
                               'history are disabled.'] + kept)
    else:
        # ask.py never wires session history, so the engine's prior-session
        # caveat is stale here even for a live provider.
        result['warnings'] = [w for w in result['warnings'] if 'prior-session' not in w]
    if cli_warnings:
        result['warnings'] = list(cli_warnings) + list(result.get('warnings') or [])
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
