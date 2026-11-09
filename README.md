# Meeting Copilot

**Meeting answers grounded in your project files.**

A community plugin for Hermes Desktop by [Adam Nugroho](https://github.com/adam4788).
Capture a consented meeting, choose a project, and explicitly ask for a short
answer with numbered evidence sources. Read the suggestion and decide how to use
it. Nothing is spoken, posted, or submitted for you.

![Cobalt audio waveform crossing layered document shapes on an off-white halftone background with fine blue guide lines.](docs/assets/meeting-copilot-hero.png)

*Generated conceptual artwork; not a product screenshot.*

[Get started](docs/getting-started.md) · [Try the offline demo](docs/demo.md) ·
[Privacy](SECURITY.md) · [Documentation](docs/README.md)

## Current status

**Early developer preview.** The published source includes OpenAI/OpenRouter
bring-your-own-key setup. Live responses from those accounts remain unverified.
Native audio is experimental; macOS fictional-audio checks found dropped frames.
Windows Desktop pane and runtime verification is still in progress.

| Platform | Earlier CI checkpoint (b01a55a) | Desktop and native audio status |
| --- | --- | --- |
| Windows | Python 3.11/3.12 suite and JavaScript checks passed in CI | Desktop pane/runtime under verification; SoundCard/WASAPI capture remains experimental |
| macOS | Python 3.12 suite, JavaScript checks, and overlay helper build/self-test passed in CI | Microphone/system-audio checks used fictional content; native frame loss remains unresolved |
| Linux | Python 3.11/3.12 suite and JavaScript checks passed in CI | Native audio and Desktop use are not established by these checks |

The CI results above describe source revision
[`b01a55a`](https://github.com/adam4788/meeting-copilot/commit/b01a55a05f2609f366eec8d5a035879754b8263c).
See [CI](https://github.com/adam4788/meeting-copilot/actions),
[verification notes](docs/verification.md), and the
[native loss investigation](docs/native-backpressure.md).
CI and plugin validation do not prove a complete meeting works on your computer.

## What it does

- Reads supported text files from a selected project and shows the evidence used
  for each answer. Markdown, plain text, and reStructuredText are supported.
- Uses local Whisper transcription, optional explicit Meta cloud transcription,
  or an optional, unofficial Meetily JSON attachment, depending on platform setup.
- Sends an answer request to the provider and exact model you select in
  **Settings → Plugins → Meeting Copilot**. OpenAI and OpenRouter are the main
  Desktop answer routes.
- Offers explicit recent-transcript actions: **Catch me up**, **Extract actions**,
  and **Suggest a question**. These use your selected model, not the whole meeting.
- Keeps past sessions excluded and web search off by default in new panes.
  Broader scopes are explicit choices; saved choices persist.
- Offers a read-only macOS overlay for the transcript and detected questions.
  Temporarily enable interaction to drag its header, choose a position preset,
  or turn **Show in recordings** on. Turning it off only requests best-effort
  exclusion; the overlay may still appear in captures. Hide it when it must not
  appear. Ask questions in the Desktop pane.

```mermaid
flowchart TD
    accTitle: Meeting Copilot answer workflow
    accDescr: Consented capture is transcribed into meeting context. Project retrieval supplies evidence. You request an answer from your selected model and review the numbered sources.
    A[Consented capture] --> B[Transcription]
    B --> C[Meeting context]
    P[Selected project files] --> R[Read-only retrieval]
    C --> Q[You choose Suggest answer]
    R --> Q
    Q --> M[Selected provider and exact model]
    M --> E[Suggested answer and numbered sources]
    E --> U[You review and decide]
    classDef default fill:#F2F2F2,stroke:#0000F2,color:#181818,stroke-width:1px
```

Capture, transcription, retrieval, and answer generation are separate steps.
Local transcription does not make hosted answers local.

## Install in Hermes Desktop

Requires **Hermes 0.22.7 or newer**. Install the published source at a full revision:

```sh
hermes plugins install adam4788/meeting-copilot --ref 9a261fd4e549f7ce1715fc39ee49b5a2907d42c6 --enable
hermes plugins doctor meeting-copilot-live --ci
```

Open the command palette and run **Open Meeting Copilot**. Audio preparation is
separate and platform-specific; installation and doctor do not start recording
or verify a live provider. See [the setup guide](docs/getting-started.md) before
enabling capture. Catalog admission has not been established; this is a managed
Git installation of a community project.

The settings layout described below is for this source checkout. The pinned
published revision above predates this change; installing that revision does
not install the new layout.

Review scanner findings for the pinned source before continuing installation.
An installed or reloaded plugin does not establish Desktop pane, provider, or
audio readiness. Windows capture remains experimental.

## Ask your first question

1. In **Settings → Plugins → Meeting Copilot**, open the plugin's agent settings
   for the default profile and save an OpenAI or OpenRouter key in its secret
   field. Reopen the pane; never put keys in chat or model fields.
2. From the pane, use **Open settings** (or navigate to **Settings → Plugins →
   Meeting Copilot**). Choose the matching answer provider and exact model ID,
   and select a transcript source and audio/transcription defaults if needed.
   Older Desktop hosts without plugin settings pages keep these controls in the pane.
3. In settings, select one project, keep **Exclude past sessions** checked and
   automatic web fallback off. Keep the pane's specific public web query empty.
4. Type a question about a synthetic project file and choose **Suggest answer**.
   Open **Evidence** and compare the answer with its sources.
5. To add meeting context, prepare the platform's transcript source, select any
   active Meetily recording in the pane, confirm participant permission, and
   explicitly start capture. Pause when finished and check that stopping is
   confirmed. After changing capture settings, confirm permission again.

Start with [the synthetic walkthrough](docs/demo.md). It also includes an offline CLI
demo that needs no API key, audio device, or Hermes installation.

## Privacy and control

Answer requests send the question, bounded meeting context, and retrieved
excerpts to the selected HTTPS provider. Provider billing and retention rules
apply. Optional Meta transcription also sends meeting audio off-device; local
Whisper may download model weights during preparation.

Project retrieval can read files outside plugin storage. Session history is
read only when enabled. Web opt-in sends a public query through Hermes search;
query filtering is best effort. Select only sources you may send to the chosen
provider and review evidence before sharing an answer.

Capture requires consent and Start. No extra raw audio or transcript files are
saved by this plugin; Meetily retains its own files under its own settings.
Closing the pane initiates pause, and an in-flight provider request cannot be
recalled. Check stop status instead of assuming the device has stopped.

Read [security and privacy](SECURITY.md) and the
[architecture and data flow](docs/architecture.md) for the complete boundaries.

## Known limits

- Native capture can lose audio under load. Long meetings, broad device
  compatibility, and multi-speaker accuracy are not established.
- OpenAI/OpenRouter access, credits, exact model IDs, and API compatibility
  depend on the account. There is no automatic provider/model fallback.
- Sources are evidence supplied to a model, not proof of every generated claim.
  Missing or adversarial evidence can still produce an incorrect answer.
- PDF, DOCX, and XLSX extraction is not implemented. Project scans and session
  reads are bounded; this is not a search of the whole computer.
- Optional web search depends on a registered, profile-scoped Hermes tool and
  may be unavailable. The inspected isolated plugin-host route did not pass
  the request scope needed for BYOK; see [compatibility cautions](docs/catalog-compatibility.md).

## Explore the repository

| Goal | Guide |
| --- | --- |
| Set up providers, project scope, and audio | [Getting started](docs/getting-started.md) |
| Run a synthetic demonstration | [Demo guide](docs/demo.md) |
| Understand keys and model boundaries | [BYOK](docs/byok.md) |
| Understand retrieval and source limits | [Knowledge engine](docs/knowledge.md) |
| Work on the code or run tests | [Development](docs/development.md) and [contributing](CONTRIBUTING.md) |
| Review catalog packaging and disclosures | [Catalog preflight](docs/catalog-readiness.md) |
| Use the visual direction and assets | [Project brand guide](docs/brand-guide.md) |

Meeting Copilot is an independent community project. It is not an official Nous
Research product and is not endorsed by Nous Research. Code is licensed under
[MIT](LICENSE).

### Portable evidence

In the answer pane, **Copy answer + evidence** copies a plain-text report only
when you confirm. It includes the displayed answer, reported model route,
warnings, and numbered source excerpts (up to 2,050 Unicode characters per
source), so colleagues can inspect the basis for a suggestion. Source-update dates are not
meeting timestamps; source numbers identify retrieved documents, not meeting
segments. It is not a complete transcript or independently verified
meeting record. The copy does not send a new model request and excludes the
retained request snapshot and live transcript. Review confidential snippets and
links before sharing; clipboard history or device sync may retain the copy.
### Frozen transcript context

Answers based on live segments retain the exact bounded, sanitized transcript
context sent to the model, even after transcript corrections. Inspect its
per-answer excerpts in **Transcript context sent with this answer**. Backend
times remain uninterpreted, and references are not proof that a model claim is
correct. A separate **Copy sent transcript context** action requires confirmation;
ordinary evidence export excludes it. See [transcript context](docs/transcript-context.md)
for retry, retention, timing and verification limits.