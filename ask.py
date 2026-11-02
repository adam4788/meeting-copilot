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


def load_transcript(path):
    """Accept a Meetily transcripts.json, a segments list, or plain text."""
    text = Path(path).read_text(encoding='utf-8')
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


def main(argv=None):
    parser = argparse.ArgumentParser(description='Standalone grounded Q&A (no Hermes required).')
    parser.add_argument('--question', required=True, help='Question to answer.')
    parser.add_argument('--docs', required=True, help='Directory of project documents (.md/.txt/.rst).')
    parser.add_argument('--transcript', default=None, help='Transcript file (Meetily JSON, segments JSON, or text).')
    parser.add_argument('--project', default=None, help='Project subfolder under --docs '
                        '(default: the only subfolder, if exactly one).')
    parser.add_argument('--all', action='store_true', help='Search all projects under --docs.')
    parser.add_argument('--provider', default='fake', choices=('meta', 'fake'),
                        help='Completion backend (default fake: fully offline).')
    parser.add_argument('--model', default=None, help='Meta model override.')
    parser.add_argument('--web-query', default=None,
                        help='NOT YET SUPPORTED standalone: exits with guidance.')
    parser.add_argument('--json', action='store_true', help='Emit machine-readable JSON.')
    args = parser.parse_args(argv)

    docs = Path(args.docs)
    if not docs.is_dir():
        raise SystemExit(f'Docs directory not found: {docs}')
    if args.web_query is not None:
        raise SystemExit(
            'Web research has no standalone provider yet (host search only). '
            'Run without --web-query for file-grounded answers.')

    try:
        transcript = load_transcript(args.transcript) if args.transcript else None
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Cannot read transcript: {exc}')
    if transcript is None:
        transcript = ''
    if args.project and args.all:
        raise SystemExit('Choose --project NAME or --all, not both.')
    project = args.project
    subs = sorted(sub.name for sub in docs.iterdir()
                    if sub.is_dir() and not sub.name.startswith('.') and not sub.is_symlink())
    if project is None and not args.all:
        if len(subs) == 1:
            project = subs[0]
        else:
            raise SystemExit('Pick one project with --project NAME (or --all): '
                             + (', '.join(subs) if subs else '(no project folders under --docs)'))
    if project is not None and project not in subs:
        raise SystemExit(f'No project {project!r} under {docs}.'
                         + (f' Available: {", ".join(subs)}.' if subs else ' No project folders found.'))
    if args.provider == 'fake':
        # Fake means fully offline: the engine would otherwise try the Meta
        # default route whenever a key happens to be configured.
        os.environ.pop('MODEL_API_KEY', None)
    started = time.monotonic()
    try:
        core = AnswerCore(project_root=docs, completion=build_completion(args.provider, args.model),
                          search=DisabledSearch(), history=NoHistory())
        result = core.answer(args.question, transcript=transcript, project=project,
                             all_projects=args.all)
    except (ValueError, OSError) as exc:
        raise SystemExit(f'Invalid request: {exc}')
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
    if args.json:
        print(json.dumps({**result, 'total_seconds': total}, indent=2, default=str))
        return 0
    print(result['answer'])
    print()
    print('Sources:')
    for i, source in enumerate(result['sources'], 1):
        title = source.get('title') or source.get('source') or 'source'
        print(f'  [{i}] {title} ({source.get("source", "")})')
        snippet = (source.get('text') or '').strip().replace('\n', ' ')
        if snippet:
            print(f'      {snippet[:200]}')
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
