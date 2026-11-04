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

## Follow-up: delayed admission, provenance, and failed-edit UI

Baseline for this follow-up: `fa85c265cafb9e5dc39530aac45d6ab4d8c4749b`.
The previous receipt above describes that baseline, not the final test totals.

### Reproduced before fixing

- All **10** critic interleavings failed on the old implementation: delayed
  scope or reused Show × replace/remove × empty/inherited process key; plus
  delayed first Show before any bridge reservation × replace/remove. Each gate
  is after the real host captures A and before state-method admission. A new
  native question reached the fake provider with A despite the completed edit.
- Missing/no-op fresh-scope helpers and cross-home transport rebinding have
  dedicated failing-before-fix cases. Refresh now constructs a new host scope
  inside a copied authenticated action context under overlay admission locking,
  pins the originating home, and fails closed on unavailable/unchanged scope.
  Editor-triggered refresh uses the retained pane context, not editor identity.
  Newer pane ownership fences delayed actions; already-admitted answers and
  running capture snapshots remain independent.
- Legacy persisted Meta without provenance migrated to local. New explicit
  selections carry `transcriptionChoiceVersion: 1`; proven choices survive
  unrelated edits. Unavailable saved cloud choices disable Start, Resume, and
  New meeting, with local-provider/key-setup guidance and cloud disclosure.
- Initial lifecycle/claim errors use fixed guidance, never transport text.
  Confirmed key-edit retirement immediately changes Hide to Show. A separate
  lifecycle notice survives failed key readback; failed writes retain sanitized
  retirement/shutdown fields and an allowlisted uncertainty code. Retry hide
  stays available until confirmed; subsequent confirmation clears the warning.

### Executed follow-up gates

- Final isolated `npm test`: **714 Python tests, OK (3 existing Windows skips)**,
  JavaScript syntax check, **134 Node tests passed**. Full receipt:
  `/Users/adamnugroho/.hermes/cache/scratch/combined-final-gate-w11198r2/npm-test.log`.
- Focused host-scope, delayed credential, overlay credential, and overlay
  revision suite: **25 tests passed**, including the 10-way matrix and fresh
  scope failure, retained-worker, inherited-key, and owner-fencing assertions.
- Actual React/ReactDOM + jsdom effects (including StrictMode) connected over
  private stdio to the real FastAPI router and installed host scopes. Normal,
  StrictMode, readback failure, committed-write/unconfirmed-shutdown failure,
  initial lifecycle failure, and initial claim failure passed. No React packages
  were installed; existing dependencies were loaded read-only. This is separate
  from the default dependency-free `npm test` command. Run with:
  `MEETING_REACT_TEST_MODULES=/path/to/existing/node_modules/ node test_real_effects.cjs`.
  Additional modes: `STRICT=1`, `READBACK_FAULT=1`, `WRITE_SHUTDOWN_FAULT=1`,
  `INITIAL_LIFECYCLE_FAULT=1`, or `INITIAL_LIFECYCLE_FAULT=claim`.
- Alternate-order heartbeat/lease/overlay investigation: reversed focused suite
  repeated three times, **96 tests passed**. The reported heartbeat flake was
  **not reproduced or claimed fixed**. The old test has a busy-loop pump without
  a join/finally cleanup, an inferred isolation risk rather than a demonstrated
  root cause; no unrelated lease thresholds or test timing were changed.
- Managed-scratch Swift compile and `--self-test`: `overlay state tests passed`;
  `--protocol-version`: `meeting-overlay/2`. `git diff --check` passed.

### Boundaries

All test credentials are synthetic; fake provider authorization is observed,
not live-provider authentication. Tests isolate HOME/HERMES_HOME/TMPDIR and use
an allowlisted environment; the real-effect backend refuses socket connections.
Native window rendering, hardware capture/permissions, screen-share exclusion,
and production deployment remain untested. No installs, main edits, pushes,
merges, live provider calls, or real credential changes were performed.

Fresh scoped publication depends on the installed host's home-stamped scope
API; unsupported or no-op refresh fails closed. Standalone unscoped transports
retain their existing process-context behavior. The native owner-authorized key
route is unchanged. Existing work may retain A; removal is not revocation.
External `.env` edits are not serialized by this editor's admission boundary:
Hide/Show to refresh the retained context after those edits. Previously manual
Meta choices without provenance are indistinguishable from automatic defaults
and intentionally require a new selection; no old bare Meta value is treated as
proof of cloud opt-in.
