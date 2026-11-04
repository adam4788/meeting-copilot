# Meeting Copilot

A source-grounded meeting assistant for Hermes Desktop by **Adam Nugroho**.

Get short, speakable answer suggestions during Teams or Zoom calls, grounded in your own project files and prior sessions before optional web research. Built as a desktop integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> **Early development.** Not a finished replacement for Meetily or Cluely. Hardware compatibility, transcription quality, and answer latency must be checked on your machine. No meeting bots, automatic speaking, or message sending.

## Standalone use (no Hermes required)

The answer core runs without Hermes installed, without a Hermes home, and
without network access unless you opt into a provider:

```sh
python demo_standalone.py            # offline fixture demo, exits nonzero on failure
python eval_retrieval.py             # synthetic recall@k + latency percentiles (not production accuracy)
python ask.py --docs ./sample-docs --doctor          # first-run check: layout, searchable chunks, provider
python ask.py --docs ./sample-docs --list-projects
python ask.py --docs ./sample-docs --search "unit price"
python ask.py --question "What is the unit price?" --docs ./sample-docs --provider fake
python ask.py --question "What is the unit price?" --docs ./sample-docs --transcript ./sample-docs/transcript.sample
# With MODEL_API_KEY already provided by your approved secret manager:
python ask.py --question "..." --docs ./sample-docs --provider meta
```

Your own documents use one folder per project:

```sh
mkdir -p my-docs/acme
printf 'Acme renewal is in September.\n' > my-docs/acme/renewal.md
python ask.py --docs ./my-docs --search "renewal"
python ask.py --docs ./my-docs --question "When is the Acme renewal?"
```

`--doctor --json` reports setup failures in its `problems` array, including
missing project folders and how to arrange the documents; it exits `2` when
not ready. For `--provider meta`, this offline check only checks that
`MODEL_API_KEY` has at least 8 characters after trimming surrounding whitespace;
it does not validate the credential, network, or model.
JSON's `provider.ready` reflects that local check, while
`provider.connection_verified` is `false`.

`ask.py` has three job-shaped modes: `--list-projects` shows what can be
searched, `--search` returns evidence only (no model call), and `--question`
answers with numbered citations. It reads a docs folder
(`<docs>/<project>/*.md|txt|rst`) plus an optional transcript
(`--transcript meeting.json`, or `-` for stdin), prints sources, warnings,
and timing, or `--json` for machine output. Provider failures exit `2`.
Providers: `fake` (deterministic offline, the default) and `meta`
(Meta Model API via `MODEL_API_KEY`). Web research and session history are
unavailable standalone — the CLI says so instead of failing obscurely.
Library use: `answer_core.AnswerCore` with injected
`completion`/`search`/`history` providers; `hermes_adapters` holds the
host-backed factories the plugin uses by default.

## Meta API key

In the pane, open **Meta API key**, paste into the masked field and explicitly
**Save key**. Never paste a credential into chat. Saved, status-request configured
and provider-verified status are separate; saving does not verify Meta or reload
workers. Pause capture before editing: new host-scoped requests can use the saved
key immediately, ahead of inherited keys, while existing requests/workers retain
their snapshots. Process-based code may need a safe gateway restart. **Remove saved
key** can expose an inherited fallback; it is not credential revocation. Ambiguous
or multiline `.env` files are refused without rewriting unrelated data.
See [key setup and security boundaries](docs/meta-api-key.md), including the
Settings → Plugins fallback for hosts without native-owner credential editing.

## Design

- Explicit start, pause, and consent controls. Capture never starts at import or installation.
- Independent macOS microphone/system-audio transcription with selectable local Whisper models, or opt-in Meta Muse Voice cloud transcription (`MODEL_API_KEY`; audio leaves the device only in that mode); optionally attach to an explicitly selected Meetily recording. Native Windows capture remains experimental.
- Local transcription; this project writes no additional raw audio recordings. Meetily retains its own recording behavior.
- Select one project, **All projects + sessions**, or sessions only, with visible evidence. All-project scope covers up to 100 eligible project folders, not your whole computer.
- **Automatic web fallback is off by default.** When local evidence is insufficient and you opt in, the configured model can draft a filtered generic public query. Filtering is best effort, not a privacy guarantee. A query you enter explicitly always overrides automatic planning.
- Read-only answer generation through Meta Muse Spark (Meta Model API) when `MODEL_API_KEY` is set in the gateway environment, otherwise your configured model or an explicitly selected authenticated provider/model, independently of the transcription model. No global chat-model changes, agent tools or external-system mutations.
- In-memory transcript buffer and a capture heartbeat that stops listening when the pane disconnects.

**Privacy boundary:** local transcription does not mean fully local answers. The question, bounded transcript, and selected source snippets are sent to your configured answer model, including the fallback planner when enabled. Use a locally hosted model if those must remain on-device. Search providers receive either the filtered model-drafted query or the query you explicitly enter. All-project scope can mix unrelated project context; review evidence before sharing. See [SECURITY.md](SECURITY.md).

## Status

Deterministic checks, synthetic-file macOS transcription and explicit answer-model routing have been exercised. Live independent microphone/system capture still needs user-approved permission and device testing. See the [verification notes](docs/verification.md); do not infer production readiness from passing unit tests.

A macOS overlay exists (`native/macos_overlay.swift` and `overlay_bridge.py`), launched only by the pane's Show overlay action after you start listening. It streams real incremental answers into the tapped question row using your current pane settings (project/sessions, web fallback, answer provider/model), auto-syncing scope changes while shown; nothing is captured or sent until you tap. `sharingType = .none` is best-effort only: Apple documents it as legacy, so assume the overlay may appear in a screen share. No visual or Zoom/Meet screen-share test has been performed. The overlay returns to click-through after ~10s; press the shortcut (default Ctrl+Option+Cmd+O) to interact again.

## Install and use

Prerequisites: a configured Hermes Desktop/Agent 0.22+ installation and Python 3.11+. Meetily 0.4.1 is optional for the JSON attachment mode; the adapter is unofficial and future versions may require changes.

Run these commands using the **Python environment belonging to your Hermes installation**:

```sh
git clone https://github.com/adam4788/meeting-copilot.git
cd meeting-copilot
python install.py
hermes plugins enable meeting-copilot-live --no-allow-tool-override
```

Installation copies only this plugin into the active `HERMES_HOME`, backing up an existing copy. It does not start recording or change your model configuration. Close/detach the pane before reinstalling. Use the default profile; other profiles are not yet supported.

**Backend updates:** an in-process gateway may keep an already-mounted backend in memory even after plugin disable/enable. After updating, restart that gateway **when its other sessions are idle**; do not assume a hot-reloaded pane means the Python backend was refreshed. Host-isolated deployments can pick up a changed content-addressed API module without that restart.

1. Open the **Meeting Copilot** pane in the desktop layout.
2. Start recording in Meetily, then choose that active recording in the pane.
3. Choose **Search scope**: one project, **All projects + sessions**, or **Past sessions only**.
4. Confirm permission and click **Attach to Meetily**.
5. Click **Suggest answer** for the latest question, or type a specific question.
6. **Automatic web fallback** is unchecked by default; check it to allow automatically planned searches for this meeting. To override it, expand **Search online with a specific query** and enter a public query explicitly.

**Detach** stops the copilot reading, not Meetily recording. **Clear meeting** clears the copilot's buffer/result but does not delete Meetily's files or undo requests already sent to a provider.

Current file retrieval supports Markdown/plain text/reStructuredText, not PDF, DOCX or XLSX. See [KNOWLEDGE.md](KNOWLEDGE.md), [MEETILY.md](MEETILY.md), and the experimental [native audio guide](AUDIO.md).

### Independent macOS transcription and model selection

No Meetily application, recordings, or models are required for this mode. A macOS audio worker plus a small signed Swift capture helper are built during preparation (see step 1); they only run after you click Start listening.

1. Install this plugin as above, then run `python prepare_macos.py` on macOS with `uv` and Apple Command Line Tools installed. This prepares an isolated Python 3.12 environment, faster-whisper CPU runtime, and an original signed Swift helper; it does **not** start capture or ask for recording permissions.

   For cloud transcription instead of local Whisper, set `MODEL_API_KEY` in the gateway environment before preparing, then choose **Meta Muse Voice** as the transcription provider in the pane and pick an ASR mode (Endpointing, Diarization, or Push-to-talk). Local transcription stays the default and needs no key.
2. Restart an already-running gateway from a separate terminal when its other work is idle: `hermes gateway restart`. Reloading desktop plugins alone does not refresh an already-mounted Python backend.
3. Open the pane with the bottom-right **Meeting Copilot** button or **Cmd+K → Open Meeting Copilot**. The status-bar button also hides it; hiding unmounts the pane and pauses/detaches capture.
4. Choose **Independent macOS transcription**, then **Microphone**, **System audio**, or **Microphone + system audio**. System audio captures system playback, not a specific selected app. Select **tiny.en**, **base.en**, or **small.en**. This initial release supports English only; higher-accuracy models use more memory/CPU. First explicit use can download weights from the public faster-whisper model source into the normal Hugging Face cache.
5. Confirm participant consent and click **Start listening**. Approve macOS Microphone and/or Screen & System Audio Recording permissions when requested. Permission attribution and real audio routing need validation on your machine. **Pause listening** terminates the worker/helper; no additional audio recording is saved.
6. Under **Answer model**, keep the default (Meta Muse Spark via the Meta Model API when `MODEL_API_KEY` is set in the gateway environment) or select an authenticated provider and exact model, including Meta Model API entries. This applies only to the copilot's answer and optional fallback planner, not Hermes chat or transcription. Explicit selection failures do not silently switch models. The public search provider remains the configured Hermes search provider. Each answer reports its `model_route` beside the elapsed time.

Local transcription is separate from answer privacy: bounded transcript/source context may still leave the device for the selected answer provider. Disable automatic web fallback for sensitive calls. No raw audio is sent to that provider. Cancel, pause, and Clear meeting mark requests so no further answers are published, but they cannot recall context already transmitted to a model or search provider, nor interrupt an in-flight provider call already in progress.

For a no-device transcription check, generate a non-sensitive WAV and run the installed `audio-runtime/bin/python audio_worker.py --file path/to/synthetic.wav --model tiny.en` from the installed `dashboard/core` directory. Do not use private recordings as public test evidence. The [REA reconstruction report](docs/meetily-reconstruction.md) documents architectural evidence, not runtime parity with Meetily. This implementation does not copy or invoke Meetily binaries/assets.

## Development

Use Python 3.11+ and Node.js 20+:

```sh
python -m pip install -r requirements-dev.txt
npm test
npm run check
```

Audio dependencies are optional for unit tests and non-Windows development. Runtime audio installation and desktop installation are documented below as those paths are verified.

## Architecture

```text
Microphone + system loopback → local streaming transcription
                                      ↓
                              live transcript buffer
                                      ↓
                       user presses Suggest answer
                                      ↓
                  project files → prior sessions → optional web
                                      ↓
                       configured model, no agent tools
                                      ↓
                      short answer + source evidence
```

The audio layer, knowledge engine, and desktop UI are separate components so that proven capture/transcription systems can be integrated without replacing the answer engine.

## Responsible use

Obtain participant consent and follow workplace recording rules. This is an assistive tool, not a covert interview or exam aid. No screen-share invisibility or evasion guarantees. Review suggestions: past session statements are not proof that an action succeeded, and sources can be outdated or wrong.

## References and acknowledgments

- [Meetily](https://github.com/Zackriya-Solutions/meetily): the preferred local Windows transcription source through an original, unofficial JSON adapter. This repository does **not** claim to be a Meetily fork or ship its code/binaries.
- [Meeting Overlay](https://hermes-agent.nousresearch.com/docs/plugins/meeting-overlay): reference for the live meeting-copilot workflow.
- Cluely: product-category reference only. No code, branding, or affiliation claimed.

Dependencies retain their own licenses. If upstream code is incorporated later, its required notices must be preserved.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Small, tested changes and real Windows device reports are welcome. Never attach private meeting recordings, credentials, or workplace documents to public issues.

## License

[MIT](LICENSE) © 2026 Adam Nugroho.
