# Read-only knowledge engine

Author: Adam Nugroho

## Synchronous API

```python
from knowledge import KnowledgeEngine

engine = KnowledgeEngine(project_root=None, hermes_home=None)
projects = engine.projects()  # [{"name": ..., "path": ...}]
hits = engine.search("release approval", project="example-project", limit=6)
result = engine.answer("What approval is required?", transcript="", project="example-project",
                       answer_provider="openai", answer_model="EXACT_MODEL_ID",
                       include_history=False)
# Only after explicit user consent for this exact query:
# result = engine.answer("...", project="example-project", web_query="public product documentation",
#                        answer_provider="openai", answer_model="EXACT_MODEL_ID")
```

`search` returns dictionaries with `id`, `title`, `source`, `text`, and `updated_at`
(Unix seconds, or `None` for web snippets). `answer` returns `answer`, `sources`,
`elapsed_seconds`, and `warnings`. Sources are numbered in list order for `[1]`
citations. A source is evidence supplied to the model, not proof that every
answer statement is supported. Missing or out-of-range numeric citations and
transcript references without a matching snapshot produce warnings, including
on interrupted partial streams. An answer without retrieved evidence is also
marked; a syntactically valid citation does not verify its claim.
Offload calls to a worker thread; no autonomous agent is started.

Project root discovery: explicit argument, `MEETING_COPILOT_PROJECT_ROOT`, then
`~/Documents/Projects`, falling back to `~/Projects` when the former is absent
and the latter exists. Hermes home discovery: explicit argument, `HERMES_HOME`,
then `%LOCALAPPDATA%/hermes` on Windows or `~/.hermes` elsewhere. Only the default
profile is supported. Project selection must resolve to one direct child of the
project root. With no selected project, only prior sessions are searched unless
`all_projects=True` is explicitly enabled.

## Retrieval and limits

File formats: UTF-8 Markdown (`.md`), plain text (`.txt`), and reStructuredText
(`.rst`), including README files. PDF, DOCX and XLSX extraction is not implemented.
Files are bounded to 256 KB; at most the first 32,000 characters of each file,
600 chunks, 3,000 examined files and eight directory levels per selected project.
Chunks are 2,000 characters. Hidden directories/files, dependency/build trees,
symlinks and auth/config/credential/secret/password/token filenames are excluded.
File contents are cached for 15 seconds (at most 64 cached project entries,
each holding up to 600 chunks; scans cycling more projects than that re-read
within the TTL), so edits can take that long to appear.
An in-memory SQLite FTS5/BM25 index ranks cached chunks and session candidates.
The Python SQLite build must include FTS5. No project index is persisted.
Indexes are cached per engine (LRU, bounded by entry count and total indexed
characters, not by RSS); each lookup fingerprints the corpus with SHA-256, so
warm cost is linear in corpus size, a cyclic working set larger than the cache
rebuilds, ranking is serialized per engine, and corpora above the character
budget are queried uncached. These are deliberate simplicity/correctness
tradeoffs, not latency guarantees.

Sessions use `state.db` through a read-only SQLite URI and `query_only=ON`.
The engine discovers table/column schema before querying. FTS selects at most
100 candidates; without session FTS it examines at most the latest 1,000
message IDs. A 0.4-second SQL execution budget and 0.2-second lock timeout bound
reads. Missing/incompatible/locked databases fail closed to no session results.
Only plain user/assistant content is eligible; tool messages, messages carrying
tool-call payloads, structured JSON blocks and non-default-profile sessions are
excluded. Inactive/hidden entries are excluded where those schema fields exist.
Prior assistant statements are explicitly labeled **unverified**; neither an
assistant claim nor a user request proves an action happened.

## Scope and web fallback

`all_projects=True` searches every eligible direct child project (max 100) plus
sessions, ranking the full eligible corpus in one FTS index; above 5,000 chunks
it falls back to per-project FTS candidate pools (a bounded approximation). It
cannot be combined with `project`.
`web_fallback=True` runs `web_policy.plan_web` after local retrieval:
one model call decides whether local evidence suffices and, if not, drafts a generic
public query; unsafe/invalid output or errors mean no search. Both the synchronous
engine API and the server `AssistRequest` default `web_fallback` to `False`
(the pane leaves automatic fallback unchecked); pass it explicitly to enable.
An explicit `web_query` always takes precedence.
Results include `web_search: {used, mode, query}`.

## Privacy and online boundary

Local file/session retrieval runs before any optional web request. No query is
built by string-copying the question, transcript or evidence; with fallback
enabled the model drafts a filtered generic query (best effort, see Scope above).
An explicit nonblank `web_query` is passed to the registered public Hermes
`web_search` tool with a limit of three, in the authenticated profile scope.
That field must come from an explicit user action, never from a model. If the
host has not registered the tool or does not supply a bound profile scope, the
answer continues with local evidence and reports that web search is unavailable.
Web sources are search descriptions, not downloaded or verified pages. No web
extraction or link following occurs.

Answer generation requires an explicit provider and exact model ID, or an
injected standalone completion function. The Desktop uses scoped BYOK settings
for OpenAI/OpenRouter; optional Meta routing is explicit. The former generic
Hermes configured-model fallback is unavailable. OpenAI/OpenRouter requests have
a 30-second timeout and 2048-token output limit, without automatic retry or
provider/model fallback. See [BYOK](byok.md) for account compatibility and
credential boundaries. The selected question (2,000 characters), recent
transcript (6,000 characters), and up to nine selected evidence snippets (2,050
characters each for local/session evidence, 1,500 for web; six local/session plus
three web) may leave the machine for
the configured hosted model. Only titles/text/citation numbers are sent, not
absolute source paths. The caller must disclose this before enabling answers.
`elapsed_seconds` measures actual wall time, including retrieval and setup; it
is not a latency promise. Parent UI should run background jobs and show status.

Common key/password/token patterns and PEM private keys are redacted. This is
best-effort, not a complete DLP guarantee: select only approved projects and do
not store secrets in searchable notes. Retrieval evidence and meeting transcripts
are untrusted data, not model instructions. Prompt injection remains a model
risk, but the model has no tools or action permissions. No plugins/config are
enabled or changed by this module. Hermes itself may update its normal runtime
caches/auth/logs when its host integration or web client is used.

## Tests

```sh
python -m unittest tests.test_knowledge -v
# Run pure retrieval tests without site-packages / installed Hermes:
python -S -m unittest tests.test_knowledge -v
```

Tests use temporary synthetic documents and SQLite databases; model/web methods
are injected with `unittest.mock`. They make no network requests. Optional live
verification should use approved local sources and the installed Hermes Python
runtime. Keep real session excerpts, client notes and integration output outside
the repository. Model failures return retrieved sources and a warning without
leaking exception text; missing evidence is never replaced with invented facts.

## Hermes integration references

- Official architecture: https://hermes-agent.nousresearch.com/docs/developer-guide/architecture/
- The public tool registry dispatches `web_search` with a JSON result containing
  `success` and `data.web` metadata when the host has registered the tool.
- The optional public-facade embedding helpers and host-mode credential limits
  are described in [compatibility review](catalog-compatibility.md).

Retrieval tests are independent of live Desktop/web integration, which requires an installed, configured
Hermes environment. No additional pip packages are needed for this
module's deterministic tests or text/session retrieval.
