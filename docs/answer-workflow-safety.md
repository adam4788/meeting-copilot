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
with fixed guidance. Provider prose alone is not evidence of HTTP 401, so the
pane does not promise authentication-specific diagnosis.
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

## Follow-up blocker fixes (base `29777f5`)

- Recheck resets submission-failure and progress-pause flags independently and
  restarts GET polling even after failed Cancel. It never replays a POST.
- Lost submission responses gate new answer intents. Legacy missing-slot 404
  plus Cancel 404 has an explicit **Reset local answer tracking** escape hatch.
  It requires confirmation acknowledging that the old request may be running
  or complete and a later ask may duplicate context. Reset itself neither sends
  nor cancels anything. Declining confirmation preserves the gate.
- Real KnowledgeEngine model/search/history diagnostics use fixed messages,
  including exception-class names and web warnings on otherwise successful
  answers. Status/progress/history and captured output are checked together.
- Question/public-query editors cap input; outgoing answer fields are validated
  before POST. Oversized saved snapshots/identifiers are rejected locally, not
  truncated into a different replay or route. The router returns a fixed 422
  response without Pydantic input, location, parser context, or exception text.
  Tests cover malformed JSON, invalid types and lengths through actual FastAPI
  plus the Desktop-shaped `HTTP 422 <body>` error representation.
- Status identifies the displayed error source and omits stale answer retry
  context when capture/stop/overlay errors own the alert. Capture recovery cannot
  accidentally replay that old context.
- Verified terminal progress stays visible when status refetch fails. Tests use
  real TanStack QueryClient 5.104.1 (with React/ReactDOM 19.3.0, jsdom 29.1.1),
  confirm its default invalidate-on-error resolution, and inspect HTML attributes
  as well as text for synthetic diagnostic leaks. Successful reconciliation
  relinquishes the fallback to newer canonical results; Clear removes it.
- The offline bridge rejects outstanding RPCs on child exit. The matrix also
  requires an explicit assertion/cleanup completion marker, preventing an
  unresolved Promise and crashed child from falsely passing with exit zero.

Verification of these changes:

- Deterministic RED cases reproduced Cancel/Recheck, ambiguous second click,
  missing legacy reset, wrong capture retry, unbounded question/422 reflection,
  and terminal invisibility. Running the real QueryClient case against the
  original `29777f5` UI failed the terminal-visibility assertion; current UI passed.
- `python -m unittest test_answer_safety test_answer_core test_knowledge test_model_selection`:
  **292 tests passed**, including redaction and actual engine/router checks.
- `MEETING_REACT_TEST_MODULES=/path/to/existing/node_modules/ node test_real_effects_matrix.cjs`:
  **104 scenarios passed**, normal effects and StrictMode, retaining the prior
  lifecycle, credential, overlay, cancellation and exact-replay scenarios.
- Full `npm test`: **724 Python tests, OK (3 existing skips); 134 Node tests
  passed**, JavaScript syntax check passed.
- Scratch-only Swift compilation followed by `--self-test`: **overlay state
  tests passed**; `--protocol-version`: **meeting-overlay/2**.
- `git diff --check`: passed. No dependency installation, live Desktop/provider,
  real secrets/audio, push, or merge. Legacy cases use the current router's
  real legacy HTTP paths with opt-ins stripped, not an installed old backend.
