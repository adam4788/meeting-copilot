# Verification

## Independent macOS implementation checks

The original Swift helper compiled and was ad-hoc signed with embedded microphone usage metadata. Its installed `--check-only` returned `capture_started=false`, microphone metadata present, sources `microphone/system/both`, and a 16 kHz PCM contract. This is not a permission grant or hardware-capture test.

A generated English speech WAV was decoded by the **installed isolated Python 3.12 faster-whisper CPU int8 worker using tiny.en**. It returned: “The meeting co-pilot transcribed speech locally, our launch review is scheduled for Friday.” The input was synthetic, not a real meeting. This verifies independent file decoding/model loading/transcription without Meetily; it does not establish recognition accuracy across voices or languages.

A real explicit `openai-codex` / `gpt-6.1-sol` route returned the requested public marker “Independent model routing works.” in 7.58 seconds including route setup. The provider catalog returned three authenticated provider entries; this does not establish that every listed model is accepted by its account. Model selection and fallback planning share a request-local completion function; the global chat configuration is not modified.

A direct Meta Model API route (`meta_models.py`, Muse Spark `muse-spark-1.3/1.2/1.1` via `https://api.meta.ai/v1/chat/completions`, stdlib HTTP, key from `MODEL_API_KEY` only) is covered by `test_meta_models.py`: allowlist validation, bearer auth, sanitized failures, catalog advertisement only when the key is present, default-to-Meta answer routing with `model_route` labeling, and visible fallback to the configured model when Meta is unreachable. No live Meta API call has been exercised yet; that requires a funded key and billing verification.

`hermes plugins validate` (catalog admission gate) passes on a pin-mirror tree: manifest, requires_hermes `>=0.22.0`, capability probe, no core overrides, desktop stays inside the SDK surface, security scan safe. Two local-only notes: validating the raw working tree flags vendored `.so` files inside the gitignored `.venv-audio/` (absent from any pin), and the pre-existing `agent.*` / `hermes_cli.*` / `tools.*` imports are reported as private-core-import warnings for the reviewer. The new Meta route itself uses only the Python standard library.

The backend adds consent-gated macOS start/pause, allowlisted audio sources/models, generation-scoped callbacks, and paused-state normalization. Tests cover stopped callback rejection, safe device enumeration without replacing active capture, independent model routing, concurrency and explicit-model failure without fallback. See the current test output for evolving counts.

**Not yet verified:** live independent microphone audio, ScreenCaptureKit system audio, combined capture, permission attribution/denial behavior on this installed host, long-call resource use, and visual acceptance of the new selectors. These require an explicit user-started recording rehearsal. Meetily-based live transcript ingestion is separate evidence, not proof of the independent capture path.


This records exercised checks, not promised features. The project is an early developer preview.

## Deterministic checks

`npm test` runs the Python unittest suite, desktop JavaScript syntax check, and Node pane-contract tests. Test totals evolve; use each run's printed counts rather than assuming a fixed suite size. Local Windows runs have three skips: two explicitly opt-in hardware checks and one symlink-privilege check. The Windows junction test passed. GitHub CI independently runs the deterministic suite on Windows and Linux, Python 3.11 and 3.12; see the actual Actions run for current status.

Covered behaviors include capture consent, paused startup, bounded transcript memory, corrected-segment upserts, recording-switch isolation, discard of cleared in-flight answers, read-only sessions, best-effort secret exclusion/redaction, path traversal/reparse rejection, bounded all-project retrieval, automatic fallback planning and fail-closed query checks, explicit-query precedence, UI opt-out/capability gating, and serialized start/pause lifecycle operations. Pane-contract tests use React/SDK stand-ins; they are not visual acceptance tests.

An independent review found a start/pause race, incompatible terminal-status shapes, and a device endpoint instance-method error. Each was reproduced and fixed with regression tests before publication.

## Real Windows/runtime checks

- Microphone and loopback enumeration and short capture succeeded.
- Local Whisper transcribed synthetic Windows SAPI speech directly and through real speaker loopback. This is not multi-speaker meeting accuracy evidence.
- Runtime plugin validation passed discovery, manifest parsing, import, and registration.
- Installed status, projects, devices, and Meetily-recording listing endpoints returned HTTP 200 using profile-bound, one-shot native gateway tickets—the desktop's authentication protocol. No auth checks were disabled.
- Real prior-session retrieval returned matches without treating tool messages as proof.
- A real authenticated answer request returned a sourced answer in 2.991 seconds. A separate initial call took 10.882 seconds. Individual observations, not latency guarantees.
- An explicit public web query returned Microsoft documentation URLs and a sourced answer in 5.439 seconds. No transcript-derived query was sent.
- Automatic web fallback was exercised with only a public Kubernetes probes question and empty synthetic project/session roots. It drafted a generic query, retrieved three web sources, and returned a cited answer in 18.357 seconds. The configured search provider failed authentication, but the host's built-in keyless rescue succeeded. This does not establish reliability of that configured provider or guarantee future fallback availability.
- Test answers were cleared and capture was left paused.

## Not yet established

- A full real Teams or Zoom meeting with multiple speakers.
- Live growth from an active installed Meetily recording. The adapter has synthetic atomic-snapshot/correction/completion tests and local format inspection, not a completed live-meeting trial.
- Production latency/accuracy targets, a fully reviewed visual layout across screen sizes, or broad Windows audio-device compatibility.
- Hosted-provider or open-source-program acceptance guarantees.

Never publish private transcripts, project/session excerpts, native tickets, or recordings as evidence. Ordinary CI must not open audio devices or call a model/search provider.
