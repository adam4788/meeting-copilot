# Getting started

Meeting Copilot has three separate setup tasks: install the plugin, configure an
answer provider, and prepare a transcript source if you need meeting audio.
Begin with a synthetic document question before testing a meeting.
For native Windows, follow the [Windows setup guide](windows-setup.md).

## Install the published source

Use Hermes 0.22.7 or newer. The plugin ID is `meeting-copilot-live`.

```sh
hermes plugins install adam4788/meeting-copilot --ref b1e9089eef0d55da3c724306b13e5d4479a1eef0 --enable
hermes plugins doctor meeting-copilot-live --ci
```

Run **Open Meeting Copilot** from the command palette. Desktop settings labels
may vary by Hermes version. Find this plugin's settings rather than entering
credentials into the Copilot pane.

The manifest declares `fastapi>=0.115,<1`; Hermes resolves it under its core
constraints. Doctor checks registration and setup. It does not test your API
account, audio devices, or meeting accuracy. Catalog admission is a separate
process; see [catalog preflight](catalog-readiness.md).

The full source checkout includes development mutation tests and operations
that launch processes or install dependencies. Review the complete scanner
findings for the selected revision. An installed or reloaded plugin does not
verify the Desktop pane, a provider account, or native audio on your machine.

## Configure answers

1. In Hermes plugin settings, open **meeting-copilot-live** for the default
   profile. Enter an OpenAI or OpenRouter key in its matching secret field and
   save. Never paste keys into chat, questions, or model fields.
2. Reopen the pane after settings change. First finish active requests and pause
   capture; admitted requests retain their credential snapshots.
3. Expand **Answer setup**, choose **Answer provider**, and enter the exact
   **Answer model** ID your account supports.

Keys, credits, and Chat Completions compatibility depend on the account. There
is no account model discovery or automatic provider/model fallback. A saved key
does not prove a successful connection. Live OpenAI/OpenRouter responses remain
unverified; see [BYOK boundaries](byok.md). The optional
[Meta API route](meta-api-key.md) is explicit and is never the answer default.

## Choose project evidence

Project discovery uses `MEETING_COPILOT_PROJECT_ROOT` when configured in the
backend environment, otherwise `~/Documents/Projects`. Each project must be a
direct child directory. For example:

```text
Projects/
  copilot-sample/
    launch.md
```

Use a fictional file first, as shown in the [demo](demo.md). Supported formats
are UTF-8 `.md`, `.txt`, and `.rst`; PDF, DOCX, and XLSX extraction is unavailable.
Hidden paths, symlinks, dependency trees, and common credential filenames are
excluded. Filtering is best effort, so keep secrets out of searchable material.

In **Answer setup**, select one project in **Search scope**, keep **Exclude past
sessions** checked, leave **Automatic web fallback** off, and leave **Specific
public web query** empty. A fresh pane excludes history and web; saved scope
choices persist. **All projects + sessions** can include unrelated evidence.

If a project is missing, check the root and child-directory layout. An already
running backend will not automatically pick up an environment change. Arrange
any required gateway restart when other Hermes work is idle. Do not restart an
active session just to reload this plugin.

## Prepare a transcript source

Audio is optional for a typed project question. Keep capture paused until you
have permission from participants and have checked the chosen source.

| Source | Preparation and limits |
| --- | --- |
| macOS native | From a source checkout, `python prepare_macos.py --home /path/to/profile` prepares an isolated Python 3.12 audio runtime and compiles/signs Swift helpers. It requires uv and Apple Command Line Tools. It supports managed catalog checkout and custom installer layouts; it does not start capture or request recording permission. Native loss is unresolved. |
| Windows native | Experimental SoundCard/WASAPI capture, with optional dependencies in `requirements-audio.txt`. Consult [Windows audio](audio.md). Desktop setup is being verified separately; prior standalone synthetic checks do not prove the complete Desktop flow. |
| Meetily | Optional, unofficial attachment to an explicitly chosen active JSON recording. Meetily owns its recording files and retention. See [adapter setup](meetily.md). |

In **Transcript source**, choose the capture source and available audio settings.
The current pane defaults to Meetily; select your intended source explicitly.
Local Whisper is independent of the answer API key. Where offered, optional
Meta transcription sends meeting audio off-device and requires a separate
explicit choice and configured key. Model preparation may download weights.
No compiled helper executable is committed to this repository.

For an existing custom local installation, `python install.py` backs up replaced
plugin files and preserves configuration. Close the pane and finish active
Hermes work first. A mounted backend needs a safe gateway restart to load new
code. This legacy developer path is separate from managed Git installation.

## Listen, ask, and stop

1. Confirm **Permission check**, then choose **Start listening** or the Meetily
   attachment action. Approve any actual OS recording prompt yourself.
2. Type a question, or leave it blank to ask about the latest conversation, and
   choose **Suggest answer**. Inspect **Evidence**, warnings, and uncertainty.
3. Use Pause/Detach when finished. If stopping is still pending, follow the pane's
   status and recovery controls. Do not restart capture while shutdown is uncertain.

Closing the pane initiates pause; it is not an instant physical-stop guarantee.
Pause does not erase the transcript. Clear removes the plugin's buffered content
and results, but cannot recall a submitted provider request or delete Meetily
source files. A retry explicitly resends saved context.

## When something is unavailable

| Symptom | Check |
| --- | --- |
| Pane will not open | Hermes version, plugin enablement, and doctor output |
| Provider unavailable | Matching Hermes secret setting, reopened pane, exact model ID, account access and credits |
| No project evidence | Project root, direct child layout, supported text format, and [scan limits](knowledge.md) |
| Capture unavailable | Platform preparation and the selected transcript source; do not infer readiness from CI |
| Stop or answer status uncertain | Recheck status before restarting or asking again; duplicate requests may incur billing |

See [privacy](../SECURITY.md), [current status](../README.md#current-status), and
[compatibility cautions](catalog-compatibility.md) before broader use.
