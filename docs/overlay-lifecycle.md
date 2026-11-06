# Overlay lifecycle and publication contract

## Identity and retirement

The macOS overlay is a read-only display of bounded transcript and question
snapshots. It cannot submit answers; use the Desktop pane. The backend rejects
clicks even from an older helper, retains no pane credential context, and sends
no provider keys to the native helper.

## Ordering limits and non-pane API compatibility

Pane claims, ordered actions, owner tokens and scope revisions prevent stale
panes from replacing a newer display. The helper protocol requires
`meeting-overlay/2`; rebuild an incompatible helper before showing it.

## Capture lease, Restart, and reload recovery

Only the owning pane's heartbeat extends the capture lease. Closing the pane
requests pause and overlay shutdown. A lost renderer is retired by the watchdog;
a new renderer cannot silently take over its active capture.

## Capture expiry and physical-stop uncertainty

Logical retirement is not proof that the audio device stopped. The pane reports
an unconfirmed stop, blocks a new capture, and keeps Retry stop available until
cleanup succeeds. Verify the OS audio indicator when shutdown is uncertain.

## Overlay process shutdown uncertainty

Hide and Clear retire display publication before waiting for process cleanup.
If shutdown cannot be confirmed, the window may still be visible; retry Hide and
verify it is gone before screen sharing. Capture exclusion is best-effort.

## Bounded controller liveness observation

Helper liveness probes and cleanup use bounded waits. An unknown result remains
unknown rather than being reported as a confirmed exit.

## Lock order

Lifecycle and display publication use ordered locks; potentially blocking pipe
writes and process cleanup run outside the publication path or have deadlines.

## Injected transport contract

Test transports may exercise generic snapshot and cancellation behavior.
Production `BackendTransport` never admits overlay answers.

## Verification

Run the Python suite and Desktop JavaScript tests in
[development](development.md). Synthetic tests cover read-only click rejection,
credential isolation, lease ownership, display retirement, and bounded cleanup.
Actual macOS overlay rendering and native capture require separate verification.

## Autonomous overlay retirement and process inspection

The watchdog retires abandoned displays. Process-group cleanup retains
unconfirmed handles until exit is observed.

### Unreaped-child / shared-sample follow-up

An unreaped or uninspectable helper is reported as uncertain, not cleanly
stopped. Retry Hide or Clear and verify the window has closed.
