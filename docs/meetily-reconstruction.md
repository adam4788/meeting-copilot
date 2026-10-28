# Meetily 0.4.1: bounded reconstruction evidence

## Outcome and scope

The installed artifact contains static evidence of macOS-native audio capture, Whisper and Parakeet engines, model discovery, and permission helpers. Matching-version public source corroborates a separable capture → PCM → transcription-provider architecture. **An independent implementation need not launch, automate, link to, or read the private state of Meetily.** This is an architectural recommendation, not a tested replacement.

Only static inspection was performed. No Meetily executable or bundled helper was launched; no debugger, injection, recording, permission request, private database, installed model directory, or private configuration was used. No UI, asset, model weight, or binary was copied. Public source excerpts were downloaded into scratch for corroboration, not incorporated into implementation. This report is the only project file created by this investigation; no implementation or commits were made.

## Reproduction and retained evidence

The qualified `agent-plugin-rea-c4599aab:reverse-engineer-anything` skill and its native/artifact and connection/recovery references were consulted. The parent reported that REA MCP discovery returned no tools. The pinned CLI fallback worked without configuring Hermes or starting Ghidra. These commands actually ran and returned exit status 0; all three stderr files were empty:

```sh
mkdir -p $HOME/.hermes/cache/scratch/meeting-rea
npx -y rea-agents@6.7.0 inspect-macho /Applications/Meetily.app --json > $HOME/.hermes/cache/scratch/meeting-rea/macho.json 2> $HOME/.hermes/cache/scratch/meeting-rea/macho.stderr
npx -y rea-agents@6.7.0 inspect-rust-binary /Applications/Meetily.app/Contents/MacOS/meetily --json > $HOME/.hermes/cache/scratch/meeting-rea/rust.json 2> $HOME/.hermes/cache/scratch/meeting-rea/rust.stderr
npx -y rea-agents@6.7.0 inspect-plist /Applications/Meetily.app/Contents/Info.plist --json > $HOME/.hermes/cache/scratch/meeting-rea/plist.json 2> $HOME/.hermes/cache/scratch/meeting-rea/plist.stderr
```

These are complete CLI JSON records, not truncated terminal previews:

| Label | Complete local evidence | Actual REA evidence ID |
|---|---|---|
| M | [macho.json]($HOME/.hermes/cache/scratch/meeting-rea/macho.json) | `ev_7c20de66d37b39290d7be226eab1866cdec4b32092ef45072b18f10f2933ea15` |
| R | [rust.json]($HOME/.hermes/cache/scratch/meeting-rea/rust.json) | `ev_fcde7c9039487b2aaa90372ce35f2dd274003368356ae813dc5d0ab17e49e534` |
| P | [plist.json]($HOME/.hermes/cache/scratch/meeting-rea/plist.json) | `ev_300c649e9724425fa92f21e137ea070a55525b16f83586cdddb3f4def70fb112` |

[manifest.json]($HOME/.hermes/cache/scratch/meeting-rea/manifest.json) records evidence-file digests, sizes, subjects, limitations, and downloaded source-file digests. [artifact-first-findings.json]($HOME/.hermes/cache/scratch/meeting-rea/artifact-first-findings.json) was saved **before** fetching upstream source. It is a selected-symbol summary, not a new REA record or exhaustive symbol list. Scratch evidence is local and subject to cache pruning; archive it separately if durable audit retention is required.

### Artifact identity

- Requested bundle: `/Applications/Meetily.app`; executable: `/Applications/Meetily.app/Contents/MacOS/meetily`.
- Executable SHA-256: `4275f5f4ce8789ee776a79af2abb29be4b0da7fdf49460655f40b27cf4defaf1` (M and R; independently recomputed with Python hashlib).
- Executable size: 70,800,272 bytes (R).
- Bundle identifier `com.meetily.ai`; short and build versions `0.4.1` (P).
- Info.plist SHA-256: `7ae38d497e673442ef02c754acccb1c5f5201c7801fcc1aa4052db6a5c116a8a` (P).
- M/P canonicalize the bundle spelling to `/Applications/meetily.app`; R retains the supplied capitalization. The executable hashes agree. The SHA above identifies the executable, **not a digest of the whole app directory**.

## Artifact-first finding ledger

| Question | Observation from shipped artifact | Supported conclusion and boundary |
|---|---|---|
| Platform/runtime | M: thin arm64, little-endian, 64-bit Mach-O; UUID `30376064-8D1C-3039-A7F8-593325DB7454`. Dependencies include CoreAudio, AudioToolbox, AVFoundation, ScreenCaptureKit, Metal, CoreML, WebKit. | Native Apple audio and inference facilities are linked. Linkage alone does not prove a particular framework is used on every path. |
| System audio | M imports `_AudioHardwareCreateProcessTap`, `_AudioHardwareDestroyProcessTap`, `_AudioHardwareCreateAggregateDevice`, `_AudioHardwareDestroyAggregateDevice`. | Strong static evidence for a Core Audio tap/aggregate-device implementation; not an observed recording or proof of default backend. |
| Microphone | M symbol names include CPAL CoreAudio device enumeration and `Stream::new`. | CPAL/CoreAudio capture facilities are present. Routing, channel choice and successful permission grant remain unobserved. |
| Engines | M symbol names include `app_lib::whisper_engine`, `WhisperEngine::new_with_models_dir` at `0x1007470ec`, `whisper_rs::WhisperContext`, `app_lib::parakeet_engine`, `ParakeetEngine::new_with_models_dir` at `0x100681d1c`, and `ParakeetModel::transcribe_samples` at `0x10081080c`. | Both engine families are represented in shipped symbols. These addresses are static symbol addresses, not call traces or function-body analysis. |
| Model lifecycle | M has `ModelInfo`, `ActiveDownload`, `get_models_directory`, `WHISPER_ENGINE`, `PARAKEET_ENGINE`, and `discover_models_standalone` names. | Static evidence for model inventory/download/lifecycle concepts. Exact model catalog, validation policy and selected model require other evidence. No installed models were inspected. |
| Permission helpers | M names include `trigger_system_audio_permission` at `0x10075440c`, `check_screen_recording_permission` at `0x100754910`, and `request_screen_recording_permission` at `0x100754aa8`. P contains microphone, screen-capture and audio-capture usage descriptions. | Permission-related code and declarations exist. Neither names nor plist declarations establish that a check is truthful or permission is currently granted. |
| OS compatibility | M records minimum macOS `14.2`, SDK `26.5`; P declares `LSMinimumSystemVersion=10.13`. | Metadata disagrees. Do not derive a working OS-support promise from the plist alone. |
| Rust dependencies | R: no `.dep-v0` auditable dependency section; inferred rustc path hash `48a229ceaefd4985c50990b14116b6d856af0985` at file offset 45222404. | Dependency metadata is unavailable, **not an empty dependency graph**. The path hash does not authenticate compiler identity. |

M imports/exports explicitly report `exhaustive=false` and unknown totals. REA combines `nm` and `dyld_info`; stripped/hidden symbols may be absent. Only `otool` build metadata is normalized; `vtool` is retained as provenance. This investigation did not recover function bodies or a call graph. R examines dependency metadata/compiler-path strings, not behavior. Those limits apply even where a symbol name is suggestive.

## Matching public source: corroboration, not binary equivalence

After recording artifact findings, the public GitHub tag was fetched. [Tag API response]($HOME/.hermes/cache/scratch/meeting-rea/upstream-tag.json) identifies `v0.4.1` directly as commit `a2cb62e827da7ef59f65064c97233efb2313878e`. The [recursive tree]($HOME/.hermes/cache/scratch/meeting-rea/upstream-tree.json) reports `truncated=false`. Downloaded files are under [upstream/]($HOME/.hermes/cache/scratch/meeting-rea/upstream/). This establishes matching declared version, **not a reproducible-build match** to the installed executable.

All source URLs below pin that commit. Details in this section are source observations unless explicitly tied to M/P above.

### Capture and permissions

- [capture/core_audio.rs, lines 55–150 and 203–238](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/capture/core_audio.rs#L55-L150): obtains the default output device, creates a mono global process tap excluding no processes, and describes a private aggregate device containing the tap. It deliberately avoids including the same output as a separate subdevice to prevent duplicate audio. The aggregate device is started through an IO callback. This corroborates M's process-tap imports; it does not establish actual active routing on this machine.
- [capture/backend_config.rs, lines 7–83](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/capture/backend_config.rs#L7-L83): advertises ScreenCaptureKit and CoreAudio; the executable `default()` branch returns **CoreAudio on macOS**, despite an older comment describing ScreenCaptureKit as default. [stream.rs](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/stream.rs#L60-L105) selects direct CoreAudio for system devices with that backend; other cases use CPAL. Source declarations do not prove all alternative backends work.
- [Cargo.toml](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/Cargo.toml#L73-L74) declares CPAL `0.15.3`; macOS dependencies include cidre pinned to revision `a9587fa`. These are **source manifest versions**, not a recovered shipped dependency bill of materials.
- [permissions.rs, lines 8–25 and 92–124](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/permissions.rs#L8-L25): `check_screen_recording_permission()` unconditionally returns true on macOS. The tap-creation probe admits that creation can succeed while permission denial yields silence. Do **not** reuse this as an authoritative permission test. Its comments associate CoreAudio audio-capture permission with macOS 14.4+, another reason not to promise compatibility solely from the bundle metadata.

### Transcription, models and selection

- [transcription/provider.rs, lines 40–73](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/transcription/provider.rs#L40-L73): defines a provider interface accepting **16 kHz mono float32 PCM**, an optional language hint, and returning text, optional confidence and partial/final status. Loaded-state and current-model queries are separate. This boundary can be independently designed without Meetily's UI/Tauri infrastructure.
- [transcription/engine.rs, lines 54–145](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/transcription/engine.rs#L54-L145): local-recording validation recognizes `localWhisper` and `parakeet`, initializing and validating the appropriate engine. Missing/error configuration defaults to Parakeet. Unknown providers are rejected in this validation path; do not generalize this to every application feature. [recording_commands.rs](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/recording_commands.rs#L332-L335) calls model validation before starting recording.
- [config.rs, lines 6–12](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/config.rs#L6-L12): default model constants are Whisper `large-v3-turbo` and Parakeet `parakeet-tdt-0.6b-v3-int8`. These are public defaults, not the user's installed selection.
- [whisper_engine/whisper_engine.rs, lines 1073–1102](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/whisper_engine/whisper_engine.rs#L1073-L1102): downloader maps tiny/base/small/medium/large-v3-turbo/large-v3 and quantized variants to `ggml-*.bin` artifacts in the public ggerganov/whisper.cpp repository. [macOS Cargo dependencies](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/Cargo.toml#L173-L179) declare whisper-rs `0.13.2` with raw-api, Metal and CoreML features. Actual acceleration/performance was not measured.
- [parakeet_engine/parakeet_engine.rs, lines 106–139](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/parakeet_engine/parakeet_engine.rs#L106-L139): catalog contains v3-int8 and v2-int8, each requiring encoder, decoder/joint, `nemo128.onnx`, and vocabulary artifacts. V3 points to a Meetily-owned model host; V2 to a pinned public Hugging Face conversion. These URLs were **read from source, not downloaded or authenticated as model artifacts**.
- [parakeet_engine/model.rs, lines 64–131](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/parakeet_engine/model.rs#L64-L131): constructs ONNX Runtime sessions for preprocessing, encoder and decoder/joint; explicitly selects CPU execution and supports quantized filenames. This is not evidence of Parakeet Metal acceleration or use of sherpa-onnx.
- [useTranscriptionModels.ts, lines 44–103](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src/hooks/useTranscriptionModels.ts#L44-L103): import/retranscription selection merges models with `Available` status from both engines. It maps configured `localWhisper` to list provider `whisper`, matches provider + model, otherwise selects the first available, while preserving a manual choice. This is a specific selection hook, not proof of all settings UX.
- [whisper_engine/commands.rs, lines 280–379](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/whisper_engine/commands.rs#L280-L379): validation can immediately accept an already loaded model; otherwise discovers available files, prefers the configured model and falls back to first available. Do not copy that fallback blindly: explicit user intent should not silently become a different model.

### License boundary

The pinned repository's [LICENSE.md](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/LICENSE.md) is **MIT, Copyright (c) 2024 Zackriya Solutions**. Copies/substantial portions require preservation of copyright and permission notices. This report references source behavior and does not embed implementation. MIT on the repository does not automatically license every bundled dependency, model weight/conversion, icon or trademark. Audit those separately before redistribution; no model-license conclusions were established here.

## Independent implementation recommendations (design, not observed behavior)

1. **Separate capture, transcription, and summarization/model selection.** Define your own PCM/segment contract, provider capabilities, explicit provider/model IDs and error types. Hermes' conversational model need not be the audio recognizer. Do not call Meetily Tauri commands, sidecars or databases.
2. **Start with explicit audio-file transcription; make live capture an independently testable module.** FFmpeg can decode files, but its presence is not proof that stock FFmpeg captures macOS system playback. For live system sound choose and test a native CoreAudio tap or ScreenCaptureKit helper under your own application identity; microphone capture is a separate input. Do not require Meetily or its helpers to be installed.
3. **Permission semantics must be honest.** Declare relevant usage strings in the actual capture executable/app and test which signed host macOS attributes permission to. Keep unknown/denied/granted distinct. Request only on an explicit user action. Successful tap creation, device enumeration or silence alone cannot establish permission. Do not copy the unconditional-true check.
4. **Own the model catalog and files.** Persist an explicit engine/model selection in plugin-owned configuration; use fresh user-authorized public model downloads or explicit user-supplied paths. Pin revision, digest, engine format, language support and license. Never inspect Meetily's private model settings or depend on its model host for an independent distribution.
5. **Fail closed on selection mismatch.** Check installed/readable/format-compatible/loadable state before capture, and expose actual loaded model. Make fallback explicit, not silent. Separate catalog availability, successful loading and recognition quality.
6. **Bound buffers and capture scope.** Timestamp microphone/system tracks separately; resample deliberately to the recognizer's format; handle device/sample-rate changes, cancellation, shutdown and duplicate/echo paths. Prefer bounded queues over the unbounded forwarding channel found in `capture/system.rs`. Avoid global system-audio capture unless the user explicitly chooses it; exclude the assistant's own playback where feasible.
7. **Verify independently before claiming parity.** Test synthetic/file PCM fixtures first, then permission-denied/unknown cases, missing/corrupt models, provider switching, and shutdown. Any later live capture needs user authorization. Benchmark actual language coverage, latency, resource use and transcript quality rather than inheriting source marketing descriptions.

## Coverage and residual unknowns

- Artifact identity, static linkage/symbol evidence, plist declarations, and matching public-source architecture: **answered within static scope**.
- Capture routing, model-selection defaults and provider formats: **source-corroborated; not verified as shipped runtime behavior**.
- Installed user's selected/available models, granted TCC permissions, app-private state: **intentionally not inspected**.
- Exact shipped dependency versions, compiler identity, source-to-binary equivalence and helper contents: **unresolved**; R provides no auditable dependency graph, and helpers were not analyzed.
- End-to-end recording/transcription, network/offline guarantees, accuracy, performance, OS compatibility and independent plugin functionality: **not tested**. No target execution or Ghidra import was necessary for this bounded report.
