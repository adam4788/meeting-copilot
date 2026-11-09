# Catalog preflight

Meeting Copilot is a standalone community plugin, not a Hermes core feature.
The proposed upstream change is exactly one file,
`plugin-catalog/meeting-copilot-live.yaml`, in NousResearch/hermes-agent.
Catalog admission remains a maintainer decision, not a result of this checklist.

## Source and release boundaries

The catalog pin must be a public, full 40-character SHA of the **runtime release**,
not a development checkout with a similar version. The release branch is
[`catalog/runtime-0.1.0`](https://github.com/adam4788/meeting-copilot/tree/catalog/runtime-0.1.0).
Its `SOURCE_REVISION` identifies the full development commit containing tests,
mutation probes and exporter source. Both trees remain reviewable.

The exporter uses an explicit runtime allowlist. It keeps source, entrypoints,
text guides, sample documents, license and the reviewed documentation assets;
it omits development tests and mutation probes. This is a packaging boundary,
not a scanner ignore. No binaries are shipped and no catalog pin self-updates.

The plugin manifest, dashboard manifest and Desktop plugin are version 0.1.0.
The declared host floor is `>=0.22.7`; capability absence must fail closed rather
than patching a host or borrowing credentials. API dependencies carry upper
bounds and are resolved under Hermes core constraints.

## Gates before the catalog PR

1. Run the source regression suite and native checks with synthetic fixtures.
2. Export from a clean, committed source checkout. Compare every runtime source
   file with the source revision, accounting for the README provenance notice.
3. Publish the runtime release and clone its exact public SHA into a clean folder.
4. In an isolated Hermes environment, run:

   ```sh
   hermes plugins doctor /path/to/exact-runtime-checkout --ci
   hermes plugins validate /path/to/exact-runtime-checkout --install-deps
   ```

5. Inspect every warning, then run the catalog's structural validator against
   the one-file entry. Match the entry's version, floor and capabilities to the
   pinned `plugin.yaml`. Record the public pin and actual results in the PR.

`--install-deps` can change the **validation environment**. Do not run it in an
active user's interpreter. Registration declares no tools or hooks and starts
no devices, model calls or workers. Doctor and validator success do not prove
live Desktop, provider, recording or transcription quality.

An earlier clean export of source
`32680e03db334a07dcb904cf63acbff2b8eb25cf` passed doctor and validation with no
warnings on upstream `6cff782b8d081a0ed382eaf75c432d452cd452cc` and the official
v0.22.7 checkout `b200f5bc`. That was preliminary evidence, **not validation of a
later release pin**. The catalog PR must cite the separately validated public
release. Current rules impose no commit-age waiting period.

## Installation disclosures

- Explicit questions send bounded transcript/context and retrieved excerpts to
  the selected OpenAI or OpenRouter HTTPS API. Optional explicit Meta routes
  also contact Meta. Model access, billing and retention are provider-specific;
  live OpenAI/OpenRouter account responses remain unverified.
- Capture requires consent and Start. Optional Meta transcription sends audio
  off-device. Local Whisper uses an isolated audio environment and model cache;
  initial model preparation may download weights.
- macOS setup requires preinstalled CPython 3.12, uv and Apple Command Line Tools
  with the macOS 26 SDK. It installs declared Python packages and compiles/signs
  reviewable native source. Automatic interpreter downloads are disabled.
- Capture, transcription and the overlay use child processes only after explicit
  actions. Setup does not start capture. There is no telemetry, self-update,
  credential-store modification or unattended recording.
- Retrieval reads selected project text outside plugin-owned storage and, only
  when enabled, eligible prior-session SQLite data. Web opt-in may send a public
  query through the profile-scoped host search dispatcher. Keep history and web
  excluded for confidential project-only use.
- Secrets stay in Hermes-managed settings and require a valid admitted request
  scope. Missing or incompatible scopes fail closed. Isolated-child and
  secondary-profile BYOK behavior remains limited; see
  [compatibility](catalog-compatibility.md).
- The proposed catalog platform is macOS. Native audio can lose frames and
  long-call reliability is unestablished. Windows source remains experimental;
  its hardware/Desktop path is not claimed by a macOS catalog listing.
- The overlay's recording exclusion is best effort. Hide it and verify it is gone
  when it must not appear. Clipboard exports require explicit action and may
  include confidential excerpts retained by clipboard history or sync.

The [native preview](assets/overlay-controls.png) uses only fictional strings;
[asset provenance](assets/README.md) identifies its platform and source. It is
not a live account, device or recorder-exclusion result.

Canonical references: [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins),
[public API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/public-api),
[Desktop SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk),
[catalog submission](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission),
and [admission rules](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md).
