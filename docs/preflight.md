# Capture checks and answer options

Capture and answers have separate requirements. A missing answer key, answer
model or project does **not** block local transcription. Capture never starts
from a check, permission setup, model listing, settings change or model preparation.

## Before Start

The pane checks the current source, selected audio inputs, transcription model
and provider, participant permission, and stopped state. Native macOS checks
also verify the prepared version's helper/interpreter, source-specific OS
permissions, default microphone/active display metadata, installed audio
libraries and locally cached Whisper weights. Meta transcription checks its
scoped key and libraries without sending audio or testing account/network access.
Meetily checks that the selected recording is active and readable.

Each row explains what needs attention. Start is disabled if a required check
fails, is unknown, is missing, belongs to earlier settings or is older than
15 seconds. Use **Recheck required settings** after fixing setup or if the checks
expire. Permission consent must be reconfirmed after capture settings change.
The backend independently checks again immediately before every Start, including
API requests; an earlier green UI result cannot bypass it.

Checks use no recording or live provider request. They do not prove device
quality, future access, network service, loss-free capture or transcription
accuracy. Hardware and permissions may still change after a check. Read the
capture status after Start and Pause.

### First-use macOS permissions

Choose the audio source and press **Request macOS permissions…**. Confirm the
separate disclosure, then review macOS's own prompts yourself. This starts only
the helper's permission setup command, not its capture command. Denied or
restricted access may need **System Settings → Privacy & Security** and an app
restart. A completed setup request does not mean permission was granted: recheck.
If setup times out, finish or dismiss any outstanding OS prompt before retrying.

Rebuild the prepared helper after updating this version. An older helper cannot
provide this permission-check contract and therefore blocks Start. See
[macOS preparation](getting-started.md#prepare-a-transcript-source).

### Prepare a missing local model

After preparing the macOS audio runtime, open a terminal in the installed plugin
runtime directory. This is the plugin root for catalog installs, or
`dashboard/core` inside the plugin for a legacy installation. Run the exact
command shown by the missing-model row, for example:

```sh
python -B capture_preflight.py --prepare-model base.en
```

Use `tiny.en`, `base.en` or `small.en` to match your selected model. This explicit
command may download public Whisper weights from Hugging Face into the audio
runtime's normal cache. It does not install software, record, request OS
permission, transcribe, or send meeting content. It uses the prepared interpreter
and strips unrelated credentials; implicit cached Hugging Face token use is off.
If you intentionally configured offline cache settings, preparation respects them.
Reopen the pane and **Recheck required settings** after it finishes. The readiness
check itself uses offline-only model loading and never downloads missing files.

### Windows

Native Windows capture is now fail-closed: this version cannot safely verify all
required Windows device and privacy access without capture. Installing libraries
alone does not enable Start. Use an accessible active Meetily recording until a
supported Windows readiness probe is available. This is a deliberate limitation,
not a successful native Windows verification.

## Follow the Hermes default

New preferences default to **Follow Hermes default**. Existing explicit OpenAI
or OpenRouter choices are preserved. In this mode Copilot calls the genuine
host-provided `ctx.llm` facade without provider, model, task or auth overrides.
Hermes selects its current main model, owns credentials and applies its own
routing/fallback policy. Copilot does not copy or read core credentials. There is
no separate plugin answer key to set. No LLM request occurs while checking setup.

The facade must have been supplied during this plugin's real registration in
the same runtime and profile, and the answer request must have a matching
host-stamped scope. The current detached-answer architecture does not support
an isolated remote facade: its originating RPC context can expire after the
initial request returns. Missing registration, unsupported host, isolated mode
or mismatched scope fails closed with an actionable message. Enable the plugin
on the Agent side and reopen the pane; if this host still cannot provide the
route, choose an explicit OpenAI/OpenRouter override. No fallback is made by
Copilot from an unavailable Hermes route to a different provider.

This mode follows Hermes changes at call time, including a later explicit
**Retry**; it does not pin the model originally used by a failed answer. The
answer shows the safe provider/model attribution returned by Hermes when
available, plus a reminder of the host-routing policy. No specific OAuth,
custom provider, account or live model has been verified by these tests.

## Answer model options

Choose a provider with a configured plugin key, then press **Load model options…**
and confirm the disclosure. Only this explicit action contacts the selected
provider's fixed HTTPS listing endpoint with its scoped saved key. No question,
transcript, project data or unrelated credential is included. Opening Settings,
refreshing readiness or starting capture does not discover remote models.

- **OpenAI:** IDs come from the list returned for that key. Conservative chat-family
  filtering hides known non-text routes. This is not proof of Chat Completions
  compatibility, usable credits, or successful answer access.
- **OpenRouter:** IDs come from its public model catalog, filtered for text output
  when modality metadata is supplied. This is **not** an account entitlement list;
  account access, provider preferences, credits and compatibility remain unverified.

Choose a listed option explicitly or enter an exact compatible model ID manually.
Loading does not pick a model, replace a saved choice, change credentials or fall
back to another provider. Empty/failed discovery leaves manual entry available.
Options are pane-local, not persisted as account facts. Provider/model changes,
pane closure and profile/connection changes discard deferred responses.

Listing rejects redirects and oversized payloads, returns sanitized errors, and
bounds the UI wait to ten seconds. A timed-out request may still finish in its
single bounded worker; no automatic retry is made. Another listing is rejected
while that worker remains outstanding. Reopen the backend only if a request never
finishes and no other work is active.

The inspected Hermes model-options route can merge curated fallbacks, make
additional requests and update caches, so it is not reused as authoritative BYOK
account availability. The plugin retains strict host-issued profile scope and
fails closed when it is absent. See [BYOK](byok.md) and
[compatibility](catalog-compatibility.md).

## Verification limits

Regression tests use synthetic permission, device, model and provider fixtures.
Swift validation is compile/static-only. No live permission prompts, recording,
provider requests, model downloads, installation or publication were performed
for this change. Live account access and sustained capture still need separate
explicit testing.

Hermes route attribution is host-reported metadata, not independent proof of the
provider that served a fallback. The host may report its current configured main
route. No private host routing internals are inspected.
