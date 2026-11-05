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

- `knowledge.py` and the optional `hermes_adapters.default_search()` call
  `tools.web_tools.web_search_tool`. The official Web Search Provider guide
  describes this dispatcher, but the catalog scanner warns about the module
  import. Explicit web opt-in, public-query filtering and failure reporting
  remain in place. New users default to web off.
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

No core functions or registries are patched by the production plugin. An
upstream public fresh-copy authenticated scope API would let the private seam
be removed; no issue or PR has been submitted by this work.

Development-only scan cautions also remain visible. `verify_overlay_eof.py`
compiles and executes locally inspected method source for in-memory mutation
checks, and patches objects only inside those isolated tests. It is not copied
by `install.py` or imported by runtime entrypoints. The explicitly obsolete
`real_effect_test_backend.py`/`test_real_effects.cjs` fixtures describe the retired
credential editor and are not current validation evidence or installed files.
Their old private imports are not runtime integrations. These files remain
reviewable source; they were not relocated to hide scanner warnings.

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
