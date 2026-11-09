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

## Follow-up blocker fixes

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
- Terminal progress stays visible when status refetch fails. Successful
  reconciliation relinquishes the fallback to newer canonical results; Clear
  removes it. Historical real-React and QueryClient checks are in the record
  below; the current desktop suite uses stand-ins.
- The current offline bridge rejects outstanding RPCs on child exit.


Historical test receipts, including the retired credential-editor harness, are
available in the [original record](https://github.com/adam4788/meeting-copilot/blob/73a1ddc1fe30b766cd51a1b3e784dd65a8d005ba/docs/answer-workflow-safety.md).
