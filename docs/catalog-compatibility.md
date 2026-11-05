# Catalog compatibility review

The catalog validator reports warnings as well as failures. A passing run does
not mean this plugin has no version-sensitive dependencies. Review the exact
pinned commit and its complete validator output before submission.

The optional `hermes_adapters.default_completion(llm)` helper now accepts only an
explicitly supplied public `ctx.llm` facade. It calls the documented synchronous
`complete(messages=..., max_tokens=..., timeout=..., purpose=...)` API and reads
`result.text`. It does not discover a client, extract credentials, or import
`agent.auxiliary_client`. This embedding helper follows the host's model and
fallback policy; the fixed-origin BYOK answer path does not use it.

Two active compatibility seams remain:

- `knowledge.py` still calls `tools.web_tools.web_search_tool` for explicit web
  opt-in. The optional embedding adapter `hermes_adapters.default_search()` now
  requires an explicitly supplied public `ctx.dispatch_tool` and dispatches only
  `web_search` with the exact query and a limit of three. It performs no context
  discovery, retries, LLM calls or history searches. The current dashboard does
  not use this adapter: its separately loaded API module exports `router`, and
  the reviewed public dashboard contract supplies no `ctx` argument or factory.
  Capturing an arbitrary plugin manager/context or using a global default would
  lack a verified per-request profile/approval binding. The missing surface is
  a documented profile-bound tool-dispatch handle available to dashboard route
  handlers, with supported lifetime/context propagation to answer workers.
  Public `ctx.dispatch_tool` exists for registered callbacks; this is specifically
  a dashboard handoff gap, not a claim that Hermes lacks tool dispatch.
  Explicit web opt-in, query filtering and failure reporting are unchanged.
- `overlay_bridge.BackendTransport._fresh_action_context()` uses the private
  `hermes_cli.web_server_profiles._config_profile_scope('current')` helper.
  It refreshes a **copy** of the authenticated pane action context, verifies
  profile-home identity, and captures a fresh secret snapshot for subsequent
  actions. Already admitted answer workers retain their own snapshots.
  Missing/incompatible host APIs fail closed with reopen guidance. This private
  helper can change between Hermes versions. There is no documented equivalent
  public fresh-copy scope API in the reviewed docs. The host's
  `refresh_installed_secret_scope` mutates the installed mapping in place and
  would change existing workers' snapshots, so it is not a safe replacement.

The needed scope API must refresh host-owned secret sources into a **new**
authenticated-profile snapshot inside the action admission boundary, preserve
launch/multiplex policy, and leave admitted worker contexts unchanged. A shallow
`contextvars.copy_context()` alone shares the old mapping; `get_secret` alone
reads that frozen mapping. Neither establishes freshness after an admission wait.
Copying all profile credentials or importing another private builder is not the
replacement. No credential values were inspected to evaluate these contracts.

The documented `session_search` tool returns actual FTS-backed messages without
an LLM call. It is not a replacement for web dispatch, and changing history to
use it would require the same authenticated dashboard dispatch handoff plus
equivalence checks for current filtering, labels and request-local exclusion.
History behavior and access were not widened in this work.

No core functions or registries are patched by the production plugin. An
upstream public fresh-copy authenticated scope API would let the private seam
be removed; no issue or PR has been submitted by this work.

Development-only scan cautions remain visible in the full checkout.
`tests/verify_overlay_eof.py` compiles and executes locally inspected method
source for in-memory mutation checks. It is neither installed nor imported by
runtime entrypoints. The retired credential-editor harness is available in Git
history; it is not current validation evidence.

`scripts/export_catalog_runtime.py` creates a distinct allowlisted runtime tree
outside the checkout, preserving runtime bytes and the exact README hero asset.
Tests and probes are omitted as non-runtime dependencies; no scanner exclusions
are added. `SOURCE_REVISION` ties the artifact to the complete development
commit. Validate both the full source and actual export, reporting warnings
separately. A catalog pin must identify the release tree actually validated.

Focused regression coverage includes the public facade's exact call signature,
no implicit host lookup, sanitized failures and no retries; actual synthetic
host-scope snapshots across worker contexts; overlay context replacement and
fail-closed refresh; plus the existing cancellation, ownership, retirement and
stale-publication suites.

Primary documentation reviewed:

- [Catalog admission rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
- [Public Plugin LLM API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access)
- [Web search dispatcher](https://hermes-agent.nousresearch.com/docs/developer-guide/web-search-provider-plugin)
- [Desktop SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk)
- [Public tool dispatch](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [Session search](https://hermes-agent.nousresearch.com/docs/reference/tools-reference)
