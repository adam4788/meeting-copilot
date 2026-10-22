# Meeting Copilot

A Windows-first, source-grounded meeting assistant by **Adam Nugroho**.

Get short, speakable answer suggestions during Teams or Zoom calls, grounded in your own project files and prior sessions before optional web research. Built as a desktop integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> **Early development.** Not a finished replacement for Meetily or Cluely. Hardware compatibility, transcription quality, and answer latency must be checked on your machine. No meeting bots, automatic speaking, or message sending.

## Design

- Explicit start, pause, and consent controls. Capture never starts at import or installation.
- Read your explicitly selected active Meetily transcript; retain the Windows audio pipeline you already trust. Native Windows capture is an experimental fallback.
- Local transcription; this project writes no additional raw audio recordings. Meetily retains its own recording behavior.
- Select one project, **All projects + sessions**, or sessions only, with visible evidence. All-project scope covers up to 100 eligible project folders, not your whole computer.
- **Automatic web fallback is on by default.** When local evidence is insufficient, the configured model can draft a filtered generic public query. Turn it off for sensitive calls; filtering is best effort, not a privacy guarantee. A query you enter explicitly overrides automatic planning.
- Read-only answer generation through your configured model. No agent tools or external-system mutations.
- In-memory transcript buffer and a capture heartbeat that stops listening when the pane disconnects.

**Privacy boundary:** local transcription does not mean fully local answers. The question, bounded transcript, and selected source snippets are sent to your configured answer model, including the fallback planner when enabled. Use a locally hosted model if those must remain on-device. Search providers receive either the filtered model-drafted query or the query you explicitly enter. All-project scope can mix unrelated project context; review evidence before sharing. See [SECURITY.md](SECURITY.md).

## Status

The project is being built and validated on Windows. See the [verification notes](docs/verification.md) for what has actually been exercised. Do not infer production readiness from passing unit tests.

## Install and use

Prerequisites: a configured Hermes Desktop/Agent 0.22+ installation, Python 3.11+, and Meetily 0.4.1. The Meetily adapter is unofficial and depends on its live JSON format; future versions may require adapter changes.

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
6. **Automatic web fallback** is checked by default; uncheck it to prevent automatically planned searches. To override it, expand **Search online with a specific query** and enter a public query explicitly.

**Detach** stops the copilot reading, not Meetily recording. **Clear meeting** clears the copilot's buffer/result but does not delete Meetily's files or undo requests already sent to a provider.

Current file retrieval supports Markdown/plain text/reStructuredText, not PDF, DOCX or XLSX. See [KNOWLEDGE.md](KNOWLEDGE.md), [MEETILY.md](MEETILY.md), and the experimental [native audio guide](AUDIO.md).

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
