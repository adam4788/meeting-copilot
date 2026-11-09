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

## Evidence build: 9a261fd (2026-11-08)

This historical checkpoint used `9a261fd4e549f7ce1715fc39ee49b5a2907d42c6`, which
adds portable answer/evidence copy and immutable per-answer transcript context.
Its exact runtime export passed doctor discovery/import/registration and catalog
validation including dependency preparation; security scan reported SAFE. Two
initial dependency attempts failed on an upstream GitHub 504; the subsequent run
explicitly completed resolution and installation. A disposable host synchronization
warning remained, so this does not establish active packaged Desktop readiness.

Local Windows checks: 756 Python tests passed, 86 platform/optional-Hermes skips;
151 Desktop tests passed, 2 POSIX skips; 3 packaging checks passed. Independent
review reran all 19 snapshot unit/integration tests. The synthetic checks cover
exact provider-context equality, correction and retry behavior, clear/session
isolation, bounded retention and separate clipboard consent. Current hosted CI
runs are available in [Actions](https://github.com/adam4788/meeting-copilot/actions).

Backend timestamps remain uninterpreted, and a valid reference is not proof that
a model's claim is true. No new physical recording, live provider account test,
or successful packaged Windows activation is claimed by this build.

## Settings and lifecycle checkpoint: c5aa3cf (2026-11-08)

The installation guides now pin
`c5aa3cf85bd94ce7134925d0ebc0c1c5a17c9f12`.
[PR #8](https://github.com/adam4788/meeting-copilot/pull/8) moves answer
preferences into native plugin settings, hardens capture/error reporting, and
improves transcript/evidence export.
[PR #9](https://github.com/adam4788/meeting-copilot/pull/9) registers the host's
explicit disposer, releases owned/in-flight pane claims, and prevents retained
callbacks from reopening or using an unloaded plugin.

[All five main-commit CI jobs passed](https://github.com/adam4788/meeting-copilot/actions/runs/42535380563):
Windows/Linux Python 3.11 and 3.12, plus macOS tests and helper build/self-test.
The final PR-head checks passed too; failures on earlier PR revisions do not
describe this commit. The PR #9 synthetic checkpoint reports 886 Python tests
run (23 skipped) and 200 Desktop tests passed. Those checks exercise fixtures,
including host unload during synthetic backend capture, not a physical device
or a live provider account.

An independent macOS check on November 9 repeated 886 Python 3.11 tests
(23 skipped), all 200 Desktop tests under Node 22, and all three runtime-export
checks. The fake-provider walkthrough also completed, explicitly without a model
call. The clean source exported 61 runtime files. Doctor passed discovery,
manifest parsing, import and registration in a disposable Hermes home; it warned
that the conditional Windows-only SoundCard dependency was not installed on
macOS. The official `validate --install-deps` then completed dependency
resolution/installation and passed all admission checks; the security scan
reported safe. Validation reports isolated plugin-host mode with buffered
dashboard responses. This does not establish request-scoped BYOK handoff; the
[compatibility caveat](catalog-compatibility.md) still applies. No repair of the
active runtime was performed. The validated export is local and unpublished;
its source revision is the public pin above, not a separate catalog release.

Installation is a separate step. A machine may still have an older plugin even
when its Hermes application has been updated. Check the installed plugin against
the pinned source; matching files alone do not prove the running process loaded
them. No complete live meeting, Windows native capture, OpenAI/OpenRouter
response, or long-call reliability is established by this checkpoint.
