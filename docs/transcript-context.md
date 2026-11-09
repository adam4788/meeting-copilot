# Frozen transcript context

For a new pane answer based on live segments, admission freezes at most 40 recent
segments. Text and metadata are sanitized before the exact serialized provider
context is bounded to 6,000 characters. A long newest segment may be explicitly
shortened; older segments are omitted whole. A fresh snapshot ID and digest bind
references to that copy. Correcting a Meetily row cannot rewrite an old answer's
context. This is recent context, not a complete meeting record.

The model receives the snapshot JSON unchanged as its transcript context, alongside
the question and retrieved evidence. Optional automatic web planning uses this
same frozen window, with its additional path redaction; it never receives a
different slice of the live transcript. The answer's **Transcript context sent with
this answer** section displays the same retained excerpts. Recent answers show
their own retained context and evidence while available, rather than the newest
answer's context. Completed and partial streamed answers carry their own
snapshot. Numeric document citations and transcript references such as `[T1]`
are separate. Unknown transcript references trigger a warning even when no
snapshot was retained; a valid reference does not verify that the model's claim
follows from it.

## Times and identities

Backend timestamps are deliberately uninterpreted. Depending on capture route they
can be chunk starts or ASR receipt times; Meetily's `audio_start_time` has its own
semantics. The plugin does not convert these into utterance times, playback offsets
or UTC claims. Missing, invalid and unknown-source timestamps remain absent.
An ingestion fallback clock is not exported as a capture timestamp. Backend IDs
can be missing; per-answer references are not invented stable backend IDs.

## Retry and retention

Retry references the server-owned snapshot and checks the original question,
transcript, source scope, model choice and current generation before reusing it.
Changed or expired replay context fails before provider submission. Legacy/plain
text requests have no mapped segment provenance. There are at most eight retained
request slots; their snapshots are evicted together. Clear and new meeting retire
snapshots and prevent late workers from publishing old context. A provider request
already sent cannot be recalled.

## Explicit export

**Copy answer + evidence** still excludes transcript snapshots. The separate
**Copy sent transcript context** action requires confirmation about confidential
meeting text, metadata and clipboard persistence. It copies the exact provider
transcript JSON, not a new live-buffer export, and makes no model call. Clipboard
history, synced devices and copies outside the app are not erased by Clear.

## Verification boundary

Synthetic tests cover exact provider payload equality, stream/completed/partial
responses, correction immutability, replay alteration refusal, clear during an
in-flight call, new-meeting isolation, bounded retention, missing IDs/times,
Unicode and truncation, production sanitization, clipboard refusal and exclusion
from ordinary evidence export. They do not verify real model attribution quality,
Desktop display, hardware capture, or real provider accounts. Sanitization is
best-effort defense in depth, not DLP.
