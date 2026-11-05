# Meeting Copilot

A read-only meeting assistant for Hermes Desktop by Adam Nugroho. Capture a
consented meeting, select a project, then explicitly ask for a short answer
with numbered evidence sources. Nothing is spoken, posted or submitted for you.

**Development status:** native microphone and system-audio paths have been tested
with fictional content. Native frame drops remain an open reliability issue.
OpenAI/OpenRouter live account responses are still unverified; automated tests
use a synthetic vendor. Passing doctor or validation is not production readiness.

## Setup

Requires Hermes **0.22.7+**. The manifest declares `fastapi>=0.115,<1`, resolved
under Hermes's core constraints. In a source checkout:

```sh
hermes plugins doctor "$PWD" --ci
hermes plugins validate "$PWD" --install-deps
```

A catalog entry is being prepared; the local BYOK commits are not published yet.
Do not treat this project as an admitted catalog plugin. See
[catalog readiness and disclosures](docs/catalog-readiness.md) and
[compatibility cautions](docs/catalog-compatibility.md).

For the existing custom local installation, close the pane and finish active
Hermes work before running `python install.py` in the appropriate profile. It
backs up replaced plugin files and preserves configuration. A mounted backend
needs a safe gateway restart to load changed code. Do not restart active work.

On macOS, `python prepare_macos.py --home /path/to/profile` prepares the installed
plugin's separate Python 3.12 audio runtime and builds/signs its Swift helpers
from source. Both catalog checkout and custom installer layouts are supported.
It needs uv and Apple Command Line Tools; it installs audio requirements but
never starts capture or requests recording permission. No compiled executable
is committed. Windows native capture is experimental and requires the optional
`requirements-audio.txt` dependencies and separate hardware verification.
Meetily JSON attachment is optional and unofficial.

## Choose a provider and use the pane

1. In Hermes Settings → Plugins → **meeting-copilot-live**, select the default
   profile, enter an OpenAI or OpenRouter key in its secure field, and Save
   settings. Never put keys in chat or model fields. Reopen the pane afterward.
2. In Answer setup, choose that provider and its exact model ID. Keys, credits
   and Chat Completions compatibility are account-dependent. There is no
   automatic provider/model fallback or account model discovery.
3. Choose one project. Keep **Exclude past sessions** checked and web off for
   project-only retrieval. Saved intentional scope choices persist.
4. Select the transcription source. Local Whisper is independent of the answer
   key. Confirm permission, then Start. A new OS permission prompt needs approval.
5. Type a question or ask about the latest conversation, then Suggest answer.
   Inspect sources and uncertainty. Pause when finished and check the stop status.
   Closing the pane initiates pause; it is not an instant-stop guarantee.

The optional explicit Meta API route and transcription remain available for
existing integrations; they are never the default. The old in-pane key editor,
startup dotenv bridge and generic host-model fallback have been removed.
See [BYOK boundaries](docs/byok.md) and [optional Meta setup](docs/meta-api-key.md).

## Data and execution disclosure

Selected questions, bounded transcript and retrieved excerpts leave the device
when sent to the explicitly selected OpenAI/OpenRouter or optional Meta HTTPS
API. Optional Meta transcription sends audio off-device. Provider billing and
retention rules apply. Local transcription uses cached/downloadable Whisper
weights and does not make answers local.

Retrieval reads selected project files outside plugin-owned storage, and prior
Hermes session SQLite data only when history is enabled. Optional web search
sends a query through the host dispatcher. Filtering is best-effort; review
sources before sharing. New panes default to history excluded and web off.

Capture/overlay/audio workers are child processes; preparation runs local
compiler, signing and dependency-install commands. Recording requires explicit
consent and Start. No extra raw audio files are saved by capture. The overlay
may appear in screen shares; no invisibility guarantee is made. No telemetry or
self-updater is introduced. Credentials use Hermes-managed scoped settings;
production code does not edit credential stores. Standalone CLI environment
credentials are a separate explicit mode.

Private host scope/search imports remain version-sensitive. Development-only
mutation and backpressure probes are reviewable source and are not installed
runtime entrypoints. See [security boundaries](SECURITY.md) and
[native backpressure evidence](docs/native-backpressure.md).

## Offline checks and development

```sh
python ask.py --docs sample-docs --provider fake --question "What is the unit price?"
python demo_standalone.py
python -m unittest discover -v
node --check desktop/plugin.js
node --test test_desktop.mjs
python verify_native_backpressure.py
```

Install `requirements-dev.txt` in a development environment for tests. The
backpressure probe needs Swift but opens no audio devices. Standalone BYOK uses
`--provider openai|openrouter --model EXACT_ID` with the corresponding key supplied
in the calling environment. It does not read credential files. Synthetic tests
are not a live-provider or hardware reliability claim. See [contributing](CONTRIBUTING.md).
