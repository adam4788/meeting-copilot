# Optional Meta API route

Configure the optional **Meta API key** only in **Hermes Settings → Plugins →
Meeting Copilot**. The plugin declares `MODEL_API_KEY` as a secret and resolves it
through the bound Hermes `agent.secret_scope.get_secret` API. Missing or failed
host scope is unavailable; the plugin does not read credential files or borrow
an unscoped launch environment key.

Meta is an explicit optional API route for existing integrations, never the
answer default. Both provider `meta` and an exact supported model ID are required
(`muse-spark-1.3`, `muse-spark-1.2`, or `muse-spark-1.1`). The current desktop
provider list focuses on OpenAI and OpenRouter. Local faster-whisper transcription
needs no Meta key; optional cloud Meta ASR is a separate consented choice.

The old in-pane key editor, its save/remove/revision controls, and the plugin's
startup dotenv bridge have been retired to meet Hermes catalog secret-handling
rules. Existing Hermes-managed settings remain owned by Hermes. Reopen the pane
when you change them; finish existing requests and pause capture first. Already
admitted requests and workers retain their snapshots. No plugin control claims
to revoke an already submitted provider request.

See [BYOK setup and boundaries](byok.md) for the supported answer routes,
standalone process-environment mode, readiness limits and validation evidence.
Never paste a key into chat, an answer request or a model field. This change does
not create, rotate, delete, reveal or verify account keys.
