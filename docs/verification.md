# Verification

This records exercised checks, not promised features. The project is an early developer preview.

## Deterministic checks

`npm test` runs the Python unittest suite and desktop JavaScript syntax check. The local run exercised 53 tests, with three skips: two explicitly opt-in hardware checks and one Windows symlink-privilege check. The Windows junction test passed. GitHub CI independently runs the deterministic suite on Windows and Linux, Python 3.11 and 3.12; see the actual Actions run for current status.

Covered behaviors include capture consent, paused startup, bounded transcript memory, corrected-segment upserts, recording-switch isolation, discard of cleared in-flight answers, read-only sessions, best-effort secret exclusion/redaction, path traversal/reparse rejection, explicit-query-only web search, and serialized start/pause lifecycle operations.

An independent review found a start/pause race, incompatible terminal-status shapes, and a device endpoint instance-method error. Each was reproduced and fixed with regression tests before publication.

## Real Windows/runtime checks

- Microphone and loopback enumeration and short capture succeeded.
- Local Whisper transcribed synthetic Windows SAPI speech directly and through real speaker loopback. This is not multi-speaker meeting accuracy evidence.
- Runtime plugin validation passed discovery, manifest parsing, import, and registration.
- Installed status, projects, devices, and Meetily-recording listing endpoints returned HTTP 200 using profile-bound, one-shot native gateway tickets—the desktop's authentication protocol. No auth checks were disabled.
- Real prior-session retrieval returned matches without treating tool messages as proof.
- A real authenticated answer request returned a sourced answer in 2.991 seconds. A separate initial call took 10.882 seconds. Individual observations, not latency guarantees.
- An explicit public web query returned Microsoft documentation URLs and a sourced answer in 5.439 seconds. No transcript-derived query was sent.
- Test answers were cleared and capture was left paused.

## Not yet established

- A full real Teams or Zoom meeting with multiple speakers.
- Live growth from an active installed Meetily recording. The adapter has synthetic atomic-snapshot/correction/completion tests and local format inspection, not a completed live-meeting trial.
- Production latency/accuracy targets, a fully reviewed visual layout across screen sizes, or broad Windows audio-device compatibility.
- Hosted-provider or open-source-program acceptance guarantees.

Never publish private transcripts, project/session excerpts, native tickets, or recordings as evidence. Ordinary CI must not open audio devices or call a model/search provider.
