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
- **Configured for this status request**: the same trusted resolver used by Meta
  and ASR sees a plausibly nonempty key in this request's snapshot. This does not
  describe every running worker, validate credentials, or prove model access.
- **Not verified with Meta**: this editor does not make a billable request,
  authenticate against Meta, or test audio permissions. It never marks a key
  verified merely because saving worked.

Every successful edit is checked on disk, then the pane separately reads the
status endpoint back. If confirmation fails, recheck before retrying. Another
settings edit invalidates the pane's revision rather than being overwritten.

## Safely apply changes

The editor persists **only to disk**, but that does **not** mean restart-only activation:

| Consumer | Effect of saving/removing the file |
| --- | --- |
| New requests under the supported single-profile Hermes `_config_profile_scope` | The host reads a fresh scope; the saved file takes precedence over inherited process keys immediately. Removal can expose the inherited fallback. Host external/managed secret sources can override the file. |
| Existing scoped requests, Meta completion closures, running ASR workers | Keep their previously captured key. They are not hot-reloaded or revoked. |
| Fresh questions in an already shown overlay after a pane-editor save/remove | Reload the host credential scope inside the existing authenticated pane context, even when search/model settings are unchanged. Already-admitted answers retain their snapshots. No pane ownership, action sequence, or heartbeat is renewed. |
| Unscoped owner/process-based code | Continues using its process environment. A safe restart may be needed to load file changes; a nonempty inherited service key can still win at startup. |
| Foreign/multiplexed profiles | Never borrow the launch owner's key. Editing is unavailable in these scopes. Their host-owned precedence is not changed by this editor. |

No shared `os.environ` mutation, automatic restart or provider check occurs on edit.
Overlay admissions and lifecycle changes serialize with the edit. If the host
cannot reload the overlay's credential context, the overlay is retired rather
than accepting new questions with stale credentials. The editor distinguishes a
successful file write from that failure, and reports whether helper shutdown was
confirmed. Retry Hide and verify the old window is gone when shutdown is uncertain.
The same rebind/retirement runs after an uncertain partial write. Edits through
other host settings or external editors do not run this callback: Hide/Show the
overlay after those edits to obtain a new context.
**Removal is not revocation.** A previously loaded file key may also remain in the
process environment, even if it did not originate in a service secret manager.

Status uses `request_configured`, `activation` (`host-scoped` or `process`),
`process_configured`, and `process_differs_from_saved`, plus `saved`, `verified`
(always false), and an opaque `revision`. The process flags describe only the
environment, not an effective override of a bound request scope. There is no
`reload_required`/`runtime_override` assertion. The POST response still belongs
to its pre-write request scope; the pane's separate GET reads a fresh snapshot.

1. **Before editing**, pause capture and wait for existing answers to finish.
   After editing, recheck status and start capture again to take a fresh snapshot.
2. For process-based code, use Hermes' gateway restart control when needed.
   For a CLI-managed service, `hermes gateway restart` must target that same
   active profile/home. Coordinate with other active sessions first.
3. Reopen the pane and select **Recheck key status**.
4. If the backend has a different process-environment key, update/remove
   the value in the service environment or secret manager that supplies it.
   Saving does not erase that fallback, even when new scoped requests prefer the
   file. Removal or a restart may expose it. Never put keys in shell arguments.
5. Select **Meta** as the answer provider and try a non-sensitive question.
   Test Muse Voice separately with participant consent. A fallback answer does
   not verify Meta; a working text answer does not verify Muse Voice permission.

The existing `meta_models` launchd startup bridge reads a supported active-home
saved key when no nonempty process key exists and the host tokenizer is available;
`model_selection.selected_completion` uses that resolver for Meta, and audio
workers consume their startup environment.
Other host launch modes must load their normal Hermes environment on restart;
if the configured badge remains false, use the host's supported secret setup.
The editor does not bypass provider selection or add a competing runtime resolver.
Removal deletes only the saved `MODEL_API_KEY` assignments, not credentials in
other profiles, service environments, keychains, provider pools or already-running
workers. Other tools using this profile's `MODEL_API_KEY` can change on their next
scoped request, not only after restart.

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
  ancestors), malformed/ambiguous dotenv, wrong owner and permissive
  credential files fail closed. Native macOS extended ACLs on the home, lock or
  `.env`, including inheritable ACLs, are refused before any secret temporary
  file is opened. Ancestor allow ACLs are also refused; standard deny-only
  ancestor ACLs are permitted. Linux POSIX ACL xattrs are refused. Unsupported
  platforms and failed ACL inspection fail closed. The editor does not silently chmod an existing
  credential file or rewrite unrelated entries to repair them. Fix permissions
  through the host/administrator if needed. The `.env.lock` contains no secrets;
  owner-owned read-only sharing of that lock is allowed, write sharing is not.
- Files containing multiline values, BOMs, or any binding that python-dotenv and
  the **installed host tokenizer** interpret differently are unsupported. GET,
  Save and Remove report this without mutating the file, creating a lock, or
  staging secrets. Validation repeats under the lock and on the candidate. Each
  binding is compared independently so duplicates cannot hide a discrepancy.
  In particular, an embedded `MODEL_API_KEY=` inside an unrelated multiline
  value must never produce a successful removal. Refusal does not disable an
  already effective credential; manually review the file with the administrator.
  Do not use another editor as an automatic bypass for this warning.
- Updates reuse Hermes' cross-process `.env.lock`, preserve supported unrelated
  bytes (including blank lines, comments and CRLF), and stage its private temporary file +
  fsync writer inside an ACL-checked private directory beside `.env`. Only a
  checked candidate is atomically published. Existing mode, owner and group
  are retained. Custom extended attributes and file flags are explicitly refused,
  not discarded. macOS-generated `com.apple.provenance` is allowed only when the
  candidate matches the original exactly; a mismatch leaves the original intact.
  An opaque keyed revision covers the home and
  entire file; stale writes receive 409. A final disk read confirms the write.
  Concurrent Hermes writers cooperate with this lock. Arbitrary external editors
  ignoring it are not transactional; a last pre-write comparison detects ordinary
  conflicts, but cannot eliminate an adversarial same-user race.
- The pane compares the POST revision with its subsequent GET revision, not just
  the saved flag. A different writer between those requests produces a conflict
  notice, never confirmation that this pane's key was saved.
- Startup and editing both disable interpolation, but have different safety
  boundaries. The startup bridge compares the **complete ordered target binding
  history** from whole-file python-dotenv parsing with the installed host tokenizer
  on every physical line: source offset and exact unexpanded value, including all
  duplicates. It also checks the full-file host result. Benign unrelated multiline,
  escaped values or quoted unrelated names no longer block a safe target. Hidden
  target lines, quoted target names and shadowed mismatches still refuse startup
  seeding even if both final maps agree. Invalid parsing, BOMs and invalid UTF-8
  remain refusal cases. The editor remains stricter: any unsupported binding
  anywhere in the file blocks GET/Save/Remove before mutation.
- Missing host/tokenizer remains deliberately **fail-closed**; no standalone
  tokenizer approximation is shipped. A copied parser has no proven parity across
  installed host versions. Startup does not load the saved file in this case;
  a separately supplied nonempty process key remains usable. The owner endpoint
  returns `credential_tokenizer_unavailable` with fixed, actionable instructions;
  the pane translates that code into fixed text without echoing transport errors.
  Unconfigured Meta completion also reports the startup constraint. Upgrade Hermes
  and safely restart only after active sessions finish, or ask the administrator
  to supply `MODEL_API_KEY` through the service/secret-manager environment. Merely
  re-saving the file or restarting the same incompatible host cannot fix it.
- Empty/whitespace inherited keys
  do not block the launchd startup bridge; nonempty process keys retain precedence
  **only for that startup bridge/unscoped code**, not fresh host-scoped requests.
  Runtime Meta resolution uses Hermes' trusted `agent.secret_scope` API. Foreign
  scopes cannot borrow the launch key; missing scope on a routed request fails
  closed. ASR snapshots the scoped key before its thread starts and removes the
  gateway startup marker from the child environment to prevent launch-home reload.
  Owner-scoped requests retain the host's legitimate environment fallback.
  A host-built scope remains authoritative, including for files this bridge/editor
  refuses: this plugin does not rewrite or re-parse trusted scope snapshots during
  model/ASR requests. Refusing startup seeding or editing is not revocation of a
  key already supplied by the host. Manual administrator review is required.
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
private modes, real macOS ACL rejection, extended-metadata preservation/refusal,
symlinks/hardlinks, ambiguous multiline refusal, startup fail-closed, duplicate
assignments, stale/concurrent edits, save/remove and unchanged process environment.
When Hermes is installed, a clean child process additionally uses the **real host
lock/writer**, simulates startup, and exercises `selected_completion('meta', …)`
with a fake HTTP provider; Muse Voice handshake construction uses the same key.
That integration test skips on machines without Hermes, never on this verified host.

`python -m unittest test_credential_env -v` compares target histories against the
installed tokenizer, including same-valued shadow assignments, quoted names,
multiline targets, duplicate bindings, line endings and literal variable values.

`python -m unittest test_credential_runtime -v` uses fresh child processes and
synthetic profiles to check empty/nonempty startup precedence through fake Meta
model and ASR HTTP transports, trusted owner/foreign/unbound/multiplex scopes,
and scope retention through the actual ASR thread and subprocess boundary.

`python -m unittest test_credential_host_scope -v` uses the **installed**
`_config_profile_scope`, `secret_scope` tokenizer/resolver and host lock/writer,
not stub mappings. It checks refusal before any mutation, both parsers after a
successful removal, actual scoped Meta and ASR transports before/after save/remove
with empty/nonempty inherited environments, retained closures/worker snapshots,
and real foreign-profile scopes. Only provider transports and the owner principal
(normally supplied by authenticated host middleware) are synthetic.

`python -m unittest test_overlay_credentials -v` drives the actual merged router,
installed host scopes, `BackendTransport`, native-click admission and fake Meta
HTTP authorization. A gated existing answer keeps A; fresh overlay questions use
B after replacement, and after removal use only a legitimate inherited key or
make no provider call. Rebind failure closes admission, retains cleanup ownership,
and reports confirmed/unconfirmed shutdown. Partial-write failure also refreshes
the committed credentials without claiming the write succeeded.

`node --test --test-name-pattern='Meta key' test_desktop.mjs` exercises the real
uncompiled pane event handlers. One test connects them over private test-only
stdio to the actual ASGI credential routes and a real isolated private `.env`,
proving Save/Remove effects, readback and an actual interleaved second writer.
The installed-host variants use the real request scope/tokenizer/lock/writer;
portable variants substitute host seams. No unauthenticated listener or real provider is involved. The
React/SDK stand-ins test behavior, not browser visual acceptance. No live Meta
credential, installed plugin, user `.env` or gateway was touched.

### Key-flow regression receipt

The regressions were reproduced red before their fixes: native existing/inheritable
ACLs, multiline startup resurrection, empty/whitespace precedence, foreign scope
fallback, UI interleaving and label guidance. Follow-up red tests caught ASR scope
loss across its thread/child boundary and first-import owner-scope compatibility.

With a fresh isolated `HOME`/`HERMES_HOME`/`TMPDIR` and an allowlisted process
environment containing no live credentials:

- Follow-up regressions first failed on ambiguous-file acceptance, misleading
  status semantics, and UI activation guidance. A CRLF/blank-line preservation
  test also caught dotenv attaching blank lines to the removed assignment.
- Targeted `python -m unittest test_credentials test_credential_runtime
  test_credential_host_scope test_meta_models test_meta_asr test_macos_audio -v`:
  **90 tests passed, none skipped**.
- Targeted `node --test --test-name-pattern='Meta key' test_desktop.mjs`:
  **8 tests passed, none skipped**, including both installed-host pane variants.
- Full `npm test`: **588 Python tests, passed (3 Windows-only skips)**;
  JavaScript syntax check passed; **77 desktop tests, passed**.
- Real macOS ACL tests and the real Hermes lock/writer integration ran, not skipped.
- Provider HTTP/ASR transports were fake. No live provider, device capture,
  gateway restart, deployment installation, push or merge was performed.

ACLs/custom metadata and differing provenance remain explicit refusal cases, not
silent repair. Visual browser acceptance and live provider credentials are outside
this isolated test receipt.

### Startup availability follow-up

The benign unrelated-multiline model/ASR startup regression and missing-tokenizer
API/UI guidance tests were observed failing before their fixes. The target-history
matrix also covers hidden assignments followed by identical or different final
values; these never seed the process key. Real host-scoped requests remain governed
by the host's snapshot, while the editor refuses those files without creating a lock.

Full isolated `npm test` after this follow-up: **599 Python tests passed (3
Windows-only skips)**, JavaScript syntax check passed, **79 desktop tests passed**.
No live credentials, provider calls, installs, pushes or merges were used.
