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

The investigation separately found avoidable allocation in the Python silence
detector: every small frame after four seconds concatenated the entire growing
utterance to inspect only its last 6,400 samples. It now copies at most those
6,400 samples. Tests cover small microphone-sized frames, arbitrary partitions,
the energy threshold, and the copy budget. The four-second earliest boundary,
400ms silence criterion and twelve-second bound are unchanged. This reduces
reader work; it does not establish that the observed native loss is fixed.

Further physical tests require a freshly confirmed quiet, private environment.
Do not suppress drop warnings or treat a synthetic scheduling experiment as
native-device reliability verification. No new microphone permission was
accepted during this investigation.
