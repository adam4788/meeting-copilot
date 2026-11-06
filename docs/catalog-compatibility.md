# Catalog compatibility review

The catalog validator reports warnings as well as failures. A passing run does
not mean this plugin has no version-sensitive dependencies. Review the exact
pinned commit and its complete validator output before submission.

## Public API audit (2026-11-06)

The [current public API list](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/public-api)
does not include the private scope import below. The catalog validator warns
without failing, but the [catalog rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
require public extension surfaces. A passing validator is therefore not a claim
of full public-API conformance or catalog readiness.

The optional `hermes_adapters.default_completion(llm)` helper now accepts only an
explicitly supplied public `ctx.llm` facade. It calls the documented synchronous
`complete(messages=..., max_tokens=..., timeout=..., purpose=...)` API and reads
`result.text`. It does not discover a client, extract credentials, or import
`agent.auxiliary_client`. This embedding helper follows the host's model and
fallback policy; the fixed-origin BYOK answer path does not use it.

Dashboard web search now uses the public `tools.registry` for an already
registered `web_search` tool, with an explicit active-profile scope and a
matching bound secret scope. It never imports a tool implementation to populate
the registry or retries through a private path. If the registry, tool or bound
scope is absent, the answer continues with local evidence and reports that web
search is unavailable. The current dashboard API supplies no `ctx`, so it does
not provide the parent-agent context of `ctx.dispatch_tool`. A fresh dashboard
probe against the inspected host found no registered search tool; the optional
web feature may therefore be unavailable even when a provider is configured.
The separate embedding adapter still accepts an explicit `ctx.dispatch_tool`.

`overlay_bridge.BackendTransport._fresh_action_context()` uses the private
`hermes_cli.web_server_profiles._config_profile_scope('current')` helper. Show
and explicit scope changes refresh a **copy** of the authenticated pane action
context and verify profile-home identity. **Overlay clicks currently reuse that
retained context without refreshing it.** A synthetic transport probe confirmed
that clicks after a key rotation or deletion can use the old key until the pane
action is refreshed. Treat this as an unresolved credential-freshness defect
and avoid overlay answers until it is fixed.
Missing/incompatible host APIs fail closed with reopen guidance. This private
helper can change between Hermes versions. There is no documented equivalent
public fresh-copy scope API in the reviewed docs. The host's
`refresh_installed_secret_scope` mutates its installed mapping in place, so
calling it on a shared context could change admitted workers' snapshots.
`build_profile_secret_scope(home)` does return a fresh file/external/managed
mapping, and rebinding that mapping avoids mutating the original. However,
it does not itself perform the host's external-source hydration or use its
launch-profile resolver. In the inspected host, `_config_profile_scope` does
both where applicable. A synthetic comparison against the installed host
showed a launch-injected key in the host scope but not the direct builder's
mapping. Equivalence for injected credentials and routed profiles is therefore
unproven, so neither approach is a safe replacement yet.

BYOK reads require a host-stamped scope whose home matches the active request.
They snapshot only the requested key before calling the public `get_secret` API and reject a
missing key instead of accepting Hermes's single-profile environment fallback.
Launch-injected keys remain usable when the host includes them in its scope.
An absent, unstamped, or mismatched scope fails closed.

The inspected in-process dashboard route binds a host scope at request entry.
Defensively copying that scope with the public `set_secret_scope` API preserves
launch-injected, external and managed values already present and separates old
workers from new requests. It does not refresh after an admission wait: an
offline test bound a synthetic key, delayed the request, revoked the key, and
found that a public copy still contained it. Directly invoking the private
refresh helper saw the deletion, but the actual click path does not call that
helper at admission. The plugin no longer has a credential-edit route; its retained legacy
change callback is not reached by `credentials.make_router`.

The inspected isolated plugin-host child serves the forwarded dashboard router
without transferring the parent's request-bound secret context. An offline probe
of that host child route reported BYOK unavailable even with a synthetic launch
value. The plugin intentionally does not fall back to process-environment keys.
This also affects secondary-profile routes that Hermes forwards to a plugin host.
An upstream request-scoped credential handoff is needed for those routes; live
Desktop/provider behavior has not been verified. The public
`credentialed_provider_request` handles declared OAuth/Codex sign-ins, not
these OpenAI/OpenRouter API-key routes, so it is not a drop-in BYOK replacement.

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
