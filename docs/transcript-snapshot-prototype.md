# Transcript snapshot prototype (not active)

`transcript_snapshot.py` is a source-only, tested primitive. The plugin runtime
export deliberately does not include or use it yet. This is not a delivered
meeting-timestamp citation feature.

It freezes at most 40 recent segments, sanitizes text and metadata first, budgets
the exact provider JSON to 6,000 characters by default, and records a fresh
per-answer snapshot identity and digest. A long newest segment may be explicitly
shortened; older segments are omitted whole. References belong to this snapshot,
not to the live buffer or another meeting. Corrected Meetily rows create a new
snapshot while an old answer retains its original excerpt.

Native capture timestamps describe chunk starts, not exact utterance timing.
Meetily audio_start_time remains uninterpreted: no UTC conversion, playback link,
or claimed offset. Missing, invalid and unknown-source timestamps stay absent.
No reference establishes that a model statement is true or that a person agreed
to an action. Source IDs can be absent; synthetic references do not pretend to
be stable backend IDs. Sanitization is defense in depth, not DLP.

## Integration acceptance gates

1. Build inside the admission lock and bind the immutable snapshot to generation
   and request identity. Send `provider_json` unchanged, not through another
   truncation/cleaning stage. Verify the actual synthetic wire payload.
2. Completed and streamed responses retain the same snapshot, including partial
   results. Never attach a later buffer snapshot. Clear, session switch and
   cancellation must suppress late publication and release retained snapshots.
3. Retry must replay the server-owned snapshot with the original provider/model
   scope. Do not trust a caller-supplied snapshot ID to authorize another user's
   text. Old plain-text requests explicitly have no segment provenance.
4. Show transcript references separately from numbered document evidence. Accept
   references only within the answer's snapshot; flag unknown references, and
   never infer a commitment, owner, date or exact utterance time from a citation.
5. Export transcript excerpts only through a separately disclosed explicit action;
   existing Copy answer + evidence must continue to exclude hidden transcript
   snapshots. Tests must cover permission refusal and missing clipboard APIs.
6. Update runtime install/export allowlists and validate before activation.

Current tests cover metadata mutation/corrections, identical text across sessions,
missing IDs, invalid timestamps, Unicode and quoting, budget truncation, bounded
recent windows, production sanitization, and frozen dataclass mutation rejection.
They do not establish transport, live UI, retry, or lifecycle integration.
