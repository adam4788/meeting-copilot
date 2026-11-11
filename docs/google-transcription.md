# Google transcription preview

This is a separate, explicitly selected **Gemini 3.5 Transcribe Live** route,
not a Gemini answer model. Local Whisper and saved Meta choices remain unchanged.
It uses the fixed model `gemini-3.5-transcribe-live` and Google's official
`generativelanguage.googleapis.com` Live WebSocket endpoint. No fallback, automatic
retry, automatic session renewal, custom URL, SDK default auth or ambient API key
is used. The existing reviewed `websockets >=15,<16` audio runtime is required.

## Setup and consent

Save a Google API key in the plugin's **Google Gemini transcription API key**
secret field in Hermes. Never paste it in chat, a model ID or a shell command.
Only the current host-bound profile's `MEETING_COPILOT_GEMINI_API_KEY` secret is
accepted. The host sends it once over the isolated worker's private stdin; the
worker uses the `x-goog-api-key` header, never a query string. Preflight checks
local dependencies, scope, presence and preview allowance, not Google account,
quota, billing tier or network access. Checks never call a provider.

Select Google explicitly, review the cloud notice, confirm participant permission,
and Start. Audio is sent off-device to Google. Google's free-tier data may be used
to improve products; paid-tier data is not, according to the current pricing page.
Use an appropriate paid project for private meetings. The plugin cannot verify
which billing tier a key uses. Existing Meta `DIARIZATION` preferences survive
switching to Google and back. Google always uses source labels: **you** means the
microphone source, **remote** means system audio, not identified people. It has no
speaker diarization or word timestamps. Interim events are never committed as
final transcript segments.

## Deliberately small preview allowance

- Up to **60 seconds of audio per source** and **75 seconds transport wall time**.
- Both sources means two independent streams, each counted.
- At most **four stream attempts** across profiles and releases for this OS user.
- Each planned stream atomically reserves **$1 of a shared $4 local allowance**
  before worker startup. Failed, cancelled or uncertain attempts are not refunded.
- There is no reset or extension endpoint. Exhaustion blocks new calls until a
  separately reviewed and authorized extension.
- Fixed limits also bound audio bytes, server events, message sizes, text output,
  reported tokens, connection setup and shutdown. No automatic retries.

The reservations are **not actual charges or a guaranteed provider bill cap**.
Current published rates are $3.50 per million audio input tokens and $21 per
million text output tokens, with a roughly $0.009/audio-minute estimate. Two
60-second sources are roughly $0.018 before variations. Server token/output
behavior and billing enforcement can differ; Google's own spend controls may lag.
The $4 reservation is intentionally conservative within the authorized shared
API budget, not permission for unlimited subsequent usage. Other API spending
must be accounted for separately.

Pause immediately stops native input, then allows a bounded final-event drain and
socket retirement. If retirement is unconfirmed, Start stays blocked. The dedicated
ASR protocol does not document an end-of-audio acknowledgement: preserved final
events do not prove a complete tail. A visible warning remains; last words may be
missing even after the connection closes successfully.

## Explicit synthetic WAV comparison API

`POST /transcription/google/test` accepts `consent: true`, `audio_base64`, and the
current lifecycle authority (`revision` + `epoch` from `/overlay/lifecycle`, or a
valid pane action). It accepts only a bounded PCM16 little-endian mono 16 kHz WAV,
1–60 seconds. It accepts **bytes, never a local path or URL**. Use generated,
nonpersonal fixture audio for development. There is no automatic test on mount.

This uses the same paced transport and scoped key, reserves one stream attempt,
and returns final segments plus bounded numeric usage in memory. It starts no
microphone, screen or system capture, inserts nothing into the meeting transcript,
and makes no answer request. The pane shows **Google audio test** with a no-device
notice while it runs. Start and overlapping tests are blocked. Pause, pane release,
lease expiry, credential changes and teardown can cancel and retire its exact
worker; no long network wait holds the lifecycle lock. If an HTTP client disappears,
its already transmitted data cannot be recalled, and the ordinary pane lease / hard
transport limits still apply. A confirmed worker stop is not a complete-tail proof.

The API does not perform speech-quality evaluation itself. Compare returned text
with the known fictional reference and report actual model errors, latency and
usage, separately from mocked protocol tests. No successful real Google test is
claimed merely because source tests pass.

## Primary references

- [Dedicated Live transcription contract](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe)
- [Pricing and data-use tiers](https://ai.google.dev/gemini-api/docs/pricing)
- [Billing and spend-control limitations](https://ai.google.dev/gemini-api/docs/billing#project-spend-caps)
- [Official header-authenticated Live client](https://github.com/googleapis/go-genai/blob/main/live.go)
