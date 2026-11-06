# Catalog preflight

The manifest declares Hermes Agent `>=0.22.7` and the dashboard's FastAPI
dependency with an upper bound. The compatibility floor has not been rechecked
against its oldest supported host in the current guide audit. Hermes must resolve
dependencies under its own core constraints. The plugin's manifest, dashboard,
desktop and proposed entry version are all 0.1.0.

Run in an isolated Hermes validation environment:

```sh
hermes plugins doctor /path/to/meeting-copilot --ci
hermes plugins validate /path/to/meeting-copilot --install-deps
```

Registration exposes no tools or hooks and does not open devices. Doctor's
successful registration is not a live dashboard, device or provider test.
Validate a clean export of the exact commit to be pinned as well.

The proposed upstream change is one file,
`plugin-catalog/meeting-copilot-live.yaml`, in NousResearch/hermes-agent. It must
contain a full 40-character SHA reachable in Adam's public repository. BYOK
source is published at `b1e9089eef0d55da3c724306b13e5d4479a1eef0`. Publication
is separate from admission: the actual release tree still needs independent
validation, a public pin, and review. No catalog PR is made by preflight.
A repository's own catalog folder does not list the plugin.

## Installation disclosures

- Explicit questions send bounded transcript/context and retrieved excerpts to
  the selected OpenAI or OpenRouter HTTPS API; optional explicit Meta routes
  also contact Meta. Model access, billing and retention are provider-specific.
  No live OpenAI/OpenRouter result has been verified yet.
- Capture requires consent and Start. Optional Meta transcription sends audio
  off-device; local Whisper uses an isolated Python audio runtime and model
  cache. Initial model preparation can download weights. Native helpers are
  compiled from reviewable Swift source locally; no compiled helpers are
  committed. Native frame loss remains an unresolved reliability limitation.
- Retrieval reads selected project text, and prior Hermes session SQLite data
  only when history is enabled. Web opt-in may send a public query through the
  host search dispatcher. These reads can extend outside plugin-owned data.
  Keep history excluded and web off for confidential project-only use.
- Native capture/overlay and audio workers are child processes. Local setup
  compiles/signs native source and installs bounded audio dependencies in a
  separate runtime. No capture starts at import, registration or validation.
- API secrets are declared in Hermes-managed plugin settings and obtained via
  its scoped secret API. The plugin does not edit credential stores. The
  separate standalone CLI explicitly accepts process environment credentials;
  it is not the hosted credential path.
- The initially proposed catalog platform is macOS, the device-tested path.
  Windows native capture remains experimental and needs its documented audio
  dependencies and hardware verification; they are not silently installed into
  Hermes by this macOS catalog manifest. Meetily attachment is optional.

See [compatibility cautions](catalog-compatibility.md): active private scope and
search imports remain version-sensitive, and development mutation/probe helpers
remain visible to scanners. Passing validation does not constitute maintainer
approval. No telemetry, self-update, credential changes or unattended recording
are introduced by this preflight.

Canonical requirements: [catalog submission](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
