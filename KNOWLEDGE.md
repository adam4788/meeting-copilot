# Read-only knowledge engine

Author: Adam Nugroho

## Synchronous API

```python
from knowledge import KnowledgeEngine

engine = KnowledgeEngine(project_root=None, hermes_home=None)
projects = engine.projects()  # [{"name": ..., "path": ...}]
hits = engine.search("release approval", project="example-project", limit=6)
result = engine.answer("What approval is required?", transcript="", project="example-project")
# Only after explicit user consent for this exact query:
# result = engine.answer("...", project="example-project", web_query="public product documentation")
```

`search` returns dictionaries with `id`, `title`, `source`, `text`, and `updated_at`
(Unix seconds, or `None` for web snippets). `answer` returns `answer`, `sources`,
`elapsed_seconds`, and `warnings`. Sources are numbered in list order for `[1]`
citations. A source is evidence supplied to the model, not proof that every
answer statement is supported. Invalid/missing citations produce a warning.
Offload calls to a worker thread; no autonomous agent is started.

Project root discovery: explicit argument, `MEETING_COPILOT_PROJECT_ROOT`, then
`~/Documents/Projects`. Hermes home discovery: explicit argument, `HERMES_HOME`,
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
File contents are cached for 15 seconds, so edits can take that long to appear.
An in-memory SQLite FTS5/BM25 index ranks cached chunks and session candidates.
The Python SQLite build must include FTS5. No project index is persisted.

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

`all_projects=True` searches every eligible direct child project (max 100, per-project
top-`limit` preselection) plus sessions; it cannot be combined with `project`.
`web_fallback=True` (the server default) runs `web_policy.plan_web` after local retrieval:
one model call decides whether local evidence suffices and, if not, drafts a generic
public query; unsafe/invalid output or errors mean no search. The synchronous
engine API keeps `web_fallback=False` unless explicitly enabled; the desktop/API
defaults it to true. An explicit `web_query`
always takes precedence. Results include `web_search: {used, mode, query}`.

## Privacy and online boundary

Local file/session retrieval runs before any optional web request. No query is
built by string-copying the question, transcript or evidence; with fallback
enabled the model drafts a filtered generic query (best effort, see Scope above).
An explicit nonblank `web_query` is passed, unchanged, to Hermes
`tools.web_tools.web_search_tool(query, limit=3)`. That field must come from an
explicit user action, never from a model. Web sources are search descriptions,
not downloaded or verified pages. No web extraction or link following occurs.

Answer generation is **not necessarily offline**: it reuses the configured
Hermes model/auth through `agent.auxiliary_client.call_llm`, task
`meeting_copilot`, with no tools, 600 output tokens, low reasoning effort and
a requested 30-second timeout. The existing task override is honored if present;
otherwise Hermes chooses its configured route. No credentials are copied,
printed, or set by this module. The selected question (2,000 characters), recent
transcript (6,000 characters), and up to nine selected evidence snippets (2,050
characters each for local/session evidence, 1,500 for web; six local/session plus
three web) may leave the machine for
the configured hosted model. Only titles/text/citation numbers are sent, not
absolute source paths. The caller must disclose this before enabling answers.
Hermes/provider retries, auth refresh, initialization and queue waits can exceed
the requested timeout; `elapsed_seconds` measures the actual wall time, not a
latency promise. Parent UI should run background jobs and show elapsed status.

Common key/password/token patterns and PEM private keys are redacted. This is
best-effort, not a complete DLP guarantee: select only approved projects and do
not store secrets in searchable notes. Retrieval evidence and meeting transcripts
are untrusted data, not model instructions. Prompt injection remains a model
risk, but the model has no tools or action permissions. No plugins/config are
enabled or changed by this module. Hermes itself may update its normal runtime
caches/auth/logs when its configured model/web client is used.

## Tests

```sh
python -m unittest test_knowledge -v
# Run pure retrieval tests without site-packages / installed Hermes:
python -S -m unittest test_knowledge -v
```

Tests use temporary synthetic documents and SQLite databases; model/web methods
are injected with `unittest.mock`. They make no network requests. Optional live
verification should use approved local sources and the installed Hermes Python
runtime. Keep real session excerpts, client notes and integration output outside
the repository. Model failures return retrieved sources and a warning without
leaking exception text; missing evidence is never replaced with invented facts.

## Hermes integration references

- Official architecture: https://hermes-agent.nousresearch.com/docs/developer-guide/architecture/
- Installed `agent/auxiliary_client.py`: synchronous `call_llm(task=..., messages=...,
  tools=None, max_tokens=..., timeout=..., reasoning_config=...)` handles configured
  provider/auth routing, including provider-specific response normalization.
- Installed `tools/web_tools.py`: synchronous `web_search_tool(query, limit)` returns
  a JSON string with `success` and `data.web` metadata.
- `hermes_cli/runtime_provider.py` exposes `resolve_runtime_provider`; this engine
  deliberately delegates routing to the auxiliary client instead of resolving,
  duplicating or caching provider credentials itself.

These are internal Hermes APIs and may change between releases; retrieval tests
are independent of them, while live model/web integration requires an installed,
configured Hermes environment. No additional pip packages are needed for this
module's deterministic tests or text/session retrieval.
