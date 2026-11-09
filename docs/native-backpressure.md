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
