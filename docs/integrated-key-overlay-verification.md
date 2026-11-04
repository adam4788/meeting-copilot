# Integrated key / overlay safety verification

Base: `8ab8b00` on `integration/meeting-copilot-key-overlay`; work confined to the
combined scratch worktree. No main-checkout edits, installation, push, live key,
live provider call, or device capture was part of this verification.

## Behavior pinned by regressions

- The installed-host fake-provider test first failed because a fresh native
  overlay question still authorized with A after saving B. It now verifies B,
  then no saved A after removal (including legitimate inherited-key fallback).
  A gated, already-admitted answer keeps A. Existing ASR snapshot tests also pass.
- Credential edits reuse the overlay's authenticated identity with a fresh host
  secret scope; they do not mint ownership or renew leases. Failed rebind retires
  the exact bridge and distinguishes saved-on-disk from confirmed/uncertain
  shutdown. An uncertain partial write still refreshes the committed credentials.
- A deterministic startup gate exposed a second stale-context path: Show reserved
  its bridge before the edit but published it afterward. The test failed before
  the pending-startup retirement fix. Late startup now cannot publish that bridge.
- UI tests exercise cached and delayed Meta capabilities, local-default Start
  payloads, persisted cloud selections, and cloud disclosure above Start.
  The unrelated “Transcription remains local” answer-setting claim is removed.
- Quick Show failure and screen-share warnings are visible outside closed details.
  A real-router UI test injects helper startup failure, checks HTTP 503 and the
  disclosed sanitized guidance, then verifies successful retry Show and Hide
  against backend state. Transport exception details are not reflected by the
  quick action. Key-write UI tests independently verify retirement guidance.

## Executed gates

- Final isolated `npm test`: **708 Python tests, OK (3 existing platform skips)**;
  JavaScript syntax check passed; **128 Node tests passed**.
- Focused installed-host credential/overlay/lease checks: **28 Python tests passed**;
  focused default/disclosure/quick-toggle/key-retirement UI checks: **9 passed**.
- Alternate-order investigation: `test_server` followed by `test_lease_fencing`
  passed **99 tests**. A full reverse-order run before the added pending-startup
  regression passed **707 tests (3 existing skips)**. The reported heartbeat
  flake was not reproduced; lease semantics and timing thresholds were unchanged.
- Managed-scratch Swift compile succeeded. `--self-test` printed
  `overlay state tests passed`; `--protocol-version` printed `meeting-overlay/2`.
- `git diff --check` passed. Added production lines were reviewed for secret
  reflection, ownership changes, lock ordering, and inherited-key revocation.

The full-suite receipts used fresh isolated HOME/HERMES_HOME/TMPDIR and an
allowlisted environment. Provider authorization was synthetic; the host scope,
lock/writer, merged router and overlay admission were real. UI coverage uses
React/SDK stand-ins and actual event/effect handlers, with private stdio to ASGI
for backend-effect checks; it is not browser visual or live macOS permission
acceptance. No new native window was opened by the Swift self-test.

Automatic credential rebind applies to the pane key editor. Other host settings
or external `.env` edits require Hide/Show to refresh a retained overlay context.
Existing saved provider values are honored; older storage has no provenance
marker distinguishing legacy automatic Meta selection from an explicit choice.
