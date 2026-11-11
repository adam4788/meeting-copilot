# Native audio backpressure investigation

The consented physical microphone check produced two correct fictional turns
and confirmed Pause. Replaying audio after Pause added no segments. However,
the UI reported 20 native dropped frames (seven before playback), zero worker
chunk drops and zero sub-second tail drops. This is functional transcription
and stop evidence, **not loss-free capture**.

A device-free Swift harness using the production four-slot writer reproduced
the accounting at approximately 94 synthetic packets per second: no injected
writer stall produced zero drops; 100ms and 200ms writer stalls produced six
and sixteen reported drops respectively. Received plus dropped equaled attempted
packets. Initial reader stalls through 500ms produced no drops in that run;
pipe buffering provided additional headroom. These observations distinguish
writer scheduling pressure from loss-counter duplication. They do not identify
the cause of the live writer stall. Native queue bounds and loss reporting
remain unchanged.

Run `python -m tests.verify_native_backpressure` with a Swift toolchain for the
device-free regression probe. It extracts the writer functions verbatim from
the current production source, compiles them with Foundation only in a temporary
directory, and supplies synthetic zero PCM. No audio frameworks or Capture
class are included. Its five experiments assert packet conservation and force
the loss path; exact counts are scheduling-dependent. It is a development
check, not installed by `install.py`.

In the subsequent probe run, unstalled transport received all 169 packets;
100ms/200ms writer stalls reported six/fifteen drops. A 500ms initial reader
stall had no loss, while a 1.5s reader stall reported 73 drops. All totals
balanced exactly. A drop count alone therefore cannot distinguish writer
scheduling delay from a sufficiently long downstream read stall. Identifying
which caused the physical test still requires timing evidence from that path.

The native writer now measures monotonic maximum queue-wait, JSON-encoding and
write durations. A serial-queue snapshot is emitted only in the terminal helper
status. Local Whisper forwards only these three finite numeric millisecond
fields, capped at 60,000ms, through its stop summary/controller. Pause retains
them alongside the original loss counts. Unknown text/keys and invalid numbers
are discarded; these diagnostics include no PCM, transcript, wall-clock
timestamps, paths or credentials. The optional Meta transcription path is not
extended with these diagnostics yet. Forced termination can prevent a terminal
summary, so missing timings mean unknown, not zero delay.

Device-free experiments with the instrumented production writer separated the
injected signatures: a 200ms writer stall yielded about 205ms maximum queue wait
and 0.21ms write duration; a 1.5s reader stall yielded about 802ms queue wait and
810ms write duration. Packet conservation held in all five runs. Long writes
are consistent with downstream I/O blockage, but elapsed wall time can also
include descheduling. Queue delay can include preceding writes/encoding, not
only scheduler delay. These are useful timing evidence, not a causal verdict
about the physical run or a guarantee of loss-free capture. Queue capacity and
loss accounting remain unchanged. No instrumented physical capture occurred.

The investigation separately found avoidable allocation in the Python silence
detector: every small frame after four seconds concatenated the entire growing
utterance to inspect only its last 6,400 samples. It now copies at most those
6,400 samples. Tests cover small microphone-sized frames, arbitrary partitions,
the energy threshold, and the copy budget. The four-second earliest boundary,
400ms silence criterion and twelve-second bound are unchanged. This reduces
reader work; it does not establish that the observed native loss is fixed.

The investigation also found that the one-time `running` status competed for
one of the same four PCM slots. If those slots were full at startup, the helper
exited immediately before reporting either readiness or pending drops. The status
now has one separate bounded control slot. A deterministic device-free test
saturates all four PCM slots, verifies the status carries the three dropped frames,
and checks the real worker forwards that count once. This protects startup
reporting; it does **not** increase PCM capacity, prevent dropped frames, or
prove delivery through a permanently blocked pipe. The optional Meta worker now
also reports native loss deltas carried by status, error and PCM frames during
capture and in its stopped summary. This does not add writer timing diagnostics
to that path or establish loss-free cloud transcription.

Further physical tests require a freshly confirmed quiet, private environment.
Do not suppress drop warnings or treat a synthetic scheduling experiment as
native-device reliability verification. No new microphone permission was
accepted during this investigation.

## Bounded PCM aggregation and input diagnostics

A later consented Mac test produced no transcript and 610 native frame drops,
with no worker drops or reported capture error. Its stopped timing metadata
showed a maximum 622ms writer queue wait, 106ms JSON encoding time and 54ms write
time. These values establish transport congestion, not the reason recognition
produced no words. Device-free conversion checks did not reproduce zeroed audio.

The native converter now combines small hardware callbacks into at most 4,000
Float32 samples (250ms) per source before JSON encoding. Each source retains at
most one partial frame, and the writer still has four PCM slots. This reduces
per-callback serialization and scheduling overhead without an unbounded queue.
Graceful stop seals each converter under its lock, flushes its residual frame,
then drains the writer before emitting the terminal status. A rejected full or
residual frame is counted by the same loss mechanism. Native loss counts now
refer to these larger protocol frames, so raw counts are not comparable with
older small-callback counts and do not represent seconds of lost audio.

`tests.test_native_pcm_framing` extracts the actual framing/conversion code and
uses generated sample arrays only. It verifies both source labels, byte-level
sample preservation, timestamps, frame bounds, residual flush, post-retirement
rejection and accounting under sustained congestion. A deterministic blocked
writer accepts 40 small callbacks as two aggregated frames without loss. This
proves that specific congestion mitigation, not live speech accuracy or
loss-free recording. No device or permission API is used by the test.

The local worker also reports bounded numeric input and transcription counters.
They distinguish no received samples, zero-level input, and model chunks that
completed without words. Signal amplitude does not establish speech, microphone
quality or successful transcription. These diagnostics contain no audio or text.

## Explicit mono normalization for discrete layouts

A subsequent controlled run had zero transport drops but received only zero-valued
samples. Device-free tests reproduced a matching native failure: a valid
`DiscreteInOrder` channel layout containing nonzero PCM produced all-zero mono
from `AVAudioConverter`, despite successful conversion. Standard mono/stereo
fixtures had not exercised these hardware-style layouts. Enabling the converter's
`downmix` property did not correct the reproduced failure.

The converter now explicitly normalizes supported PCM into standard Float32 mono
at the original sample rate before resampling. It averages all channels, without
relying on channel labels or silently choosing only channel zero. Planar and
interleaved Float32, Float64, signed Int16 and signed Int32 are covered. Missing,
short, unsupported-layout/byte-format or nonfinite buffers fail visibly rather
than being treated as silence. Discrete two-, three- and four-channel tests
exercise every active input channel and verify the expected scaled nonzero output
through the real serialization path. Opposed-phase channels can still cancel
under arithmetic averaging; this is not a universal audio-quality guarantee.

This fixes the demonstrated offline conversion bug. The live stream's layout was
not inspected during recording, and microphone speech recognition still requires
a separately authorized verification after rebuilding the helper. No recording
or audio-setting change was made for this fix.
