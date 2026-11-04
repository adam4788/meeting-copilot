# Answer workflow safety

## Wire contracts and compatibility

- `GET /assist/{id}?missing=sentinel-v1` returns HTTP 200 with exactly
  `{"contract":"answer-progress-v1","status":"missing"}` when the slot is gone.
  The pane stops tracking and explains that a meeting change/restart may have
  cleared the answer. Existing clients without the opt-in still receive HTTP 404.
- `POST /assist` may include `response_contract: "answer-admission-v1"`.
  Admission failures raised **before scheduling a worker** return HTTP 200 with
  `contract`, `accepted: false`, and one fixed reason: `invalid_request`, `busy`,
  or `capacity`. Successful admissions retain the existing response shape.
  Legacy clients retain existing HTTP errors. Worker/transport failures are not
  classified as definite non-delivery.
- The pane does not parse arbitrary error text or depend on `Error.status`.
  Desktop can reduce HTTP failures to plain Errors. Without a trusted envelope,
  submission is reported as unconfirmed, even when an older backend actually
  rejected it. No automatic POST/replay occurs.
- Three consecutive progress failures pause progress GETs while preserving
  tracking and any live partial answer. An explicit **Recheck status** resumes
  GETs, never resends context. This also bounds missing-slot polling against
  older backends whose HTTP 404 cannot be distinguished safely from transport
  errors. Such cases are described as uncertain, not definitely missing.

## Privacy and replay

Engine exception messages/classes are not published in answer/status/history or
logged by the exception handler. Failed provider-result diagnostics are replaced
with fixed guidance (the exact known authentication guidance is allowlisted).
Interrupted streams keep their partial answer and a fixed retry notice.

Status-query tooltips/alerts, failed status rechecks, Cancel, and answer transport
failures do not reflect exception text. A failed Cancel retains tracking because
cancellation may not have reached the backend. Cancellation and status refresh
are treated separately.

History, partial-answer, and failed-answer retries are disabled/guarded while an
answer is tracked or busy. Tracking changes only on accepted submission. A lost
replay response can reattach to the active slot from status even when the pane
still has a terminal entry from the preceding answer. Explicit retry clicks keep
the original request snapshot; status checks never replay a transcript.

## Verification receipt

Against the combined worktree based on `300e848`:

- RED reproductions: actual-router missing-slot polling with plain Desktop-shaped
  Errors; status tooltip/alert leak; engine exception leak; failed Cancel leak;
  enabled conflicting history replay; missing rejection guidance; unhandled
  status-retry rejection; lost replay response hiding an active stream.
- Baseline partial-stream exception-class disclosure was independently exercised
  using `git show 300e848:server.py`; the privacy assertion failed as expected.
- Final `npm test`: **720 Python tests, OK (3 platform/hardware skips)**;
  **134 Node tests passed**, JavaScript syntax check passed.
- Real React/ReactDOM + jsdom, production router, normal effects and StrictMode:
  **84 scenarios passed, 0 failed**. Includes real missing slots, legacy HTTP
  404/400 behavior, definite admission conflict, lost accepted replay, failed
  follow-up refresh, successful and failed engines, explicit retry, live-stream
  preservation, status errors, and cancellation transport failure.
- Scratch-only Swift compile/self-test: `overlay state tests passed`;
  protocol: `meeting-overlay/2`. No window opened.
- `git diff --check`: passed.

Reproduce the effect suite with existing dependencies only:

```sh
MEETING_REACT_TEST_MODULES=/path/to/existing/node_modules/ node test_real_effects_matrix.cjs
```

The effect bridge isolates HOME/HERMES_HOME, uses synthetic fixtures and fake
hardware, forbids socket connections, and throws plain Errors for HTTP failures.
Legacy compatibility scenarios strip the opt-in fields and use the router's
legacy behavior; they do not run a separately installed historical backend.
No live Desktop, real credentials, providers, audio, installation, push, or merge
was exercised. This is not a claim of live end-to-end verification.
