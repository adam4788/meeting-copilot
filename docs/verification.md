# Verification

The [current status](../README.md#current-status) describes the published preview.
Automated checks exercise synthetic fixtures, not a complete meeting. Use the
output of the current run for test counts and platform skips.

Earlier checkpoints include headless Swift helper checks, synthetic file
transcription, and limited device observations. They do not establish current
Windows Desktop capture, live OpenAI/OpenRouter responses, visual acceptance,
multi-speaker accuracy, or long-call reliability. macOS native frame loss remains
unresolved; see the [backpressure investigation](native-backpressure.md).

[Development](development.md) describes repeatable offline checks. Prior
checkpoint details, including superseded provider and credential designs, remain
in the [historical verification record](https://github.com/adam4788/meeting-copilot/blob/73a1ddc1fe30b766cd51a1b3e784dd65a8d005ba/docs/verification.md).

## Published checkpoint: b01a55a (2026-11-07)

[All five CI jobs passed](https://github.com/adam4788/meeting-copilot/actions/runs/42401652668):
Windows/Linux Python 3.11 and 3.12, plus macOS tests and overlay helper self-test.
The Windows local suite passed 750 Python tests (73 platform/opt-in skips) and
144 Desktop tests (2 POSIX-only skips). CI also exercises those POSIX-only tests.
The correction at this pin changes only synthetic test readiness and read-only
UI wording assertions, not production behavior.

Windows packaged Desktop activation remains unverified: a tested source-managed
environment contains the audio packages, but the packaged gateway still reports
SoundCard missing. That installation/runtime mismatch is separate from CI success.
No live provider response or physical Windows recording is established by this
checkpoint. Follow the [Windows setup troubleshooting](windows-setup.md#desktop-managed-installation-and-missing-soundcard).
