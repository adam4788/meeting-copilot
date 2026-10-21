# Security and privacy

## Data flow

- Microphone and loopback audio are processed locally after explicit consent when using native capture.
- The preferred Meetily adapter reads only an explicitly attached active recording's live JSON snapshots. Meetily independently saves audio/transcripts according to its own settings; this project does not erase or change those files.
- This project does not write additional raw audio or transcripts to disk. Transcripts and
  answer results remain in bounded process memory until cleared or the process exits.
- A configured answer provider receives the question, bounded recent transcript,
  and retrieved source snippets. This may be a hosted provider. Local transcription
  is **not** a promise that answer generation stays local.
- Public search receives only the query explicitly entered by the user.
- Project/session access is read-only. No model tools, automatic speech, chat
  messages, or business-system writes are granted.
- The desktop integration uses the host's authenticated plugin API. Do not expose
  a development server or plugin API without authentication to a network.

## Limitations

Secret filtering and prompt instructions are defense in depth, not a guarantee.
Do not select a project containing material you are not authorized to send to the
configured model. Source snippets may contain inaccurate or adversarial text.
Session history records statements, not independently verified execution.

Clear meeting removes the application's displayed buffer/result; it cannot undo
an answer request already sent to a provider. Pause stops new capture but does
not erase the transcript. Closing the pane triggers pause, with a heartbeat
expiry fallback. Audio device changes may require restarting capture.

Use approved providers and obtain participant consent. There is no stealth mode,
recording-policy bypass, or screen-sharing invisibility guarantee.

## Reporting

For security-sensitive findings, use GitHub's private vulnerability reporting if
available rather than posting secrets or exploit details in a public issue.
Maintainer: [Adam Nugroho](https://github.com/adam4788).
