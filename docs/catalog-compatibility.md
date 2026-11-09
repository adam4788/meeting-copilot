# Catalog compatibility

Validate the exact source and runtime export before any submission. A passing
validator does not establish live Desktop, device, or provider behavior.

The Desktop pane calls the backend through the supported SDK. OpenAI and
OpenRouter keys are normally configured in Hermes plugin settings and read
with the public `agent.secret_scope.get_secret` API only inside a host-bound
profile scope. The plugin checks the scope's home, uses strict scope semantics,
and rejects absent or mismatched scopes and missing keys; it does not borrow
the process environment. An admitted request may finish with its existing host
scope after a key change. Provider/model selection remains explicit.

The floating overlay is read-only. It retains no pane credential context and
cannot initiate answers, including when an older helper sends a click. Ask from
the Desktop pane. A relay was not enabled because the documented SDK does not
offer a way to revoke a request already in transit to a previous profile when
the user switches profiles.

The inspected Hermes v0.22.7 isolated plugin-host child did not receive the
parent's request-bound secret scope. BYOK therefore fails closed in that mode
and in secondary-profile routes served by that child. No isolation setting is
changed automatically. The actual running gateway mode and live provider
behavior were not verified. A host-provided request-scoped credential handoff
would be needed to support those paths.

Optional web search uses the public `tools.registry` only when a scoped
`web_search` tool is already registered. Otherwise the answer continues with
local evidence and reports that web search is unavailable. The optional
embedding adapter accepts an explicitly supplied `ctx.llm`; it is not used
for the fixed-origin BYOK route.

No Hermes core functions or registries are patched by the production plugin.
Development-only mutation probes remain in the source tree and are omitted
from the narrow runtime export, along with tests. The export retains the README
hero and records its source revision. See [development](development.md) for
the commands and review validator warnings separately.

Relevant upstream references: [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins),
[public API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/public-api),
[catalog rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission),
and [Desktop SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk).
