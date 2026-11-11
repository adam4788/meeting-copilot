# Security and privacy

## Data flow

- Microphone and loopback audio are processed locally after explicit consent when using native capture with local transcription. Optional explicit Meta transcription sends audio off-device; local model preparation may download weights.
- The optional, unofficial Meetily adapter reads only an explicitly attached active recording's live JSON snapshots. Meetily independently saves audio/transcripts according to its own settings; this project does not erase or change those files.
- This project does not write additional raw audio or transcripts to disk. Transcripts and
  answer results remain in bounded process memory until cleared or the process exits.
- A configured answer provider receives the question, bounded recent transcript,
  and retrieved source snippets. This may be a hosted provider. Local transcription
  is **not** a promise that answer generation stays local.
- Public search receives either the query the user explicitly enters or, when **automatic web fallback** is opted in (off by default), a short generic query drafted by your configured model when local evidence looks insufficient. Drafted queries are filtered for URLs, emails, IPs, paths, secret-like text and project names, and fail closed on any error. This filtering is best effort, not DLP: **keep the fallback off for sensitive calls.**
- **All projects + sessions** scope reads up to 100 eligible project folders under the project root (never the whole computer); evidence from unrelated projects may reach the configured model.
- Project/session access is read-only. No model tools, automatic speech, chat
  messages, or business-system writes are granted.
- The desktop integration uses the host's authenticated plugin API. Do not expose
  a development server or plugin API without authentication to a network.

Explicit **Copy answer + evidence** exports the displayed answer and source
excerpts to the clipboard after confirmation. It does not include the hidden
request snapshot or automatically copy the live transcript. Excerpts can be
confidential; clipboard history and device sync may retain them. Clear meeting
does not clear the OS clipboard or copies you saved elsewhere.

New live-context answers also retain an immutable sanitized snapshot of the exact
bounded transcript JSON passed to the provider, including supplied segment IDs
and uninterpreted backend times. It is held only with bounded request state;
Clear/new meeting retires it. Explicit transcript-context copy has a separate
confirmation and can persist in clipboard history or sync. Ordinary answer and
evidence copy excludes it. A valid excerpt reference is not factual verification.

## Credentials and model selection

Configure API keys only in Hermes-managed secret settings for this plugin's
default profile. Production requests use `agent.secret_scope.get_secret` in a
bound host scope; unavailable scopes fail closed. The plugin does not edit
credential stores. The old in-pane editor and startup dotenv bridge are retired.

New preferences follow the current Hermes default through the genuine
registration-supplied public LLM facade, only in a matching host-bound profile
scope and supported nonisolated runtime. Hermes owns its credentials and
routing/fallback policy; Copilot never copies core secrets. Missing registration,
remote isolation or an unavailable scope blocks that route. Existing explicit
provider choices are retained. Retries in default mode use the current Hermes
default, which may differ from the original request.

You can instead choose an explicit BYOK provider and exact model ID. Set transcript source and audio
defaults in **Settings → Plugins → Meeting Copilot** (or the pane's Answer
setup and Transcript source on older Desktop hosts). The pane retains live
controls, active recording selection, explicit consent, privacy disclosures, and
the optional per-request public web query. On modern hosts, changes to capture
defaults require fresh permission confirmation in the pane. On older hosts,
review permission again after changing capture choices. There is no
implicit fallback from a failed BYOK route to the host default. After changing keys, finish requests, pause capture,
and reopen the pane. Already admitted workers retain their
credential snapshots; a submitted request cannot be recalled by changing keys.

The standalone CLI is offline-only and does not read provider credentials.
For optional Meta transcription, the authenticated parent passes the scoped
key once over a private worker pipe, not through the worker environment. Audio
workers inherit only explicitly named OS/locale settings and, for local models,
`HF_HOME`, `HF_HUB_CACHE` and `HF_HUB_OFFLINE`. Unrelated host secrets, Python
loader overrides and arbitrary `HF_HUB_*` values are excluded. Implicit Hugging
Face cached-token use is disabled because the supported Whisper weights are public.
See [BYOK boundaries](docs/byok.md) and the [optional Meta route](docs/meta-api-key.md).

## Readiness and explicit setup

Capture preflight is read-only and fail-closed. It checks source-specific local
requirements and consent without capture, permission prompts, downloads or
provider calls. Start independently rechecks. Unknown Windows device/privacy
readiness blocks native capture; Meetily remains an explicit alternative.
**Request macOS permissions…** is a separate user-confirmed setup action which
may show OS prompts but never starts capture. Explicit model preparation may
download public weights; it is not invoked by checks or Start.

**Load model options…** is also explicit. The selected provider receives only
its own host-scoped saved key at a fixed HTTPS listing endpoint, not meeting or
project content. No redirects, fallback provider, persistent catalog, automatic
refresh or model selection is used. OpenAI listing is key-visible metadata;
OpenRouter listing is a public catalog, not proof of account entitlements. Both
leave credits and Chat Completions compatibility unverified. Discarded or timed-out
requests cannot be recalled, but their results never change the chosen model.
See [preflight and options](docs/preflight.md).

## Limitations

Secret filtering and prompt instructions are defense in depth, not a guarantee.
Do not select a project containing material you are not authorized to send to the
configured model. Source snippets may contain inaccurate or adversarial text.
Session history records statements, not independently verified execution.

Clear meeting removes the application's displayed buffer/result; it cannot undo
an answer request already sent to a provider. Clear is a process-local
forget-everything boundary, not durable deletion: it suppresses cleared
content on every later scan and releases retained transcript text, but the
Meetily source files are untouched, so re-attaching after a backend restart
can surface source transcript content again. Pause stops new capture but does
not erase the transcript. Closing the pane triggers pause, with a heartbeat
expiry fallback. Audio device changes may require restarting capture.

Use approved providers and obtain participant consent. There is no stealth mode,
recording-policy bypass, or screen-sharing invisibility guarantee.

The macOS overlay's **Show in recordings** toggle controls its AppKit window
sharing policy, not the recorder. On requests normal inclusion; off requests
legacy best-effort exclusion (`NSWindow.SharingType.none`). Modern ScreenCaptureKit
and other capture tools may still include it. Neither state guarantees that a
particular recording will include or exclude the window. To keep it out of a
recording reliably, hide the overlay and verify that the window is gone before
recording. The toggle is off at launch and never starts audio capture or sends
meeting context. Dragging and position presets only rearrange the window.

## Reporting

For security-sensitive findings, use GitHub's private vulnerability reporting if
available rather than posting secrets or exploit details in a public issue.
Maintainer: [Adam Nugroho](https://github.com/adam4788).
