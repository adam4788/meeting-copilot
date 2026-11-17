# Record audio and transcript together

Recording is opt-in and separate from the existing memory-only listening mode.
Choose native capture, complete its source/model/permission checks, enable **Save
audio + transcript**, confirm participant permission, and press **Record**. Once
setup is ready, this single action starts audio saving and transcription together.
Record starts a new meeting rather than appending to an earlier recording.

**Stop & save** stops capture and finalizes the bundle. Closing the pane and its
lease-expiry fallback also initiate stopping; verify stop confirmation before
assuming the microphone is off. A failed or timed-out stop must not be treated as
finished. On an audio/transcript storage failure, capture is stopped and the
bundle is marked incomplete. Recording is not automatic, hidden, or scheduled.

## Files for later processing

The pane shows the current/most recent bundle folder. All bundles live in
`meeting-copilot-recordings/` within the active Hermes home. Each Record gets a
unique directory containing:

- `microphone.wav` and/or `system.wav`: captured sources, mono PCM16 at 16 kHz.
- `audio-timeline.jsonl`: source timestamps and WAV sample offsets, for alignment.
- `transcript.jsonl`: timestamped transcript events, including trailing segments
  the provider successfully returns. If an `id` repeats, the newer event replaces
  its earlier revision. Source labels are not guaranteed speaker identities.
- `manifest.json`: selected source/provider, start/stop times, and final state.

These open formats can be processed later without a running Copilot session.
The live answer actions still use their existing bounded recent context; saving
is not a promise that an answer used the entire recording. This first version
has no recording-library browser, playback player, or automatic re-transcription.

A `partial` state means capture/transcription loss or a save error was reported.
A manifest still saying `recording` after a crash was not finalized: treat it as
incomplete; WAV headers/tails may require recovery. Do not discard the original
files while attempting recovery. Per-session limits are 1 GiB of PCM audio and
16 MiB of transcript events; reaching a limit stops recording instead of silently
truncating it. Native capture and ASR queues can still drop data; inspect warnings.

## Privacy and availability

Files persist after Clear/new meeting, pane close, and application restart. Clear
stops an active saved recording and clears live context, not saved disk files.
Use your file manager to remove unwanted bundles. They are local, not encrypted
by Copilot, and could be included in OS backups/sync. Keep them only as long as
needed and obtain participant consent before recording. Hosted transcription
still sends audio to the explicitly selected provider; local saving does not
make a hosted provider local.

macOS native capture requires the prepared runtime, model/provider and OS
permissions. Windows native capture is fail-closed until the isolated runtime, selected
WASAPI endpoint initialization, and cached-model checks pass. Real device
validation is still required; a metadata check is not a successful recording. The Meetily adapter only reads an explicitly selected active
transcript; start and manage a recording in Meetily itself, under its own storage
and privacy settings. Synthetic tests do not verify microphones or speakers on
either platform.
