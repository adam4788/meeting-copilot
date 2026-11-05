# Bring your own API key

Meeting Copilot supports explicit OpenAI and OpenRouter Chat Completions routes.
Configure the corresponding API key in **Hermes Settings → Plugins → meeting-copilot-live (default profile)**. Use the OpenAI API key or OpenRouter API key field, then **Save settings**. These settings are declared secret fields in `plugin.yaml`; the
plugin obtains them only through `agent.secret_scope.get_secret` in a bound
Hermes scope. An unavailable scope fails closed. Reopen the pane after changing
settings. Never paste a key into chat, a model field, or an answer request.

Choose your provider and enter its exact model ID. Model access, credits and
Chat Completions compatibility depend on your account. A configured key is not
a successful connection test. The app does not infer models, discover account
models, follow redirects, retry automatically, or change providers on failure.
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
fake ...`. For standalone BYOK, set `OPENAI_API_KEY` or `OPENROUTER_API_KEY` in the
calling process environment and use `--provider openai|openrouter --model EXACT_ID`.
Only this explicit standalone CLI path reads process credentials. It does not
read or create credential files. `--doctor` checks local setup only.

Meta remains an optional explicit API route for existing integrations. Its
`MODEL_API_KEY` is also Hermes-managed in plugin mode; it is never selected
automatically. OAuth subscription routes and generic Hermes configured-model
fallback are deliberately unavailable in this foundation. This replaces the old
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
