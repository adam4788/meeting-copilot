# Meta API key configuration

## In the pane

Open **Meeting Copilot → Meta API key**. Paste `MODEL_API_KEY` into the masked
field, not chat, and select **Save key**. Typing never saves. Replacing an existing
saved key and **Remove saved key** require confirmation. The draft clears as soon
as a write starts, including on failure; it is never stored in React Query,
localStorage, plugin storage, chat or notifications. There is no reveal button.

Three different facts are shown:

- **Saved in this profile**: a value exists in the backend's active
  `$HERMES_HOME/.env` (default `~/.hermes/.env`).
- **Configured in running backend**: the backend process currently has a
  plausibly nonempty key. This is not proof of valid credentials or model access.
- **Not verified with Meta**: this editor does not make a billable request,
  authenticate against Meta, or test audio permissions. It never marks a key
  verified merely because saving worked.

Every successful edit is checked on disk, then the pane separately reads the
status endpoint back. If confirmation fails, recheck before retrying. Another
settings edit invalidates the pane's revision rather than being overwritten.

## Safely apply changes

This editor is deliberately **disk-only**. Existing Meta answer closures and
Muse Voice/audio workers can retain their loaded key. We do not mutate shared
`os.environ`, automatically restart the gateway, or promise immediate readiness.

1. Pause capture; wait for answers and other gateway sessions to finish.
2. Use Hermes' gateway restart control for the connection/profile you are using.
   For a CLI-managed service, `hermes gateway restart` must target that same
   active profile/home. Coordinate with other active sessions first.
3. Reopen the pane and select **Recheck key status**.
4. If the backend still has a different process-environment key, update/remove
   the value in the service environment or secret manager that supplies it.
   Saving/removing the file does not override an inherited process key, and a
   restart may continue to inherit it. Never put a key in shell command arguments.
5. Select **Meta** as the answer provider and try a non-sensitive question.
   Test Muse Voice separately with participant consent. A fallback answer does
   not verify Meta; a working text answer does not verify Muse Voice permission.

The existing `meta_models` launchd startup bridge reads the active home's saved
key when no nonempty process key exists; `model_selection.selected_completion`
uses that resolver for Meta, and audio workers consume their startup environment.
Other host launch modes must load their normal Hermes environment on restart;
if the configured badge remains false, use the host's supported secret setup.
The editor does not bypass provider selection or add a competing runtime resolver.
Removal deletes only the saved `MODEL_API_KEY` assignments, not credentials in
other profiles, service environments, keychains, provider pools or already-running
workers. Other tools using this profile's `MODEL_API_KEY` are affected after reload.

## Existing Hermes support and compatibility

Hermes already supports `config_schema` fields with `type: secret` and
`env: MODEL_API_KEY`. These use the existing `.env` credential lifecycle rather
than writing secrets into `config.yaml`. This plugin declares that field, so
**Settings → Plugins → Meeting Copilot** is also available on supporting hosts.
The installer includes the declaration and `credentials.py` in its artifact.

The [Desktop SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk)
provides `ctx.rest` scoped to the plugin API, not a portable secret-store SDK.
The pane therefore uses a key-specific, owner-only wrapper around the host's
existing `.env` lock and atomic writer. It does **not** call the broad `/api/env`
listing/reveal endpoints, publish environment values, or maintain a new vault.
The current host implementation was inspected at `hermes_cli/plugins_settings.py`,
`hermes_cli/config.py`, `hermes_cli/dashboard_auth/native_http.py`, and
`hermes_cli/plugin_host_http_auth.py`. Host private helper compatibility is checked
at request time; missing helpers fail closed. This is intentionally stricter than
merely satisfying the manifest's minimum Hermes version.

## Trust boundary

- Editing/status require a verified **native Desktop same-OS-user owner principal**
  on the request, attached by Hermes' authentication middleware. A loopback address,
  forged header, ordinary dashboard session, token-authenticated tenant, or hosted
  authentication boolean is not accepted. The plugin never registers a public route.
- Only the current process's own active home is editable. Routed/multiplexed
  profiles are refused rather than falling back to global environment credentials.
  No path, foreign profile, arbitrary environment name or provider URL is accepted.
  The SDK’s profile query selector is allowed only when the host resolves it to
  the already-authorized current home.
- Hosts using `plugins.isolation: host` currently forward general admission but
  not a native owner principal. Editing is **unavailable** there; use the built-in
  Settings secret field. We do not weaken authorization to make that mode work.
  Remote/shared dashboards and standalone demo servers cannot use this editor.
- `.env` must be a private, owner-owned regular file with one hardlink. Symlink,
  shared-writable directory ancestry (except root/owner-owned sticky temporary
  ancestors), malformed dotenv, wrong owner and permissive
  credential files fail closed. The editor does not silently chmod an existing
  credential file or rewrite unrelated entries to repair them. Fix permissions
  through the host/administrator if needed. The `.env.lock` contains no secrets;
  owner-owned read-only sharing of that lock is allowed, write sharing is not.
- Updates reuse Hermes' cross-process `.env.lock`, preserve unrelated bytes
  (including multiline values/comments), and use its adjacent private temporary
  file + fsync + atomic replacement. An opaque keyed revision covers the home and
  entire file; stale writes receive 409. A final disk read confirms the write.
  Concurrent Hermes writers cooperate with this lock. Arbitrary external editors
  ignoring it are not transactional; a last pre-write comparison detects ordinary
  conflicts, but cannot eliminate an adversarial same-user race.
- Bodies are bounded, JSON-only, explicitly validated without FastAPI's default
  input-echoing 422 responses; errors are fixed messages. GET/status return only
  flags and an opaque revision, not a key, suffix, or public key hash. Successful
  responses use `Cache-Control: no-store`. No request bodies or exceptions are
  logged by this module. Do not enable external HTTP body/IPC logging on secret
  endpoints. A trusted local host/renderer necessarily sees the submitted value in
  memory; this is not protection against compromised same-user code or devtools.
- No key goes in URLs, shell arguments, test snapshots or Git. JavaScript/Python
  string memory cannot be reliably zeroized; clearing UI references is not a
  secure-memory guarantee.

## Verification

`python -m unittest test_credentials -v` exercises synthetic keys in isolated
homes: authorization, malformed/bounded input, no reflection, failure handling,
private modes, symlinks/hardlinks, unrelated multiline preservation, duplicate
assignments, stale/concurrent edits, save/remove and process-env precedence.
When Hermes is installed, a clean child process additionally uses the **real host
lock/writer**, simulates startup, and exercises `selected_completion('meta', …)`
with a fake HTTP provider; Muse Voice handshake construction uses the same key.
That integration test skips on machines without Hermes, never on this verified host.

`node --test --test-name-pattern='Meta key' test_desktop.mjs` exercises the real
uncompiled pane event handlers. One test connects them over private test-only
stdio to the actual ASGI credential routes and a real isolated private `.env`,
proving Save/Remove effects and readback. Only host auth/locking seams are faked
there; no unauthenticated network listener or real provider is involved. The
React/SDK stand-ins test behavior, not browser visual acceptance. No live Meta
credential, installed plugin, user `.env` or gateway was touched.
