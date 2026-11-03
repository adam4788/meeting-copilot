"""Synthetic retrieval benchmark for the standalone core.

Builds a deterministic temporary corpus, then reports recall@k and latency
percentiles for evidence search plus fake-provider answer latency. This is
a regression/evidence tool, not a production accuracy claim: the corpus is
synthetic and the completion provider is fake.

Run: python eval_retrieval.py [--projects 12] [--docs-per-project 5]
     [--runs 20] [--min-recall 1.0] [--json]
"""

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

try:
    from answer_core import AnswerCore, DisabledSearch, NoHistory
except ImportError:
    from answer_core import AnswerCore, DisabledSearch, NoHistory

TOPICS = (
    ('pricing', 'unit price annual minimum quote'),
    ('renewal', 'contract renewal september finance approval'),
    ('shipping', 'delivery window warehouse freight schedule'),
    ('support', 'support response outage incident escalation'),
    ('hiring', 'candidate interview panel rubric decision'),
    ('security', 'access review audit credential rotation'),
)


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[index]


def build_corpus(root, projects=12, docs_per_project=5):
    queries = []
    for p in range(projects):
        project = root / f'project{p:02d}'
        project.mkdir(parents=True)
        for d in range(docs_per_project):
            topic, words = TOPICS[(p + d) % len(TOPICS)]
            marker = f'zqx{p}w{d}v'
            (project / f'{topic}-{d}.md').write_text(
                f'# {topic} note {d}\n\n{words}. Reference marker {marker}. '
                f'Project {p} document {d} discusses {topic} in ordinary prose.\n',
                encoding='utf-8')
            if d == 0:
                queries.append({'query': f'{words} {marker}',
                                'topic_query': words,
                                'topic': topic,
                                'project': project.name,
                                'expected': f'{topic}-{d}.md'})
    return queries


def run_benchmark(projects=12, docs_per_project=5, runs=20, limit=6):
    root = Path(tempfile.mkdtemp(prefix='retrieval-eval-'))
    try:
        queries = build_corpus(root, projects, docs_per_project)
        core = AnswerCore(project_root=root,
                          completion=lambda messages: 'Benchmark answer [1].',
                          search=DisabledSearch(), history=NoHistory())
        hits = 0
        topic_hits = 0
        search_latencies = []
        for query in queries:
            started = time.perf_counter()
            found = core.search(query['query'], limit=limit, all_projects=True)
            search_latencies.append((time.perf_counter() - started) * 1000)
            if any(f"/{query['project']}/{query['expected']}" in source.get('source', '')
                   for source in found):
                hits += 1
            topic_found = core.search(query['topic_query'], limit=limit, all_projects=True)
            if any(f"/{query['topic']}-" in source.get('source', '') for source in topic_found):
                topic_hits += 1
        answer_latencies = []
        for _ in range(max(1, runs)):
            started = time.perf_counter()
            core.answer(queries[0]['query'], transcript='benchmark transcript',
                        project=queries[0]['project'])
            answer_latencies.append((time.perf_counter() - started) * 1000)
        recall = hits / len(queries) if queries else 0.0
        topic_recall = topic_hits / len(queries) if queries else 0.0
        return {
            'corpus': {'projects': projects, 'docs_per_project': docs_per_project,
                       'documents': projects * docs_per_project, 'queries': len(queries)},
            'recall_at_k': {'k': limit, 'exact_reference': round(recall, 4),
                            'exact_hits': hits,
                            'natural_topic': round(topic_recall, 4),
                            'topic_hits': topic_hits},
            'search_latency_ms': _summary(search_latencies),
            'answer_latency_ms_fake_provider': _summary(answer_latencies),
            'note': 'Synthetic corpus + fake completion; not a production accuracy or latency claim.',
        }
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def _summary(values):
    return {'count': len(values), 'mean': round(statistics.fmean(values), 3),
            'p50': round(percentile(values, 50), 3),
            'p95': round(percentile(values, 95), 3),
            'max': round(max(values), 3) if values else 0.0}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Synthetic standalone retrieval benchmark.')
    parser.add_argument('--projects', type=int, default=12)
    parser.add_argument('--docs-per-project', type=int, default=5)
    parser.add_argument('--runs', type=int, default=20, help='Fake-answer latency repetitions.')
    parser.add_argument('--limit', type=int, default=6)
    parser.add_argument('--min-recall', type=float, default=1.0)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = run_benchmark(args.projects, args.docs_per_project, args.runs, args.limit)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        corpus = report['corpus']
        recall = report['recall_at_k']
        print(f"Corpus: {corpus['documents']} docs in {corpus['projects']} projects, {corpus['queries']} queries")
        print(f"Recall@{recall['k']} exact-reference: {recall['exact_reference']:.2%} ({recall['exact_hits']}/{corpus['queries']})")
        print(f"Recall@{recall['k']} natural-topic: {recall['natural_topic']:.2%} ({recall['topic_hits']}/{corpus['queries']})")
        for key in ('search_latency_ms', 'answer_latency_ms_fake_provider'):
            item = report[key]
            print(f"{key}: mean {item['mean']} ms, p50 {item['p50']} ms, p95 {item['p95']} ms, max {item['max']} ms")
        print(report['note'])
    return 0 if report['recall_at_k']['exact_reference'] >= args.min_recall else 1


if __name__ == '__main__':
    sys.exit(main())
