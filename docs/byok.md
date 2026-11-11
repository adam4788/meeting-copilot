# Bring your own API key

New panes default to **Follow Hermes default**, using the genuine host-provided
LLM facade in supported, matching-profile nonisolated runtimes. Existing explicit
provider choices are preserved. Hermes owns default-route credentials and its
routing/fallback policy. An unavailable default route fails closed; Copilot does
not borrow credentials or silently choose a BYOK provider. See
[default-route requirements](preflight.md#follow-the-hermes-default).

For explicit overrides, Meeting Copilot supports OpenAI and OpenRouter Chat Completions routes.
Configure the corresponding API key in **Hermes Settings → Plugins → meeting-copilot-live (default profile)**. Use the OpenAI API key or OpenRouter API key field, then **Save settings**. These settings are declared secret fields in `plugin.yaml`; the
plugin obtains them only through `agent.secret_scope.get_secret` in a bound
Hermes scope. An unavailable scope fails closed. Reopen the pane after changing
settings. Never paste a key into chat, a model field, or an answer request.

The inspected Hermes isolated plugin-host child route did not receive its
parent's request-bound scope in an offline synthetic probe. BYOK may therefore
be unavailable in host-isolation or secondary-profile routes; it does not fall
back to the child process environment. Live Desktop/provider behavior remains
unverified. See [compatibility](catalog-compatibility.md).

Choose a configured provider, explicitly **Load model options…**, and choose an
option or enter its exact model ID manually. OpenAI lists models returned for
your key; OpenRouter returns a public catalog, not account entitlements. Model
access, credits and Chat Completions compatibility remain unverified. A configured
key is not a successful connection test. Model listing sends only that provider
its scoped key, never meeting content, and never runs automatically. The app does
not pick models, follow redirects, retry automatically or change providers on
failure. See [checks and model options](preflight.md).
Responses are completed-only; empty, truncated or non-text output fails. Each
answer has a 30-second request timeout and a 2048-token output limit. Some models
need a different API or parameters and are unsupported. No live OpenAI or
OpenRouter account/model has been verified by the synthetic tests.

New panes default to one project, past sessions excluded, and web off. Choose a
project before asking. Saved web and history choices remain intentional user
choices. History exclusion requires one project with web off. The selected
project excerpts and current meeting context are sent to your chosen provider
when you ask. Review consent before capture. Local faster-whisper transcription
is independent of the answer key. Pause/stop and cancellation prevent subsequent
submissions; a provider request already sent cannot be recalled. Retry explicitly
resends the saved question and context.

For an offline demonstration use `python ask.py --docs sample-docs --provider
fake ...`. The standalone CLI does not support live providers or read API keys;
configure Hermes Settings and ask in the Desktop pane for a live answer.
`--doctor` checks local setup only.

Meta remains an optional explicit API route for existing integrations. Its
`MODEL_API_KEY` is also Hermes-managed in plugin mode; it is never selected
automatically. Direct OAuth subscription credential access and implicit fallback from BYOK to
Hermes are deliberately unavailable. The explicit **Follow Hermes default** mode
uses only host-owned auth and host routing when supported. This replaces the old
Meta-first default, client credential extraction, startup dotenv import, and
in-pane credential editor. Legacy tests for those removed contracts are not
claims about current behavior. `test_byok.py` exercises actual HTTP against a
synthetic localhost vendor, including exact model/header submission, source
context, empty/truncated output, HTTP failure and no retry.

Native local transcription now retains speech across the former fixed four-second
cut. It flushes after at least four seconds when the trailing 400ms is quiet
(RMS below 0.008), or at twelve seconds of buffered audio, plus at most one input
frame. This adds latency for continuous speech but keeps finite buffers and the
existing bounded queue/loss reporting. This energy heuristic may need microphone
gain adjustment and cannot guarantee word boundaries in noise. The synthetic
PCM/real tiny.en inference comparison retained the price, annual minimum,
Monday, Carol and finance facts that the fixed-cut baseline split. OS microphone
and system-audio capture still need an explicit consented device check.

Subsequent fictional native checks exercised system audio and microphone input,
but the microphone run reported native backpressure. See the
[loss investigation and remaining limits](native-backpressure.md). The bounded
silence-window optimization does not establish loss-free device capture.
