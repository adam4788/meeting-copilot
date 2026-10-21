# Local Windows audio and transcription

`audio.py` is a standalone, opt-in fallback for Windows WASAPI microphone and
render-device loopback capture. It has no dependency on Hermes configuration or
plugin registration. No capture or model download occurs at import, construction,
or device enumeration. Obtain participants' consent and follow applicable policies
before capturing a meeting. Loopback includes **all audio routed to the selected
output**, not just the meeting application.

## Dependencies

Install into the interpreter that runs the application:

```bash
uv pip install --python "$(python -c 'import sys; print(sys.executable)')" soundcard numpy faster-whisper
```

The implementation was exercised on Windows with Python 3.14 and SoundCard 0.4.6.
SoundCard uses CFFI/WASAPI on Windows; it does not require PyAudioWPatch wheels.
Default model: faster-whisper `base.en`, CPU, int8, four CPU threads. Set
`model_size='small.en'` for the larger English model, or provide an existing local
model directory. The first explicit `warmup()` or `start()` may download weights
into the Hugging Face cache. Once cached, recognition runs locally. Startup never
captures while model loading is pending. This module sends neither audio nor
transcripts to a hosted transcription service.

## Exact interface

```python
from audio import AudioCapture

capture = AudioCapture(
    input_id=None,            # exact ID from devices()['inputs']; None = default mic
    output_id=None,           # exact ID from devices()['outputs']; None = default output
    model_size='base.en',
    chunk_seconds=5.0,        # 0.2–30 seconds; 5 recommended
    queue_size=4,             # 1–32 pending chunks, shared by both sources
)
devices = capture.devices()
# {'inputs': [{'id': str, 'name': str}, ...],
#  'outputs': [{'id': str, 'name': str}, ...],
#  'default_input': str | None, 'default_output': str | None}

# Explicit opt-in, optional: download/load without opening capture devices.
# capture.warmup()

capture.start(on_segment=print, on_status=print)
# ... user-controlled meeting session ...
capture.stop()               # always place in application shutdown/finally path
snapshot = capture.status()
```

`start(on_segment, on_status=None)` returns promptly; loading, capture, inference,
and capture-time callbacks run in background threads. `stop()` may invoke the
status callback on its calling thread. Callback handlers must be quick and
thread-safe; marshal UI changes onto the UI thread. `warmup()` is blocking and
returns the cached Whisper model. `backend` and `model_factory` constructor options
are dependency-injection hooks for unit tests, not required application settings.

Segments have exactly these fields:

```python
{'speaker': 'remote', 'text': 'Recognized speech.', 'timestamp': 1700000000.0}
```

- `speaker='remote'`: selected output's WASAPI loopback.
- `speaker='you'`: selected microphone. These labels are source attribution,
  **not speaker diarization**. Microphone leakage can duplicate remote speech.
- `timestamp`: Unix seconds at the start of the captured chunk, not emission time.

`status()` returns a fresh dictionary containing:

- `state`: `idle`, `starting`, `running`, `stopping`, `stopped`, or `error`.
- `running`: whether any tracked worker is still alive. During `starting` or
  `stopping`, this does not imply a device is actively recording.
- `error`: explicit exception/source description, or `None`.
- `callback_error`: last status-callback exception, or `None`.
- `queued`: pending chunks; `dropped`: oldest chunks evicted under backpressure.

A model, capture, or segment-callback failure stops both sources and reports
`state='error'`. A failed status callback is contained in `callback_error`, without
recursively invoking it. Device enumeration and explicit `warmup()` failures raise
to their caller; startup failures are reported asynchronously. Invalid callback
arguments raise `TypeError`; invalid queue/chunk bounds raise `ValueError`.
Duplicate start, including while an old worker is still stopping, raises
`RuntimeError`. Chosen IDs must match exactly; there is no silent fuzzy fallback.
Select the output used by Teams/Zoom, which may differ from the Windows default.
Bluetooth reconnects or device changes require stop, re-enumeration, and start.

## Buffering, latency, and shutdown

Both sources use shared-mode 16 kHz, all-channel capture in 100 ms reads. Channels
are averaged to mono in memory (avoiding SoundCard's Windows single-channel
capture issue). A single inference worker transcribes approximately five-second
chunks with English language, VAD, beam size one, and no prior-text conditioning.
This is chunked near-live recognition, not partial-token streaming. Chunk edges
can split words or sentences; no overlap/deduplication or echo cancellation is
implemented. Quiet audio is not evidence of successful speech recognition.

The bounded queue drops the oldest pending chunk rather than blocking recording
or allowing an unbounded backlog. A slow CPU can therefore omit speech; monitor
`dropped`. Stop discards queued audio and suppresses pending recognition results.
No captured audio is written to disk. No recording runs unless `start()` was
explicitly called.

`stop(timeout=3.0)` signals both capture threads, joins with a shared bounded
budget, drains pending chunks, and returns. A CTranslate2 inference, model download,
in-flight application callback, or stalled audio driver cannot be forcibly killed
inside a Python thread. In that case state remains `stopping` and `running=True`
until workers exit; poll `status()` and call `stop()` again to settle the state.
Do not restart until `running=False`. A callback already in progress may finish.
All ordinary recorder handles are closed by context-manager cleanup. Errors stay
visible after stop. This implementation does not promise a hard real-time driver
shutdown deadline.

## Verification

CI-safe unit tests do not enumerate devices, capture audio, or download models:

```bash
python -B -m unittest test_audio -v
```

Hardware integration tests are explicitly opt-in, Windows-only, and play audible
synthetic speech. They open the default microphone and output loopback, may
initially download `base.en`, and must not be run during a private conversation.
Set the scratch directory to a location **outside the repository**:

```bash
MEETING_COPILOT_AUDIO_TEST=1 MEETING_COPILOT_TEST_SCRATCH="/path/to/scratch" python -B -m unittest test_audio -v
```

In PowerShell, set `$env:MEETING_COPILOT_AUDIO_TEST='1'` and
`$env:MEETING_COPILOT_TEST_SCRATCH` before invoking the same unittest command.
Without an override, synthetic test artifacts use a temporary child of
`~/.cache/meeting-copilot-tests`, cleaned on test exit. Only generated Windows SAPI
speech is saved; microphone and loopback samples remain in memory. The integration
test checks direct recognition of generated speech, separately plays it through
the default output, and asserts a `remote` live recognition result before stop.

Verified on the development Windows host:

- Real microphone and loopback each returned 16,000 stereo frames in short capture.
- SAPI-generated speech recognized directly: “The project review is on Friday.
  Please explain the deployment plan and the next action items.”
- Separately played speech was recognized through real output loopback as a
  `remote` segment: “The project review is on Friday. Please explain that”.
- Both recorders stopped; final status had no error and no live workers.
- SoundCard emitted data-discontinuity warnings in an earlier manual run, although
  the subsequent opt-in integration suite passed. Such warnings can indicate
  missing samples; speech recognition is not claimed lossless.

These checks establish local synthetic-speech recognition and real loopback,
not accuracy in an actual meeting or microphone speech quality. No real speech
transcription from a meeting was claimed or retained.

## Meetily reference and possible bridge

[Meetily Community](https://github.com/Zackriya-Solutions/meeting-minutes) is an
existing local meeting transcription application. Its
[license](https://github.com/Zackriya-Solutions/meeting-minutes/blob/main/LICENSE.md)
is MIT (copyright Zackriya Solutions); retaining that notice is required if
copying substantial portions. No Meetily code was copied into this fallback.
The inspected [Cargo manifest](https://github.com/Zackriya-Solutions/meeting-minutes/blob/main/frontend/src-tauri/Cargo.toml)
uses Rust CPAL (with a pinned upstream patch), whisper-rs and Parakeet dependencies.
Its [Windows device layer](https://github.com/Zackriya-Solutions/meeting-minutes/blob/main/frontend/src-tauri/src/audio/devices/platform/windows.rs)
explicitly enumerates WASAPI outputs for loopback and WASAPI microphone inputs.
The files are references, not a claim that every legacy capture module is active.

For users with reliable Meetily transcription, the implemented [Meetily adapter](MEETILY.md)
reads an explicitly selected active recording's live JSON snapshots, avoiding a
second capture/recognition pipeline. It is an unofficial filesystem adapter, not
a supported external Meetily API. It has synthetic snapshot tests; a complete
installed-Meetily meeting has not yet been validated. Meetily independently saves
recordings/transcripts according to its settings; this fallback module's
no-raw-capture-storage contract does not change Meetily's storage behavior.
