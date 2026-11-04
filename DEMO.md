# Demo guide — Meeting Copilot (all-Meta stack)

Verified live on 2026-11-03 (macOS, real Meta API):

- **Transcription:** Meta Muse Voice (`muse-voice-transcribe-1.0`) —
  file mode and realtime WebSocket both transcribed a spoken test file;
  DIARIZATION returned `you · Speaker A` labels.
- **Answers:** Meta Muse Spark (`muse-spark-1.3`) answered from a
  diarized transcript ("Pricing: $84 per unit. Launch: Monday, Carol
  confirms tomorrow.").
- **Suite:** full test suite green (see docs/verification.md).

## Before the demo (2 minutes)

1. `MODEL_API_KEY` must be in the gateway environment (`~/.hermes/.env`);
   restart the gateway after adding it so the pane sees the capability.
2. Open the Meeting Copilot pane once and confirm the **Transcription
   provider** shows **Meta Muse Voice** (it auto-selects when a key is
   configured and no provider was saved). Answer provider stays on
   "Use Meta Muse Spark (default)".
3. Pick **Independent macOS transcription**, audio source **Microphone**
   (or **Microphone + system audio** for the GPT Live demo below).
4. Tick the **Permission check** in the Capture session card at the top.

## Demo 1 — Live conversation copilot (60 seconds)

1. Start listening. Say: *"The rollout ships on Monday, pricing stays at
   eighty-four dollars per unit, and Carol owns the launch checklist."*
2. Ask a second person (or a second voice): *"What did we decide on
   pricing?"* — then click **Suggest answer**.
3. Show the cited answer, the per-speaker colored transcript tiles, and
   the speaker legend. Toggle DIARIZATION to show speaker labels live.

## Demo 2 — GPT Live as the "other participant"

1. On a phone/second device, open a GPT Live (voice) conversation and
   brief it: *"You are a client on a sales call. Ask me about pricing,
   timeline, and who owns the launch."*
2. In the pane choose **Microphone + system audio** so the GPT voice is
   captured as the remote side; hold your questions to your mic.
3. When GPT asks something you want help with, leave the question blank
   and click **Suggest answer** — the copilot answers from the meeting
   transcript plus your project docs. The macOS overlay can show the
   question stream beside the call window.

## Demo 3 — Bring your own transcription API

1. Show **Transcript source → Transcription provider**: Local
   (faster-whisper, nothing leaves the Mac) vs **Meta Muse Voice**
   (cloud, explicit warning in the pane).
2. Switch providers mid-demo setup only while paused; Start resumes on
   the chosen provider. Clear restarts on the *same* provider/mode.

## Fallbacks

- No network/key: switch provider to **Local** — Whisper runs on-device.
- Meetily users: backend **Meetily** attaches to an existing recording.
