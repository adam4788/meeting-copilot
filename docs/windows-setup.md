# Windows Desktop setup

> Current safety limit: native Windows Start is blocked because complete
> nonprompting device/privacy readiness checks are unavailable. Use an accessible
> active Meetily recording. The instructions below prepare dependencies only and
> do not bypass the required [preflight](preflight.md).

Requires native Hermes Desktop / Agent 0.22.7 or newer. Check the installed
version with `hermes --version`. Native Windows normally stores data in
`$env:LOCALAPPDATA\hermes`; an explicit `HERMES_HOME` selects the active profile.
The standalone `install.py --home PATH` override remains available for development,
but managed installation provides Git provenance and an immutable pin.

## Managed core installation

In PowerShell, use an exact reviewed commit from the
[runtime-release branch](https://github.com/adam4788/meeting-copilot/tree/catalog/runtime-0.1.0):

```powershell
$Pin = "REVIEWED_40_CHARACTER_RUNTIME_SHA" # replace before running
hermes plugins install adam4788/meeting-copilot --ref $Pin --no-enable
hermes plugins enable meeting-copilot-live
```

This is a custom Git source, not an admitted catalog entry. The development tree
currently produces a CAUTION security scan, including development mutation tests
and runtime child-process operations. Review the actual findings and approve the
warning explicitly before any override. Do not disable scanning. A manually
installed `meeting-copilot-live` also occupies the target directory: preserve its
backend and standalone Desktop files before a managed replacement. Do not use
`--force` as a generic troubleshooting step: it also accepts scan cautions.

This pin includes the Windows dependency declarations and recent-transcript quick
actions. A published build is not proof that Desktop has loaded it.

After an approved installation, check:

```powershell
hermes plugins doctor meeting-copilot-live --ci
```

The unified Desktop plugin is managed in **Settings → Plugins**. Its pane is
**Meeting Copilot**. A previous custom install can also have a standalone copy in
`desktop-plugins/meeting-copilot-live`; resolve that duplicate only after backing
it up. Python backend code already mounted by a running gateway may need an idle
gateway restart. Finish active work first; never restart a busy gateway to load
the plugin. Inspect the actual pane before assuming installation succeeded.

## Desktop-managed installation and missing SoundCard

For packaged Hermes Desktop, use **Capabilities > Plugins > Install from Git**
(labels can vary), enter `https://github.com/adam4788/meeting-copilot`, and pin the
full revision above in **Advanced**. Review both Agent and Desktop components,
the selected profile, dependencies, and scanner findings. An existing installation
may need the explicit **Force reinstall** option after reviewing its warnings;
never accept an unexpected caution just to get past it.

A source-checkout CLI and the packaged Desktop gateway can use different managed
Python environments. Doctor succeeding in one does not prove capture is available
in the other. Reopen the pane after installation and any idle-only restart, then
check its Windows readiness message. Do not start recording while packages are
reported missing.

If Desktop still reports missing SoundCard, verify the install review actually
completed in Desktop for the intended profile. If it fails with **Access denied**
or **WinError 5**, stop and retain the error for troubleshooting. Do not change
WindowsApps permissions, disable isolation, copy packages into the running runtime,
or use arbitrary pip commands as a workaround. Typed project questions can still
work with configured BYOK while capture setup remains blocked.

## Answer setup

The core supports project retrieval and typed questions independently of audio.
In Hermes's managed plugin settings, choose the intended profile and set an
OpenAI or OpenRouter key in the secure agent-settings field. Keep keys out of
chat, shell history, logs and project files. Reopen the pane after saving a key.
In **Settings → Plugins → Meeting Copilot**, choose the provider and its exact
model ID; the pane's **Open settings** button navigates there. Older Desktop hosts
without plugin settings pages keep **Answer setup** and **Transcript source** in
the pane. Leave paid requests pending until that choice and setup are complete.

Select one project, keep **Exclude past sessions** checked and web off for
project-only evidence. Type a question in **What should I say?** and use
**Suggest answer**. Review the numbered sources. No recording is needed for a
typed question.

## Experimental Windows capture

The Windows changes expose dependency readiness without importing audio libraries,
opening devices, downloading models or testing recording permissions. Missing
optional packages disable Windows Start in the pane, while answers remain usable.
Metadata presence alone does not prove the native libraries or hardware work. The separate preflight initializes the selected WASAPI endpoints in an isolated process without starting streams, then checks the prepared model offline.

This revision declares NumPy, SoundCard and faster-whisper as Windows-only
plugin dependencies. Hermes prepares them through its managed dependency resolver
when installing/enabling this revision; macOS/Linux do not acquire this Windows
stack. Installing them in an unrelated development venv will not enable Desktop
capture. A running gateway
keeps its original dependency environment until an idle restart. Finish active
work before restarting and recheck readiness afterward. Do not patch the active
managed environment with an arbitrary pip command. Dependency preparation can
require a download; Whisper model weights require separate explicit preparation; Start never fetches them. Review the actual dependency and scanner prompts.

When dependencies and recording permissions have been separately verified, choose
**Settings → Plugins → Meeting Copilot → Transcript source → Capture source →
Windows — experimental** (or use **Transcript source** in the pane on older
hosts). The Windows controls provide **Audio source** (Microphone, System audio,
or both) and **Local transcription model** (tiny.en, base.en, small.en). Only the selected source is
opened. The pane uses default Windows devices; system loopback includes all audio
on the default output, which may differ from the device used by a meeting app.
Whisper runs locally on the CPU using already-cached weights. From the supported runtime, explicitly run `python -B capture_preflight.py --prepare-model base.en` to prepare public weights before Start; preparation may download weights but never records audio.
The selected answer provider remains separate from transcription.

In the pane, confirm participant consent and a private test environment before
**Start listening**; confirm permission again after changing capture settings.
Pause and verify an idle stop status before editing capture defaults. Opening
Settings may unmount the pane and initiate pause. Use **Pause listening** and
verify the stop status. A slow driver or
inference can leave shutdown unconfirmed; do not assume a hidden pane proves
audio stopped. Queues stay bounded; evicted transcription chunks are shown in the
pane and may mean missing words. Native frame loss is not measured by this
SoundCard fallback, and synthetic queue tests do not establish hardware reliability.

Swift capture and the floating macOS overlay are unavailable on Windows. Use the
Desktop pane; do not run `prepare_macos.py` here. Meetily attachment remains an
optional separate integration, with its own recording lifecycle.

## Safe development verification

Use a separate checkout and development venv. The following tests use synthetic
data and do not open physical audio devices:

```powershell
$env:MEETING_COPILOT_AUDIO_TEST = '0'
python -m unittest discover -v
node --check desktop/plugin.js
node --test tests/test_desktop.mjs
python ask.py --docs sample-docs --provider fake --question 'What is the unit price?'
```

Keep installation/UI loading, synthetic capture/backend tests, live provider
responses, and physical audio verification as separate results. Passing unit
tests or doctor does not verify a loaded Desktop pane, live API account, audio
permissions, native loopback, or sustained meeting reliability.

## Source-only, non-recording diagnostics

From a development checkout, with the **same interpreter used by the gateway**:

```powershell
& $GatewayPython -B scripts/windows_audio_diagnostics.py
& $GatewayPython -B scripts/windows_audio_diagnostics.py --devices
```

Set `$GatewayPython` to the verified gateway interpreter, not a guessed system
Python. The first command checks package metadata only. The optional second
command imports SoundCard and lists endpoint names locally, but never opens a
stream, downloads a model, changes permissions, or marks capture ready. Review
names before sharing output. This helper is source-only, not a runtime dependency.

Endpoint discovery is not a permission or recording test. Before unblocking
native Windows capture, a developer must implement device/privacy preflight and
run an explicitly consented short test outside real meetings: close unrelated
audio applications, select the intended microphone/output, speak a synthetic
phrase and play a known test clip, then stop. Verify both separate source WAVs,
transcript alignment, manifest completeness, and that capture actually stops.
Also test denied access, device removal, repeated Start/Stop, and a failed source
without silently switching to another device. Do not relax the Start guard to
perform this test. Do not modify Windows privacy settings automatically.

Use the capture backend's supported shared-mode format handling. A separate
PortAudio format probe can reject 16 kHz for a WASAPI device; that alone does not
prove SoundCard (a different backend) will fail or succeed. Verify the actual
backend rather than treating dependency presence or another library's format
probe as end-to-end evidence.


### Guarded isolated Windows capture

The current source requires audited **SoundCard 0.4.6**, a supported Python gateway
interpreter, initialized selected endpoints, and cached local model weights. The
probe constructs a shared-mode WASAPI stream but never calls Start or reads PCM.
Initialization can reject access, format or device errors; it is not a guarantee
that the device remains available. Start repeats the probe and binds exact
endpoint IDs. The selected default output can include other apps' audio.

Live native capture runs in a separate fixed Python worker with a minimal OS/cache
environment, implicit Hugging Face tokens disabled, and offline model loading.
No gateway provider credentials are inherited. Running is not announced until
all selected sources deliver frames. A private stop pipe initiates bounded
shutdown; a stuck worker is terminated and incomplete output is flagged.

An inaccessible packaged WindowsApps runtime must be resolved through Desktop's
supported dependency/install flow. Do not change protected-directory permissions,
disable isolation, substitute an unrelated source venv, or infer that packages
present in a development checkout are available to the live gateway.

These changes have synthetic tests; successful real Windows microphone/system
capture and transcription remain unverified until the bounded consented test is
completed in the actual supported runtime. Never call metadata-only probes a
successful recording test.
