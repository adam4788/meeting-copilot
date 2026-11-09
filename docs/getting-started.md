# Getting started

Meeting Copilot has three separate setup tasks: install the plugin, configure an
answer provider, and prepare a transcript source if you need meeting audio.
Begin with a synthetic document question before testing a meeting.
For native Windows, follow the [Windows setup guide](windows-setup.md).

## Install the published source

Use Hermes 0.22.7 or newer. The plugin ID is `meeting-copilot-live`.
Catalog review is pending. Before admission, copy a reviewed full commit from the
[runtime-release branch](https://github.com/adam4788/meeting-copilot/tree/catalog/runtime-0.1.0):

```sh
PIN="REVIEWED_40_CHARACTER_RUNTIME_SHA" # replace before running
hermes plugins install adam4788/meeting-copilot --ref "$PIN" --no-enable
hermes plugins enable meeting-copilot-live
hermes plugins doctor meeting-copilot-live --ci
```

Run **Open Meeting Copilot** from the command palette. Desktop settings labels
may vary by Hermes version. Find this plugin's settings rather than entering
credentials into the Copilot pane.

This guide describes the layout in this source checkout. The pinned published
revision above predates the settings-page-only layout; install a revision that
contains the change before expecting that layout.

The manifest declares FastAPI plus Windows-only NumPy, SoundCard and
faster-whisper dependencies; Hermes resolves them under its core constraints. Doctor checks registration and setup. It does not test your API
account, audio devices, or meeting accuracy. Catalog admission is a separate
process; see [catalog preflight](catalog-readiness.md).

The full source checkout includes development mutation tests and operations
that launch processes or install dependencies. Review the complete scanner
findings for the selected revision. An installed or reloaded plugin does not
verify the Desktop pane, a provider account, or native audio on your machine.

## Configure answers

1. In **Settings → Plugins → Meeting Copilot**, open the agent settings for the
   default profile. Enter an OpenAI or OpenRouter key in its matching secret
   field and save. Never paste keys into chat, questions, or model fields.
2. Reopen the pane after changing keys. First finish active requests and pause
   capture; admitted requests retain their credential snapshots.
3. Use **Open settings** in the pane or navigate to **Settings → Plugins →
   Meeting Copilot**. Choose **Answer provider** and enter the exact **Answer
   model** ID your account supports. Older Desktop hosts without plugin settings
   pages keep **Answer setup** and **Transcript source** in the pane.

Keys, credits, and Chat Completions compatibility depend on the account. There
is no account model discovery or automatic provider/model fallback. A saved key
does not prove a successful connection. Live OpenAI/OpenRouter responses remain
unverified; see [BYOK boundaries](byok.md). The optional
[Meta API route](meta-api-key.md) is explicit and is never the answer default.

## Choose project evidence

Project discovery uses `MEETING_COPILOT_PROJECT_ROOT` when configured in the
backend environment. Otherwise it uses `~/Documents/Projects`, falling back to
`~/Projects` only when the first directory does not exist and the latter does.
Each project must be a direct child directory. For example:

```text
Projects/
  copilot-sample/
    launch.md
```

Use a fictional file first, as shown in the [demo](demo.md). Supported formats
are UTF-8 `.md`, `.txt`, and `.rst`; PDF, DOCX, and XLSX extraction is unavailable.
Hidden paths, symlinks, dependency trees, and common credential filenames are
excluded. Filtering is best effort, so keep secrets out of searchable material.

In the plugin settings, select one project in **Search scope**, keep **Exclude
past sessions** checked, and leave **Automatic web fallback** off. The pane's
optional **Search online with a specific query** is an explicit per-request
action; leave it empty for this narrow test. A fresh pane excludes history and
web; saved scope choices persist. **All projects + sessions** can include
unrelated evidence.

If a project is missing, check the root and child-directory layout. An already
running backend will not automatically pick up an environment change. Arrange
any required gateway restart when other Hermes work is idle. Do not restart an
active session just to reload this plugin.

## Prepare a transcript source

Audio is optional for a typed project question. Keep capture paused until you
have permission from participants and have checked the chosen source.

| Source | Preparation and limits |
| --- | --- |
| macOS native | Run `python prepare_macos.py --home /path/to/profile` from the plugin source. It requires **preinstalled CPython 3.12**, uv, and Apple Command Line Tools with the macOS 26 SDK. It creates an isolated audio environment, installs its declared dependencies and compiles/signs helpers. Automatic Python downloads are disabled; install the interpreter separately from a trusted source. Both catalog and custom layouts are supported. Preparation does not start capture or request recording permission. Native loss is unresolved. |
| Windows native | Experimental SoundCard/WASAPI capture, with optional dependencies in `requirements-audio.txt`. Consult [Windows audio](audio.md). Desktop setup is being verified separately; prior standalone synthetic checks do not prove the complete Desktop flow. |
| Meetily | Optional, unofficial attachment to an explicitly chosen active JSON recording. Meetily owns its recording files and retention. See [adapter setup](meetily.md). |

In **Settings → Plugins → Meeting Copilot → Transcript source**, review the
capture source and available audio/transcription defaults. On older hosts use
the pane's **Transcript source** controls instead. Without a saved choice, the
pane selects macOS or experimental Windows capture when backend capabilities
identify that platform. **Microphone + system audio** and local Whisper are the
defaults unless separately saved choices say otherwise. This is a selection, not
proof that the capture route is ready. If the platform
cannot be identified, choose a source explicitly. Saved source and audio choices
are not changed; Meetily still requires explicit selection and attachment.
Local Whisper is independent of the answer API key. Optional Meta transcription
sends meeting audio off-device and requires a separate explicit choice and key.
Model preparation may download weights. No compiled helper executable is
committed to this repository.

Pause capture and wait for a confirmed idle status before changing transcript
settings. The pane shows the selected source and cloud disclosure; after changing
capture settings, review it and confirm permission again. Opening Settings may
unmount the pane and initiate pause; verify the stop status before restarting.
Editing defaults does not start capture or submit an answer.

For an existing custom local installation, `python install.py` backs up replaced
plugin files and preserves configuration. Close the pane and finish active
Hermes work first. A mounted backend needs a safe gateway restart to load new
code. This legacy developer path is separate from managed Git installation.

## Listen, ask, and stop

1. If using Meetily, select the active recording in the pane. Confirm **Permission
   check**, then choose **Start listening** or the Meetily attachment action.
   Approve any actual OS recording prompt yourself.
2. Type a question, or leave it blank to ask about the latest conversation, and
   choose **Suggest answer**. Inspect **Evidence**, warnings, and uncertainty.
3. Use Pause/Detach when finished. If stopping is still pending, follow the pane's
   status and recovery controls. Do not restart capture while shutdown is uncertain.

Closing the pane initiates pause; it is not an instant physical-stop guarantee.
Pause does not erase the transcript. Clear removes the plugin's buffered content
and results, but cannot recall a submitted provider request or delete Meetily
source files. A retry explicitly resends saved context.

## Move and record the overlay (macOS)

Choose **Show overlay** in the pane. The window starts click-through so it does
not intercept your meeting app. Press **Control + Option + Command + O**
(**⌃⌥⌘O**) to interact temporarily; press it again to return early.

- **Drag the header/grip** to place the overlay freely.
- **Position presets** give one-click corner and centered placements. Top center
  keeps reference text near your camera; bottom center works like captions;
  corners leave the main presentation area clear. Placement stays within the
  usable area of the overlay's current display.
- **Show in recordings: on** allows the normal window-sharing behavior for demos
  and recordings. Your recorder must still capture the display/window containing
  it; recording a different app's window will not necessarily include it.
- **Show in recordings: off** requests the legacy macOS exclusion behavior. It is
  **best effort, not a privacy guarantee**: modern screen recorders may still
  include the overlay. Use the pane's **Hide overlay** action and verify that the
  window is gone whenever it must not be captured.

These controls change only the floating window. They do not start audio capture,
request an answer, change provider settings, or alter the meeting app's recording.
Interaction returns to click-through automatically. Recording visibility starts
off at each helper launch; do not treat that default as proof of capture exclusion.

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
