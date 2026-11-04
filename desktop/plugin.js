import { createElement as h, useState, useEffect, useRef } from 'react';
import { host, useValue, useQuery, useQueryClient, Button, Input, Textarea, Badge, PALETTE_AREA } from '@hermes/plugin-sdk';

const ID = 'meeting-copilot-live';
const muted = { color: 'var(--ui-text-secondary)', fontSize: 12, lineHeight: 1.5 };
const card = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 14, minWidth: 0, boxSizing: 'border-box', background: 'var(--ui-bg-card)' };
const selectStyle = { padding: 8, borderRadius: 6, width: '100%', maxWidth: '100%', minWidth: 0, color: 'var(--ui-text-primary)', background: 'var(--ui-bg-card)', border: '1px solid var(--ui-stroke-secondary)' };

export default {
  id: ID,
  name: 'Meeting Copilot',
  description: 'Live answers grounded in your projects, prior sessions, and optional web research.',
  version: '0.2.0',
  register(ctx) {
    function MetaKeySettings({ api: paneApi, life, onOverlayTransition }) {
      // Bind callbacks to this exact effect lifecycle, not a mutable ref that
      // could authorize an old password-field callback after a pane remount.
      const api = (path, body) => life.mounted ? paneApi(path, body)
        : Promise.reject(new Error('This pane is retired. Close and reopen it.'));
      // Secret stays only in this uncontrolled password field and one POST.
      // Never send it through React Query, plugin storage, chat or notifications.
      const input = useRef(null);
      const inFlight = useRef(false);
      const [hasDraft, setHasDraft] = useState(false);
      const [info, setInfo] = useState(null);
      const [working, setWorking] = useState(false);
      const [notice, setNotice] = useState('');
      const [overlayNotice, setOverlayNotice] = useState('');
      const currentTransition = target => target && life.mounted && target.life === life
        && target.pane === life.pane && target.owner === life.owner && target.overlayGeneration === life.overlayGeneration;
      function reflectOverlay(written, target) {
        if (!currentTransition(target) || !written) return;
        if (written.overlay_credentials === 'retired') {
          if (onOverlayTransition?.(written, target) === false) return;
          setOverlayNotice({ kind: 'retired', target });
        } else if (written.overlay_credentials === 'refreshed') {
          setOverlayNotice({ kind: 'refreshed', target });
        }
      }
      const tokenizerNotice = error => typeof error?.message === 'string' && error.message.includes('credential_tokenizer_unavailable')
        ? 'Key settings unavailable: the Hermes host tokenizer is unavailable. The saved file is not loaded by the startup bridge. Upgrade Hermes and safely restart after active sessions finish, or ask your administrator to supply MODEL_API_KEY through the service environment. Never paste keys in chat.' : '';
      const erase = () => { if (input.current) input.current.value = ''; setHasDraft(false); };
      async function recheck() {
        if (inFlight.current) return;
        inFlight.current = true; setWorking(true);
        try {
          const current = await api('/credentials/meta');
          if (!current || typeof current.revision !== 'string') throw new Error('unsupported');
          setInfo(current); setNotice('');
        } catch (error) {
          setInfo(null);
          setNotice(tokenizerNotice(error) || 'Key settings unavailable: this connection, host, file permissions or credential syntax may be unsupported. Multiline or ambiguous files need manual host-administrator review; this editor will not repair them. Use the local Desktop owner connection.');
        } finally { inFlight.current = false; setWorking(false); }
      }
      useEffect(() => { recheck(); return () => { if (input.current) input.current.value = ''; }; }, []);
      async function change(action) {
        if (inFlight.current || !info?.revision) return;
        if (action === 'remove' && (typeof window === 'undefined' || !window.confirm || !window.confirm('Remove the saved Meta key from this profile? New scoped requests can change immediately or fall back to an inherited key. Existing requests and workers may retain their loaded key. Pause capture first.'))) return;
        if (action === 'save' && info.saved && (typeof window === 'undefined' || !window.confirm || !window.confirm('Replace this profile’s saved Meta key? New scoped requests, including other tools using MODEL_API_KEY, can change immediately. Existing requests and workers may retain their loaded key. Pause capture first.'))) return;
        const body = { action, revision: info.revision };
        if (action === 'save') {
          const value = input.current?.value || '';
          if (!/^[A-Za-z0-9._~+/=-]{8,4096}$/.test(value)) { setNotice('Enter an 8–4096 character API key using letters, digits or . _ ~ + / = - only; no spaces, quotes or line breaks.'); return; }
          body.key = value;
        }
        // A delayed edit/readback may describe an overlay the user replaced.
        const target = { life, pane: life.pane, owner: life.owner, overlayGeneration: life.overlayGeneration };
        erase(); inFlight.current = true; setWorking(true); setNotice('');
        try {
          const written = await api('/credentials/meta', body);
          delete body.key;
          reflectOverlay(written, target); // lifecycle outcome must survive failed readback
          // Read the exact target back; a successful POST is not readiness.
          const current = await api('/credentials/meta');
          if (!current || typeof current.revision !== 'string' || current.saved !== (action === 'save')) throw new Error('unconfirmed');
          if (!written || current.revision !== written.revision) {
            setInfo(null);
            setNotice('Key settings changed after this write. Another settings edit may have replaced it; recheck key status before trying again.');
            return;
          }
          setInfo(current);
          // Key readback and helper shutdown are independent outcomes.
          setNotice((action === 'save' ? 'Saved — new scoped requests can use the key immediately; existing requests and workers may retain their loaded key. Not verified with Meta.' : 'Saved key removed — new scoped requests may fall back to an inherited key. Existing requests and workers may retain their loaded key. Removal is not revocation.'));
        } catch (error) {
          setInfo(null);
          setNotice(tokenizerNotice(error) || 'The key change could not be confirmed. Recheck key status before trying again; another settings edit or an unsupported host may have blocked it.');
          // Only recognize a fixed server code, never render transport text.
          if (typeof error?.message === 'string' && error.message.includes('overlay_shutdown_unconfirmed')) {
            reflectOverlay({ overlay_credentials: 'retired', overlay_stop_confirmed: false }, target);
          }
          try {
            const current = await api('/status');
            if (current?.overlay_cleanup_error) reflectOverlay({ overlay_credentials: 'retired', overlay_stop_confirmed: false }, target);
            else if (current?.overlay_shown === false && (life.owner || life.credentialShutdownUnconfirmed)) reflectOverlay({ overlay_credentials: 'retired', overlay_stop_confirmed: true }, target);
          } catch (_) { /* retain the already confirmed lifecycle warning */ }
        } finally { delete body.key; inFlight.current = false; setWorking(false); }
      }
      const overlayNoticeKind = currentTransition(overlayNotice?.target) ? overlayNotice.kind : '';
      const overlayNoticeText = overlayNoticeKind === 'retired'
        ? (life.credentialShutdownUnconfirmed
          ? 'Overlay credential refresh failed; new overlay questions are disabled but shutdown is not confirmed. Retry hide overlay and verify the old window is gone before sharing.'
          : 'Overlay credential refresh failed; the overlay was closed. Show overlay to reopen with current credentials.')
        : overlayNoticeKind === 'refreshed' ? 'New overlay questions use a fresh credential snapshot.' : '';
      return h('section', { style: card, 'aria-label': 'Meta API key settings' },
        h('strong', null, 'Meta API key'),
        h('p', { style: muted, role: 'status' }, info
          ? `${info.saved ? 'Saved in this profile' : 'No saved key'} · ${info.request_configured ? 'Configured for this status request' : 'Not configured for this status request'} · Not verified with Meta`
          : 'Key status not checked'),
        h('label', { style: { display: 'grid', gap: 6 } }, 'Meta API key',
          h('input', { ref: input, type: 'password', autoComplete: 'new-password', spellCheck: false,
            autoCapitalize: 'none', maxLength: 4096, 'aria-label': 'Meta API key', style: selectStyle,
            placeholder: info?.saved ? 'Enter a replacement key' : 'Paste key here — never in chat', disabled: working,
            onChange: e => setHasDraft(!!e.target.value),
            onKeyDown: e => { if (e.key === 'Enter') e.preventDefault(); } })),
        h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 } },
          h(Button, { disabled: working || !info || !hasDraft, onClick: () => change('save') }, 'Save key'),
          h(Button, { variant: 'secondary', disabled: working || !info?.saved, onClick: () => change('remove') }, 'Remove saved key'),
          h(Button, { variant: 'secondary', disabled: working, onClick: recheck }, 'Recheck key status')),
        notice && h('p', { role: 'status', style: muted }, notice),
        overlayNoticeText && h('p', { role: 'status', style: muted }, overlayNoticeText),
        info?.process_differs_from_saved && h('p', { role: 'status', style: muted }, 'A different process-environment key remains available as fallback after removal, or to unscoped code. Update its service or secret-manager source too; restarting alone may retain it.'),
        info && h('p', { style: muted }, info.activation === 'host-scoped' ? 'This status used a host-scoped credential snapshot, not every running worker.' : 'This status used the process environment; process-based code may need a safe gateway restart to load file changes.'),
        h('p', { style: muted }, 'New scoped requests can change immediately: the saved file takes precedence over inherited keys in the supported single-profile host. Existing requests and workers may retain old keys. Other host secret sources may override the file. Pause capture before editing; removal is not revocation.'),
        h('details', null,
          h('summary', { style: { ...muted, cursor: 'pointer', marginTop: 8 } }, 'Safe transition & verification'),
          h('p', { style: muted }, 'Wait for existing answers to finish, then start capture again to take a fresh credential snapshot. For process-based code, wait for all gateway sessions to finish before using Hermes’ gateway restart control for this connection. Reopen this pane and Recheck key status. Do not restart while another session is working.'),
          h('p', { style: muted }, 'Saving is configuration only, not a Meta authentication test. Select Meta for answers and try a non-sensitive question; test Muse Voice separately with consent. An answer fallback or configured badge does not verify Meta or audio permissions.')),
        h('p', { style: { ...muted, marginBottom: 0 } }, 'Used for Muse Voice and Meta answers. Stored only in this backend’s active HERMES_HOME/.env; never returned by this editor. Saving does not reload the gateway or change a running capture.'));
    }
    const request = (path, body) => ctx.rest(path, body === undefined ? {} : { method: 'POST', body });
    // Survives plugin unregister/re-register in the SAME renderer. Neither a
    // wall clock nor HTTP arrival can establish pane creation order.
    const orderKey = Symbol.for('meeting-copilot.pane-creation.v1');
    const order = globalThis[orderKey] ||= { client: globalThis.crypto?.randomUUID?.(), next: 0 };
    const registration = order.registration = (order.registration || 0) + 1;
    let active = true;
    let paneRegistration = null;
    const registered = () => active && order.registration === registration;
    const lifecycles = new Set();
    const notify = (kind, message) => { try { host.notify({ kind, message }); } catch (e) {} };
    function releaseOverlayPane(life) {
      lifecycles.delete(life);
      if (life.release) return life.release;
      if (!life.pane) return Promise.resolve({ accepted: false });
      life.released = true;
      try { life.release = Promise.resolve(request('/overlay/release', { pane: life.pane, action: ++life.action })); }
      catch (error) { life.release = Promise.reject(error); }
      // Observe cleanup failures too; Restart still receives the rejection.
      const warnStop = () => notify('warning', 'Capture stop is not confirmed. Lease expiry initiates stop but cannot guarantee physical shutdown. Reopen Meeting Copilot to check status and Retry stop; verify the OS audio indicator.');
      life.release.then(result => {
        if (result?.stop_confirmed === false || result?.stopping) warnStop();
        if (result?.overlay_stop_confirmed === false) notify('warning', result.overlay_cleanup_error || 'Overlay shutdown is not confirmed. Reopen the pane and Retry hide overlay.');
      }, warnStop);
      return life.release;
    }
    // Trust only diagnostics created here, never transport exception text.
    const lifecycleErrors = new WeakSet();
    function lifecycleError(message) {
      const error = new Error(message);
      lifecycleErrors.add(error);
      return error;
    }
    function MeetingPane({ registration: paneEntry }) {
      // Fence all outbound work, including previously captured Assist/retry
      // callbacks. Release uses the registration-level request, never fallback.
      const usable = () => registered() && paneEntry.active && !paneEntry.retired;
      const api = (path, body) => (usable() || (path === '/status' && body === undefined)) ? request(path, body)
        : Promise.reject(new Error('This pane is retired. Close and reopen it.'));
      const qc = useQueryClient();
      const SCOPE_KEY = 'meeting-copilot.scope.v2';
      const loadScope = () => {
        try {
          // Web fallback became opt-in: a value inherited from the legacy key
          // (written when ON was the default) must not silently re-enable it.
          const current = typeof localStorage !== 'undefined' ? localStorage.getItem(SCOPE_KEY) : null;
          const legacy = typeof localStorage !== 'undefined' ? localStorage.getItem('meeting-copilot.scope') : null;
          const parsed = JSON.parse(current || '{}');
          if (!parsed || typeof parsed !== 'object') return {};
          const out = {};
          for (const k of ['project', 'backend', 'transcriptionModel', 'audioSource', 'answerProvider', 'answerModel', 'transcriptionProvider', 'asrMode']) {
            if (typeof parsed[k] === 'string') out[k] = parsed[k].slice(0, 500);
          }
          if (typeof parsed.webFallback === 'boolean') out.webFallback = parsed.webFallback;
          if (parsed.project && typeof parsed.project !== 'string') delete out.project;
          if (!current && legacy) {
            try {
              const oldParsed = JSON.parse(legacy);
              if (oldParsed && typeof oldParsed === 'object') {
                for (const k of ['project', 'backend', 'transcriptionModel', 'audioSource', 'answerProvider', 'answerModel', 'transcriptionProvider', 'asrMode']) {
                  if (out[k] === undefined && typeof oldParsed[k] === 'string') out[k] = oldParsed[k].slice(0, 500);
                }
              }
            } catch (e) {}
          }
          // Old versions persisted Meta after automatic capability discovery.
          // No provenance means no cloud opt-in, even if the value says Meta.
          if (out.transcriptionProvider === 'meta' && parsed.transcriptionChoiceVersion !== 1) out.transcriptionProvider = 'local';
          return out;
        } catch (e) { return {}; }
      };
      const saved = loadScope();
      const [project, setProject] = useState(saved.project || '');
      const [question, setQuestion] = useState('');
      const [web, setWeb] = useState(saved.web || '');
      const [consent, setConsent] = useState(false);
      const [backend, setBackend] = useState(saved.backend || 'meetily');
      const [recording, setRecording] = useState('');
      const [error, setError] = useState('');
      const [pending, setPending] = useState(false);
      const [showTranscript, setShowTranscript] = useState(false);
      const [webFallback, setWebFallback] = useState(saved.webFallback !== undefined ? saved.webFallback : false);
      const [transcriptionModel, setTranscriptionModel] = useState(saved.transcriptionModel || 'base.en');
      const [audioSource, setAudioSource] = useState(saved.audioSource || 'microphone');
      const [answerProvider, setAnswerProvider] = useState(saved.answerProvider || '');
      const [answerModel, setAnswerModel] = useState(saved.answerModel || '');
      const [overlayOn, setOverlayOn] = useState(false);
      const [overlayMsg, setOverlayMsg] = useState('');
      const [copied, setCopied] = useState(false);
      const [persistWarning, setPersistWarning] = useState('');

      const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => api('/status'), refetchInterval: 2500, retry: 1 });
      // Keep the overlay lease alive while the pane is visible; overlay helper
      // polls use touch=false so only this heartbeat extends the 30s watchdog.
      useEffect(() => {
        if (status.error) return undefined;
        const beat = async () => {
          const life = overlayActionAt.current;
          // A rejected/uncertain claim is not authority, and must not fall back
          // to unscoped heartbeats or fresh revision tickets.
          if (!life.mounted || !life.pane || life.released) return;
          try {
            const result = await api('/heartbeat', { pane: life.pane });
            if (result?.accepted === false && life.mounted) setOverlayMsg('Pane lease expired or ownership changed. Capture will pause; close and reopen this pane.');
          } catch (e) {
            if (life.mounted) setOverlayMsg('Heartbeat failed. Capture will pause if the connection does not recover within 30 seconds.');
          }
        };
        const timer = setInterval(beat, 10000);
        return () => clearInterval(timer);
      }, [!!status.error]);
      const projects = useQuery({ queryKey: [ID, 'projects'], queryFn: () => api('/projects'), staleTime: 60000, refetchInterval: query => query.state.error ? 5000 : false, retry: 3 });
      const recordings = useQuery({ queryKey: [ID, 'recordings'], queryFn: () => api('/recordings'), refetchInterval: backend === 'meetily' ? 5000 : false, enabled: backend === 'meetily', retry: 1 });
      const data = status.data || {};
      const modelCatalog = useQuery({ queryKey: [ID, 'models'], queryFn: () => api('/models'), enabled: !!data.capabilities?.independent_models, staleTime: 60000, retry: 1 });
      const providers = modelCatalog.data?.providers || [];
      const answerModels = providers.find(p => p.id === answerProvider)?.models || [];
      const busy = pending || data.busy;
      const allProjects = project === '__all_projects__';
      // Suggest answers stay local-first: the explicit web query travels
      // only to the explicit web button and to the overlay, never silently
      // into Suggest. Overlay sync tracks the query separately (scopeKey)
      // so editing or clearing it re-syncs instead of leaving stale state.
      const scope = { project: allProjects ? null : project || null, all_projects: allProjects, web_fallback: webFallback, ...(answerProvider && answerModel ? { answer_provider: answerProvider, answer_model: answerModel } : {}) };
      const overlayQuery = web.trim() ? { web_query: web.trim() } : {};
      const overlayScopeKey = JSON.stringify({ ...scope, web_query: web.trim() || null });
      const unsupportedReasons = [
        allProjects && !data.capabilities?.all_projects ? 'All-projects search needs the updated backend (restart the gateway when other sessions are idle).' : null,
        webFallback && !data.capabilities?.automatic_web_fallback ? 'Automatic web fallback needs the updated backend.' : null,
        answerProvider && (!answerModel || !data.capabilities?.independent_models) ? 'Answer model selection needs the updated backend and a chosen model.' : null,
      ].filter(Boolean);
      const unsupported = unsupportedReasons.length > 0;
      // Closing the pane pauses capture: the overlay is hidden and the
      // watchdog alone must not keep recording for ~30s after the user left.
      useEffect(() => {
        const life = overlayActionAt.current.mounted ? overlayActionAt.current : newOverlayLifecycle();
        overlayActionAt.current = life;
        claimOverlayPane(life).catch(() => { if (life.mounted) setOverlayMsg('Pane authorization unavailable. Retry the action, or close and reopen this pane. Check the backend connection.'); });
        return () => {
          life.mounted = false;
          releaseOverlayPane(life);
        };
      }, []);
      // Auto-sync overlay scope when settings change while shown; guard
      // against out-of-order responses clearing a newer stale indicator.
      const [scopeStale, setScopeStale] = useState(false);
      const scopeKey = overlayScopeKey;
      const scopeSeq = useRef(0);
      // Owner is minted by Show, not a pane-local counter that resets on remount.
      function newOverlayLifecycle() {
        const life = { at: 0, owner: null, pane: null, claim: null, action: 0, overlayGeneration: 0,
          creation: ++order.next,
          alive: true, syncedKey: null, released: false,
          get mounted() { return this.alive && registered() && paneEntry.active && paneRegistration === paneEntry; },
          set mounted(value) { this.alive = value; } };
        paneEntry.retired = null;  // a genuinely new effect owns its own recovery
        paneEntry.retire?.(null);
        paneEntry.life = life;
        lifecycles.add(life);
        return life;
      }
      const overlayActionAt = useRef(null);
      if (!overlayActionAt.current) overlayActionAt.current = newOverlayLifecycle();
      async function claimOverlayPane(life) {
        if (!life.claim) life.claim = Promise.resolve().then(() => {
          if (!life.mounted) return null;
          if (!order.client || !Number.isSafeInteger(life.creation)) throw lifecycleError('Pane creation ordering is unavailable.');
          return api('/overlay/lifecycle');
        }).then(async ticket => {
          if (!life.mounted) return null;
          if (!ticket.epoch) throw lifecycleError('Update/restart the backend for pane creation ordering.');
          life.claimAttempted = true;
          return api('/overlay/claim', { revision: ticket.revision, epoch: ticket.epoch,
            client: order.client, creation: life.creation });
        }).then(result => {
          if (!result && !life.mounted) return null;
          if (result?.reason === 'different_renderer') throw lifecycleError('Another desktop renderer owns this backend. A reloaded renderer cannot renew its capture lease: the watchdog initiates stop within 32 seconds of the last owner heartbeat. Verify capture has stopped. Return to the original window, or close all Meeting Copilot panes and restart the backend when other sessions are idle, then reopen.');
          if (result?.accepted === false) throw lifecycleError('Another pane owns the overlay. Close and reopen this pane to take ownership.');
          if (!result?.pane) throw lifecycleError('Overlay lifecycle protocol unavailable. Update/restart the backend and reopen the pane.');
          life.pane = result.pane;
          if (!life.mounted) releaseOverlayPane(life);
          return result.pane;
        }).catch(e => {
          const detail = lifecycleErrors.has(e) ? e.message : 'Pane authorization unavailable. Check the backend connection.';
          if (!life.claimAttempted) {
            // GET cannot commit. Retry on the next user action using the SAME
            // creation identity; never replay a possibly committed POST.
            life.claim = null;
            throw lifecycleError(`${detail} Retry the action to look up lifecycle authorization, or close and reopen the pane.`);
          }
          // A lost claim response may already have committed. Do not mint a
          // new creation ID on retry: reopening is an explicit new lifecycle.
          throw lifecycleError(`${detail} Close and reopen this pane to retry lifecycle authorization.`);
        });
        return life.claim;
      }
      useEffect(() => { if (overlayOn) { scopeSeq.current += 1; overlayActionAt.current.syncedKey = null; setScopeStale(true); } }, [scopeKey]);
      async function syncOverlayScope(seq) {
        const life = overlayActionAt.current;
        const owner = life.owner;
        const current = () => life.mounted && scopeSeq.current === seq && life.owner === owner;
        // Sending is not acknowledgement. Invalidate the old key on every
        // intervening write, including manual and still-pending requests.
        life.syncedKey = null;
        try {
          const res = await api('/overlay/scope', { scope: { ...scope, ...overlayQuery, seq, owner } });
          if (!current()) return;
          if (res?.accepted === false) throw new Error('Overlay scope was rejected.');
          life.syncedKey = scopeKey; setScopeStale(false);
          setOverlayMsg('Overlay scope updated to current settings.');
        } catch (e) {
          if (!current()) return;
          // Ordered hide fails closed without closing a newer pane or Show.
          const action = ++life.action;
          try {
            const hidden = await api('/overlay/hide', { pane: life.pane, action });
            if (!life.mounted || life.action !== action) return;
            if (hidden?.accepted === false) throw new Error('Overlay ownership changed.');
            if (hidden?.overlay_stop_confirmed === false) throw new Error(hidden.overlay_cleanup_error || 'Overlay shutdown is not confirmed.');
            life.owner = null; setOverlayOn(false);
            setOverlayMsg('Overlay closed because scope synchronization failed. Show overlay to retry current settings.');
          } catch (hideError) {
            if (life.mounted && life.action === action) setOverlayMsg('Scope sync failed and overlay closure could not be confirmed. Retry Hide overlay or quit the helper; closing the pane only requests shutdown.');
          }
        }
      }
      useEffect(() => {
        if (!overlayOn || overlayActionAt.current.syncedKey === scopeKey) return undefined;
        // Permission changes are immediate; do not debounce a narrowing.
        if (overlayActionAt.current.mounted) void syncOverlayScope(scopeSeq.current);
        return undefined;
      }, [scopeKey, overlayOn]);
      // Reconcile with the authoritative backend state: if the helper died
      // or its lease expired, stop offering Hide/Sync for a gone window.
      // The grace period covers in-flight show/sync actions. Depends on the
      // whole status object so expiry observed mid-grace is still honored —
      // status polls refresh every few seconds.
      useEffect(() => {
        if (overlayOn && data && data.overlay_shown === false && Date.now() - overlayActionAt.current.at > 8000) {
          setOverlayOn(false);
          setOverlayMsg('Overlay is no longer shown (helper exited or lease expired). Show overlay to reopen it.');
        }
      }, [data]);
      // Returns true when the backend request itself succeeds, so
      // destructive callers (Clear, New meeting) reset pane-local state on a
      // confirmed action. The follow-up status refresh is best-effort: the
      // 2.5s status poll heals a missed invalidation, and a failed refresh
      // must not masquerade as a failed destructive action.
      async function act(path, body = {}) {
        if (!overlayActionAt.current.mounted) return false;
        setPending(true); setError('');
        try {
          if (path === '/pause' || path === '/clear') {
            const life = overlayActionAt.current;
            const pane = await claimOverlayPane(life);
            if (!life.mounted) return false;
            body = { ...body, pane, action: ++life.action };
          }
          const response = await api(path, body);
          if (path === '/clear') { retireLocalOverlay(); setOverlayMsg(response?.overlay_cleanup_error ? 'Overlay shutdown is not confirmed. Retry hide overlay and verify the window is gone before sharing.' : ''); }
        }
        catch (e) {
          // Only local, tagged lifecycle messages are trusted. Transport and
          // adapter diagnostics can contain credentials or meeting content.
          const recovery = {
            '/start': 'Start was not confirmed. Check audio permissions, the selected recording/provider and backend connection. Verify capture state; Pause before retrying Start.',
            '/pause': 'Pause was not confirmed; audio may still be active. Retry Pause / Retry stop and verify the OS capture indicator. Check the backend connection.',
            '/clear': 'Clear was not confirmed. Check the backend connection and retry Clear. Do not assume the meeting was cleared or capture stopped.',
          };
          setError(lifecycleErrors.has(e) ? e.message : (recovery[path] || 'Action was not confirmed. Check the backend connection and retry.'));
          return false;
        }
        finally { setPending(false); }
        try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
        catch (e) {}
        return true;
      }
      // Tracked assistance: the backend returns a request id whose live slot
      // carries streamed partials until it reaches a terminal state. The
      // pane polls that slot so slow answers show progress — and offer an
      // explicit cancel — instead of a generic busy spinner.
      const [trackedRequest, setTrackedRequest] = useState(null);
      const [liveEntry, setLiveEntry] = useState(null);
      const [lastAsked, setLastAsked] = useState(null);
      const [assistFailed, setAssistFailed] = useState(false);
      const [transcriptionProvider, setTranscriptionProvider] = useState(saved.transcriptionProvider === 'meta' ? 'meta' : 'local');
      const [asrMode, setAsrMode] = useState(['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK'].includes(saved.asrMode) ? saved.asrMode : 'ENDPOINTING');
      const [setupOpen, setSetupOpen] = useState(false);
      useEffect(() => {
        try {
          if (typeof localStorage !== 'undefined') localStorage.setItem(SCOPE_KEY, JSON.stringify({ project, backend, webFallback, transcriptionModel, audioSource, answerProvider, answerModel, transcriptionProvider, transcriptionChoiceVersion: 1, asrMode }));
        } catch (e) { setPersistWarning('Preferences could not be saved in this browser (storage unavailable). Settings still apply for this session.'); }
      }, [project, backend, webFallback, transcriptionModel, audioSource, answerProvider, answerModel, transcriptionProvider, asrMode]);
      // Capability discovery never opts a meeting into cloud audio. Only an
      // explicit selection or persisted provider choice may select Meta.
      function resetMeetingInputs() {
        setQuestion(''); setWeb(''); setLastAsked(null); setLiveEntry(null);
        setTrackedRequest(null); setError(''); setAssistFailed(false);
      }
      const cloudUnavailable = backend === 'macos' && transcriptionProvider === 'meta' && !data.capabilities?.meta_asr;
      const overlayNeedsHide = !!data.overlay_cleanup_error || !!overlayActionAt.current.credentialShutdownUnconfirmed;
      function reflectCredentialOverlay(written, target) {
        const life = overlayActionAt.current;
        if (!life.mounted || target?.life !== life || target.pane !== life.pane
            || target.owner !== life.owner || target.overlayGeneration !== life.overlayGeneration) return false;
        retireLocalOverlay();
        // Subsequent readback for this edit can refine shutdown, but only until
        // another overlay replaces this retirement. Pause/ordinary Start use
        // action ordering, but must not discard evidence about the same helper.
        target.owner = life.owner;
        target.overlayGeneration = life.overlayGeneration;
        life.credentialShutdownUnconfirmed = written.overlay_stop_confirmed !== true;
        setOverlayMsg(life.credentialShutdownUnconfirmed
          ? 'Overlay shutdown is not confirmed. Retry hide overlay and verify the window is gone before sharing.'
          : 'Overlay was closed after the key edit. Show overlay to reopen with current credentials.');
      }
      function retireLocalOverlay() {
        const life = overlayActionAt.current;
        life.overlayGeneration += 1;
        life.owner = null; life.syncedKey = null; life.credentialShutdownUnconfirmed = false;
        scopeSeq.current += 1;
        setOverlayOn(false); setScopeStale(false); setOverlayMsg('');
      }
      async function toggleOverlay() {
        const life = overlayActionAt.current;
        if (!life.mounted) return;
        setPending(true); setOverlayMsg('');
        life.at = Date.now();
        try {
          const pane = await claimOverlayPane(life);
          if (!life.mounted) return;
          const action = ++life.action;
          if (overlayOn || overlayNeedsHide) {
            const hidden = await api('/overlay/hide', { pane, action });
            if (!life.mounted || life.action !== action) return;
            if (hidden?.accepted === false) throw new Error('This pane no longer owns the overlay. Reopen the pane to take ownership.');
            if (hidden?.overlay_stop_confirmed === false) throw new Error(hidden.overlay_cleanup_error || 'Overlay shutdown is not confirmed.');
            retireLocalOverlay();
          } else {
            // Fence even a lost/in-flight Show response: it may create a new
            // owner before a delayed credential response reaches this pane.
            life.overlayGeneration += 1;
            const shown = await api('/overlay/show', { pane, action, scope: { ...scope, ...overlayQuery, seq: scopeSeq.current } });
            if (!life.mounted || life.action !== action) return;
            if (shown?.accepted === false || !shown?.owner) throw new Error('This pane no longer owns the overlay. Reopen the pane to take ownership.');
            life.owner = shown.owner; life.syncedKey = scopeKey;
            setOverlayOn(true); setScopeStale(false);
            setOverlayMsg('Overlay shown. It returns to click-through after ~10s — press the shortcut to interact, then tap a row for a streamed answer.');
          }
          await qc.invalidateQueries({ queryKey: [ID, 'status'] });
        } catch (e) { if (life.mounted) {
          setOverlayMsg('Overlay action not confirmed. Retry Show / Hide overlay; if it still fails, close and reopen this pane. Check the macOS helper and backend connection. A window may still be visible: verify before screen-sharing.');
          if (!overlayOn) setOverlayOn(false);
        } }
        finally { if (life.mounted) setPending(false); }
      }
      async function startCapture(newMeeting = false) {
        if (cloudUnavailable || overlayNeedsHide) return false;
        const retained = !!((data.segments || []).length || data.result || (data.recent || []).length || data.error_request);
        const last = data.last_session;
        const sourceChanged = last && (
          last.backend !== backend || (backend === 'meetily'
            ? last.recording_id !== (recording || null)
            : last.transcription_model !== transcriptionModel || last.audio_source !== audioSource
              || (last.transcription_provider || 'local') !== transcriptionProvider || (last.asr_mode || 'ENDPOINTING') !== asrMode));
        const message = newMeeting
          ? 'Start a fresh meeting? The earlier transcript, answers, and retry data are cleared.'
          : 'These settings differ from the previous session, so starting begins a fresh meeting and clears the earlier transcript, answers, and retry data. Continue?';
        if ((newMeeting || (retained && sourceChanged)) && typeof window !== 'undefined' && window.confirm && !window.confirm(message)) return false;
        const life = overlayActionAt.current;
        setPending(true);
        try {
          const pane = await claimOverlayPane(life);
          if (!life.mounted) return false;
          const ok = await act('/start', { pane, action: ++life.action, consent, backend, recording_id: recording || null,
            transcription_model: transcriptionModel, audio_source: audioSource, new_meeting: newMeeting,
            transcription_provider: transcriptionProvider, asr_mode: asrMode });
          if (life.mounted && ok && (newMeeting || sourceChanged)) { resetMeetingInputs(); retireLocalOverlay(); }
          return ok;
        } catch (e) {
          if (life.mounted) setError(lifecycleErrors.has(e) ? e.message : 'Start was not confirmed. Check the backend connection; close and reopen this pane to retry authorization.');
          return false;
        } finally { if (life.mounted) setPending(false); }
      }
      const answerSubmitting = useRef(false);
      const answerBlocked = !!(busy || trackedRequest || Object.keys(data.requests || {}).length);
      const answerFailure = value => value === 'Provider authentication failed (HTTP 401). Check your API key.'
        ? value : 'Answer failed. Check answer setup, credentials, and backend connection, then Retry if you want to resend this question and its saved context.';
      const acceptAnswer = res => {
        if (res?.contract === 'answer-admission-v1' && res.accepted === false &&
            ['invalid_request', 'busy', 'capacity'].includes(res.reason)) {
          setError({ invalid_request: 'Answer was not accepted. Enter a question or start listening; check the selected scope.',
            busy: 'Answer was not accepted because another answer is active. Wait for it to finish or cancel it, then ask again.',
            capacity: 'Answer was not accepted because workers are still busy. Wait for them to finish, then ask again.' }[res.reason]);
          setAssistFailed(true);
          return;
        }
        if (!res?.request_id) throw new Error('Unconfirmed answer submission');
        setLiveEntry(null); setTrackedRequest(res.request_id);
      };
      async function assistNow(extra = {}, questionOverride, scopeOverride) {
        if (!usable() || !overlayActionAt.current.mounted || answerBlocked || answerSubmitting.current) return;
        answerSubmitting.current = true;
        const asked = questionOverride !== undefined ? questionOverride : question;
        const scopeUsed = scopeOverride || scope;
        if (questionOverride !== undefined) setQuestion(questionOverride);
        setPending(true); setError(''); setAssistFailed(false);
        // Remember for explicit partial replay, not automatic transport retry.
        setLastAsked({ question: asked, extra, scope: { ...scopeUsed } });
        // Request outcome and status refresh are separate: an accepted
        // assist whose refresh fails must not be reported as failed (and
        // retried into a duplicate) — the tracked request already exists.
        try {
          const res = await api('/assist', { question: asked, ...scopeUsed, ...extra, response_contract: 'answer-admission-v1' });
          acceptAnswer(res);
        }
        catch (e) { setError('Answer submission was not confirmed. Check the backend connection and recheck status; the request may already be running or complete. Review the result before asking again.'); setAssistFailed(true); }
        finally { answerSubmitting.current = false; setPending(false); }
        try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
        catch (e) {}
      }
      // Exact replay: resend a stored request verbatim (question, scope,
      // transcript snapshot) instead of whatever the pane currently shows.
      async function replayRequest(req) {
        if (!usable() || !overlayActionAt.current.mounted || !req || answerBlocked || answerSubmitting.current) return;
        answerSubmitting.current = true;
        if (typeof req.question === 'string') setQuestion(req.question);
        setPending(true); setError(''); setAssistFailed(false);
        // A lost replay response may still mean the backend accepted it.
        setLastAsked({ question: req.question || '', extra: { web_query: req.web_query }, scope: req });
        try {
          const body = { response_contract: 'answer-admission-v1', question: req.question || '', project: req.project ?? null,
            all_projects: !!req.all_projects, web_fallback: !!req.web_fallback,
            ...(req.web_query ? { web_query: req.web_query } : {}),
            ...(req.answer_provider ? { answer_provider: req.answer_provider, answer_model: req.answer_model } : {}),
            ...(typeof req.transcript === 'string' ? { transcript: req.transcript } : {}),
            ...(req.row_id ? { row_id: req.row_id } : {}) };
          const res = await api('/assist', body);
          acceptAnswer(res);
        }
        catch (e) { setError('Answer submission was not confirmed. Check the backend connection and recheck status; the request may already be running or complete. Review the result before asking again.'); setAssistFailed(true); }
        finally { answerSubmitting.current = false; setPending(false); }
        // Same split as a fresh ask: a replayed request that the backend
        // accepted must not become a duplicate through a failed refresh.
        try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
        catch (e) {}
      }
      const [progressRetry, setProgressRetry] = useState(0);
      useEffect(() => {
        if (!trackedRequest) return undefined;
        let alive = true;
        let inFlight = false;
        let paused = false;
        let misses = 0;
        const poll = async () => {
          // Serialize: a slow backend must not let polls resolve out of order.
          if (inFlight || paused) return;
          inFlight = true;
          try {
            const entry = await ctx.rest(`/assist/${trackedRequest}?missing=sentinel-v1`);
            if (!alive) return;
            if (entry?.contract === 'answer-progress-v1' && entry.status === 'missing') {
              setTrackedRequest(null); setLiveEntry(null);
              setError('That answer is no longer available (new meeting or restart cleared it). Ask again.');
              try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); } catch (ignored) {}
              return;
            }
            if (!entry) {
              // The slot is gone (cleared by a newer session or restart): stop tracking
              // with an actionable explanation instead of stale progress.
              if (++misses >= 2) {
                setTrackedRequest(null); setLiveEntry(null);
                setError('That answer is no longer available (new meeting or restart cleared it). Ask again.');
                try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); } catch (ignored) {}
              }
              return;
            }
            misses = 0;
            setLiveEntry(entry);
            // A recovered poll clears a stale progress error; other errors stay.
            setError(prev => (typeof prev === 'string' && (prev.startsWith('Answer progress') || prev.startsWith('That answer is no longer'))) ? '' : prev);
            if (entry && (entry.status === 'done' || entry.status === 'error' || entry.status === 'cancelled')) {
              setTrackedRequest(null);
              await qc.invalidateQueries({ queryKey: [ID, 'status'] });
            }
          } catch (e) {
            // Older Desktop bridges expose only a plain Error, even for HTTP
            // rejection. Never parse its potentially secret-bearing text. On
            // old backends a missing slot is indistinguishable from a network
            // failure: pause after three failures, retain tracking, and let an
            // explicit status recheck resume GETs (never POST the transcript).
            if (!alive) return;
            if (++misses >= 3) {
              paused = true;
              setError('Answer progress unavailable. Polling is paused; the answer may still be running or may have been cleared. Check the backend connection and Recheck status to resume progress checks before asking again.');
            }
          } finally { inFlight = false; }
        };
        poll();
        const timer = setInterval(poll, 1000);
        return () => { alive = false; clearInterval(timer); };
      }, [trackedRequest, progressRetry]);
      // Reattach: reopening the pane mid-request resumes tracking the live
      // slot reported by status instead of losing its partials and Cancel.
      const liveIds = Object.keys(data.requests || {});
      useEffect(() => {
        // A terminal entry from the last answer must not hide an accepted
        // replay whose POST response was lost. Status is authoritative for
        // live ownership; reattach without ever resending the request.
        if (!trackedRequest && liveIds.length > 0) setTrackedRequest(liveIds[0]);
      }, [liveIds.join(',')]);
      const cancellableId = trackedRequest || liveIds[0] || null;
      const entries = Array.isArray(projects.data) ? projects.data : (projects.data?.projects || []);
      const result = data.result;
      const transcript = data.segments || [];
      const failed = !!(data.error_request || data.error || (data.capture && (data.capture.error || /fail|error|could not/i.test(data.capture.warning || ''))));
      const statusLabel = status.isPending ? 'Connecting…' : status.error ? 'Unavailable' : data.stopping ? 'Stopping — unconfirmed' : failed ? 'Needs attention' : data.listening ? 'Listening' : 'Paused';
        // Always-visible listening bar: the source card collapses while
        // listening, but Pause/Detach and loss evidence must never hide.
        // Native frames, worker chunks, and sub-second tails are distinct
        // units: report each separately, never summed as one number.
        const lossText = cap => {
          if (!cap) return '';
          const parts = [];
          const w = cap.worker_dropped_chunks, n = cap.native_dropped_chunks, s = cap.sub_second_dropped_chunks;
          if (typeof w === 'number' && w > 0) parts.push(`${w} worker chunk(s)`);
          if (typeof n === 'number' && n > 0) parts.push(`${n} native frame(s)`);
          if (typeof s === 'number' && s > 0) parts.push(`${s} sub-second tail(s)`);
          if (!parts.length && typeof cap.dropped_chunks === 'number' && cap.dropped_chunks > 0) parts.push(`${cap.dropped_chunks} audio segment(s)`);
          return parts.length ? ` (${parts.join(' + ')})` : '';
        };
      // The answer region finds the user: when a live or completed answer
      // updates, scroll it into view instead of leaving it below the fold.
      const answerRef = useRef(null);
      const composerRef = useRef(null);
      useEffect(() => {
        const el = answerRef.current;
        if ((result?.answer || liveEntry) && el && typeof el.scrollIntoView === 'function') {
          try { el.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (ignored) {}
        }
      }, [result?.answer, liveEntry?.answer, liveEntry?.status]);
      const [, setRetirement] = useState('');
      paneEntry.retire = message => { paneEntry.retired = message; setRetirement(message); };
      if (paneEntry.retired) return h('section', { role: 'alert', style: card },
        h('strong', null, 'Meeting Copilot pane retired — actions disabled'),
        h('p', null, paneEntry.retired),
        h('p', null, 'Close this pane and reopen Meeting Copilot. Check capture status and Retry stop if needed before giving fresh consent. If audio remains active, quit the capture process or revoke audio permissions and verify the OS indicator.'));
      return h('div', { style: { padding: 16, display: 'flex', flexDirection: 'column', gap: 14, height: '100%', minWidth: 0, width: '100%', boxSizing: 'border-box', overflowY: 'auto', overflowX: 'hidden', overflowWrap: 'anywhere', color: 'var(--ui-text-primary)' } },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' } },
          h('strong', { style: { fontSize: 17 } }, 'Meeting Copilot'),
          h(Badge, { title: status.error ? 'Backend unavailable. Check the backend connection, installation, and plugin enablement.' : status.isPending ? 'Contacting backend…' : undefined }, statusLabel)),
        // Capture session first: the permission check and the primary
        // start/pause actions live at the top, before any setup detail.
        h('section', { style: { ...card, display: 'grid', gap: 10 }, 'aria-label': 'Capture session' },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
            h('strong', null, 'Capture session'),
            h('span', { style: muted, role: 'status' }, data.stopping ? 'Stop is not confirmed — audio may still be active.' : data.listening
              ? `Listening via ${data.backend || backend}${lossText(data.capture)}${data.capture?.warning ? ' · transcript may be incomplete' : ''}`
              : ((data.segments || []).length || result || (data.recent || []).length) ? 'Paused — resume this meeting or start a fresh one.' : 'Not capturing yet.')),
          h('label', { style: { display: 'flex', gap: 8, alignItems: 'flex-start', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '10px 12px', background: 'var(--ui-bg-card)' } },
            h('input', { type: 'checkbox', checked: consent, disabled: data.listening, onChange: e => setConsent(e.target.checked), 'aria-label': 'Capture permission' }),
            h('span', null, h('strong', null, 'Permission check. '), 'I have permission to capture/read this meeting and use its context with my configured model.')),
          backend === 'macos' && transcriptionProvider === 'meta' && h('p', { 'data-cloud-capture-disclosure': true, role: 'status', style: { ...muted, margin: 0 } }, `Cloud transcription selected: meeting audio is sent to Meta while listening. ${data.listening ? 'Pause to change' : 'Change'} the provider in Transcript source below.`),
          cloudUnavailable && h('p', { role: 'status', style: muted }, 'Meta cloud transcription is unavailable. Choose Local in Transcript source, or configure a Meta key and recheck backend readiness before Start.'),
          !data.listening && !data.stopping && (pending || !consent || (backend === 'meetily' && !recording) || (backend === 'macos' && !data.capabilities?.macos_transcription)) && h('p', { style: { ...muted, margin: 0 }, role: 'status' },
            pending ? 'Starting…' : !consent ? 'Confirm permission with the check above to enable Start.' : (backend === 'meetily' && !recording) ? 'Select an active Meetily recording below to enable Attach.' : 'Independent macOS transcription is unavailable here — see the source card below.'),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
            data.stopping
              ? h(Button, { variant: 'secondary', disabled: pending, onClick: () => act('/pause', {}) }, 'Retry stop')
              : data.listening
              ? h(Button, { variant: 'secondary', onClick: () => act('/pause', {}) }, data.backend === 'meetily' ? 'Detach' : 'Pause listening')
              : h(Button, { disabled: pending || overlayNeedsHide || cloudUnavailable || (!consent || (backend === 'meetily' && !recording) || (backend === 'macos' && !data.capabilities?.macos_transcription)), onClick: () => startCapture() }, ((data.segments || []).length || result || (data.recent || []).length) ? (backend === 'meetily' ? 'Resume' : 'Resume listening') : (backend === 'meetily' ? 'Attach to Meetily' : 'Start listening')),
            !data.listening && !data.stopping && ((data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending || overlayNeedsHide || cloudUnavailable || (!consent || (backend === 'meetily' && !recording) || (backend === 'macos' && !data.capabilities?.macos_transcription)), title: 'Starts a fresh meeting; the earlier transcript, answers, and retry data are cleared', onClick: () => startCapture(true) }, 'New meeting'),
            (data.overlay_cleanup_error || (data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending, title: 'Clears past Q&A, transcript segments, and cached snippets for this meeting', onClick: async () => { if (typeof window !== 'undefined' && window.confirm && !window.confirm('Clear this meeting? Past questions, answers, and transcript segments are removed from the pane.')) return; if (await act('/clear')) { setQuestion(''); setLastAsked(null); setLiveEntry(null); setTrackedRequest(null); setError(''); setAssistFailed(false); } } }, 'Clear meeting'),
            h(Button, { variant: 'secondary', disabled: pending || (!data.capabilities?.overlay && !overlayNeedsHide), title: 'Show or hide the floating meeting overlay (macOS)', onClick: toggleOverlay }, overlayNeedsHide ? 'Retry hide overlay' : overlayOn ? 'Hide overlay' : 'Show overlay')),
          h('p', { style: { ...muted, margin: 0 } }, 'Screen-share privacy: the overlay may be visible in screenshots or screen sharing. Hide it before sharing; capture exclusion is best-effort, not a guarantee.'),
          overlayMsg && h('p', { role: 'status', style: { ...muted, margin: 0 } }, overlayMsg),
          overlayNeedsHide && h('p', { role: 'alert', style: { ...muted, margin: 0 } }, 'Overlay shutdown is not confirmed. Retry hide overlay and verify the window is gone before screen-sharing.'),
          !data.capabilities?.overlay && h('p', { role: 'status', style: { ...muted, margin: 0 } }, 'Overlay helper unavailable. Check the macOS helper build and restart the gateway when other sessions are idle.'),
          h('p', { style: { ...muted, margin: 0 } }, 'No additional audio files · disconnect initiates detach/pause; verify stop status before assuming audio is off. Resume keeps this meeting (transcript and latest answer stay); New meeting, different settings, or Clear starts fresh. Pausing can discard the last few seconds of audio.'),
          data.capture && h('div', { style: muted }, data.capture.message || data.capture.state || ''),
          data.capture && (data.capture.warning || lossText(data.capture)) && h('div', { role: 'status', style: muted },
            ['Transcript may be incomplete',
             lossText(data.capture),
             data.capture.warning ? `: ${data.capture.warning}` : ''].join(''))),
        (error || data.error || status.error || data.meetily_clear_error) && h('div', { role: 'alert', style: card },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' } },
            h('span', null, (error || (data.error_request ? answerFailure(data.error) : data.error) || data.meetily_clear_error) || 'Backend unavailable. Check the backend connection, installation, and plugin enablement.'),
            (() => {
              // Dismiss only clears local state; it never sends anything.
              // Retry replays the exact failed answer request when one is
              // known (async engine failure or the shown
              // partial result) and otherwise just rechecks backend state —
              // a capture failure recovers through Pause/Start, not re-ask.
              const progressIssue = typeof error === 'string' && error.startsWith('Answer progress');
              const storedRetry = assistFailed || progressIssue ? null : (data.error_request || null);
              const retryReq = storedRetry;
              const backendIssue = !!status.error && !storedRetry;
              // A capture failure needs capture restarted (consent already
              // given), not a status refresh: label and wire it honestly.
              const captureIssue = !assistFailed && !progressIssue && !!data.error && !storedRetry && !status.error && !data.stopping;
              // A failed forget-boundary needs Clear run again, not a
              // dismiss: the adapter keeps serving its old snapshot until
              // the boundary installs. The user already consented to
              // clearing, so the retry completes their intent.
              const clearIssue = !!data.meetily_clear_error && !storedRetry && !status.error && !data.error;
              const label = data.stopping || assistFailed || progressIssue ? 'Recheck status' : retryReq ? 'Retry' : backendIssue ? 'Retry' : captureIssue ? (data.listening ? 'Recheck status' : 'Restart capture') : clearIssue ? 'Clear again' : 'Dismiss';
              return h(Button, { variant: 'secondary', disabled: pending || (!!retryReq && answerBlocked) || (captureIssue && !data.listening && (cloudUnavailable || overlayNeedsHide || !consent || (backend === 'meetily' && !recording) || (backend === 'macos' && !data.capabilities?.macos_transcription))), onClick: async () => {
                if (data.stopping) {
                  try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
                  catch (ignored) { setError('Backend status unavailable. Check the connection and recheck status; stopping is not yet confirmed.'); }
                  return;
                }
                if (assistFailed) {
                  try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); setError(''); setAssistFailed(false); } catch (ignored) {}
                  return;
                }
                if (progressIssue) {
                  setProgressRetry(value => value + 1);
                  try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); } catch (ignored) {}
                  return;
                }
                if (retryReq) { setError(''); setAssistFailed(false); return replayRequest(retryReq); }
                if (clearIssue) { if (await act('/clear')) { setQuestion(''); setLastAsked(null); setLiveEntry(null); setTrackedRequest(null); setError(''); setAssistFailed(false); } return; }
                if (captureIssue && !data.listening) return startCapture();
                if (label === 'Dismiss') { setError(''); setAssistFailed(false); return; }
                try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
                catch (ignored) { setError('Backend unavailable. Check the backend connection, installation, and plugin enablement.'); }
              } }, label);
            })())),
        persistWarning && h('p', { role: 'status', style: muted }, persistWarning),
        !data.listening && !data.stopping && !result && h('section', { style: card, 'aria-label': 'Getting started' },
          h('strong', null, 'Getting started'),
          h('ol', { style: { ...muted, margin: '8px 0 0', paddingLeft: 20, display: 'grid', gap: 4 } },
            h('li', null, backend === 'meetily' ? 'Confirm permission above, pick your active Meetily recording below, then Attach.' : 'Confirm permission above, then Start listening.'),
            h('li', null, 'Type a question (or leave blank for the latest one) and choose Suggest answer.'),
            h('li', null, 'Read the cited answer; open Evidence for sources.'))),
        h(MetaKeySettings, { api, life: overlayActionAt.current, onOverlayTransition: reflectCredentialOverlay }),
        h(data.listening ? 'details' : 'section', data.listening ? null : { style: card },
          ...(data.listening ? [h('summary', { style: { cursor: 'pointer', ...muted } }, `Transcript source: ${data.backend || backend} — listening (pause to change settings)`)] : []),
          h('span', { id: 'mc-source-label', style: { fontWeight: 700 } }, 'Transcript source'),
          h('div', { style: { display: 'grid', gap: 6, paddingTop: 8, paddingBottom: 10 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-backend-label' }, 'Capture backend'),
            h('select', { value: backend, disabled: data.listening, onChange: e => setBackend(e.target.value), style: selectStyle, 'aria-labelledby': 'mc-backend-label' },
              h('option', { value: 'meetily' }, 'Meetily — use your existing recording'),
              h('option', { value: 'macos', disabled: !data.capabilities?.macos_transcription, title: data.capabilities?.macos_detail || '' }, 'Independent macOS transcription — no Meetily'),
              h('option', { value: 'native', title: 'Windows only · experimental' }, 'Native Windows capture — experimental')))),
          !data.capabilities?.macos_transcription && h('p', { style: { ...muted, margin: 0 } }, 'Independent macOS transcription is unavailable here. ' + (data.capabilities?.macos_detail || 'Select Meetily, or prepare macOS capture first.')),
          backend === 'macos' && !data.capabilities?.macos_transcription && h('p', { role: 'status', style: muted }, data.capabilities?.macos_detail || 'macOS transcription is unavailable on this machine.'),
          backend === 'macos' && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Audio source'), h('select', { style: selectStyle, value: audioSource, disabled: data.listening, onChange: e => setAudioSource(e.target.value), 'aria-label': 'Audio source' },
              h('option', { value: 'microphone' }, 'Microphone'), h('option', { value: 'system' }, 'System audio'), h('option', { value: 'both' }, 'Microphone + system audio'))),
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Transcription provider'), h('select', { style: selectStyle, value: transcriptionProvider, disabled: data.listening, onChange: e => setTranscriptionProvider(e.target.value), 'aria-label': 'Transcription provider' },
              h('option', { value: 'local' }, 'Local — faster-whisper on this Mac'),
              h('option', { value: 'meta', disabled: !data.capabilities?.meta_asr, title: data.capabilities?.meta_asr ? '' : 'Set MODEL_API_KEY for the gateway, then prepare macOS capture again' }, 'Meta Muse Voice — cloud transcription'))),
            transcriptionProvider === 'local' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Local transcription model'), h('select', { style: selectStyle, value: transcriptionModel, disabled: data.listening, onChange: e => setTranscriptionModel(e.target.value), 'aria-label': 'Local transcription model' },
              h('option', { value: 'tiny.en' }, 'Whisper tiny.en — fastest'), h('option', { value: 'base.en' }, 'Whisper base.en — balanced'), h('option', { value: 'small.en' }, 'Whisper small.en — higher accuracy'))),
            transcriptionProvider === 'local' && h('span', { style: muted }, 'Runs locally in a separate audio worker. First use may download the selected model. macOS microphone / screen-and-system-audio permissions are required. No Meetily process or recording is needed.'),
            transcriptionProvider === 'meta' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Meta ASR mode'), h('select', { style: selectStyle, value: asrMode, disabled: data.listening, onChange: e => setAsrMode(e.target.value), 'aria-label': 'Meta ASR mode' },
              h('option', { value: 'ENDPOINTING' }, 'Endpointing — one segment per utterance'),
              h('option', { value: 'DIARIZATION' }, 'Diarization — speaker labels per source'),
              h('option', { value: 'PUSH_TO_TALK' }, 'Push-to-talk — single-turn finals'))),
            transcriptionProvider === 'meta' && h('span', { role: 'status', style: muted }, 'Cloud transcription: meeting audio is sent to Meta (muse-voice-transcribe) while listening. Only final turns are kept; the API key stays on the gateway and is never shown here.')),
          backend === 'meetily' && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('span', { style: muted }, 'Start recording in Meetily, then choose the active meeting here. Only the explicitly selected transcript is read.'),
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-rec-label' }, 'Active Meetily recording'),
              h('select', { value: recording, disabled: data.listening || recordings.isPending, onChange: e => setRecording(e.target.value), style: selectStyle, 'aria-labelledby': 'mc-rec-label' },
                h('option', { value: '' }, recordings.isPending ? 'Looking for active Meetily recordings…' : (Array.isArray(recordings.data) ? recordings.data : []).filter(r => r.status === 'recording').length ? 'Choose an active Meetily recording…' : 'No active Meetily recordings found — start one in Meetily'),
                ...(Array.isArray(recordings.data) ? recordings.data : []).filter(r => r.status === 'recording').map(r => h('option', { value: r.id, key: r.id }, r.title || r.id)))),
            recordings.error && h('span', { role: 'alert', style: muted }, `Meetily lookup unavailable: ${recordings.error.message}. Start or check Meetily, then retry.`),
            h('span', { style: muted }, 'Meetily manages its own saved recordings and transcripts. Pausing here detaches the copilot; it does not stop Meetily recording.')),
          h('p', { style: { ...muted, marginBottom: 0 } }, 'Permission and session controls are in the Capture session card at the top.')),
        h('label', { ref: composerRef, style: { display: 'grid', gap: 6 } }, h('strong', { id: 'mc-q-label' }, 'What should I say?'),
          h(Textarea, { value: question, onChange: e => setQuestion(e.target.value), placeholder: 'Leave blank to answer the latest question, or type a question… (⌘/Ctrl+Enter to send)', rows: 3, 'aria-labelledby': 'mc-q-label', onKeyDown: e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !e.repeat && !e.nativeEvent?.isComposing && !busy && !unsupported) { e.preventDefault(); assistNow(); } } })),
        ...(unsupported ? [h('p', { role: 'alert', style: muted }, `Suggest is unavailable: ${unsupportedReasons.join(' ')} See Answer setup below, then restart the gateway when other sessions are idle.`)] : []),
        h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
          h(Button, { disabled: answerBlocked || unsupported, onClick: () => assistNow() }, busy ? 'Checking your sources…' : 'Suggest answer'),
          cancellableId && h(Button, { variant: 'secondary', onClick: async () => {
              try { await api('/assist/cancel', { request_id: cancellableId }); }
              catch (e) { setError('Cancellation was not confirmed. The answer may still be running; recheck status before asking again.'); setAssistFailed(true); return; }
              setTrackedRequest(null); setLiveEntry(null);
              try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
              catch (e) { setError('Answer progress unavailable after cancellation. Recheck status before asking again.'); }
            } }, 'Cancel answer'),
          h(Button, { variant: 'secondary', disabled: !result?.answer, onClick: async () => { if (!usable()) return; try { await navigator.clipboard.writeText(result.answer); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch (e) { setError('Clipboard unavailable. Select and copy the answer manually.'); } } }, copied ? 'Copied ✓' : 'Copy'),
          question && h(Button, { variant: 'secondary', disabled: busy, onClick: () => { setQuestion(''); composerRef.current?.querySelector('textarea')?.focus(); } }, 'Clear question')),
        liveEntry && ['queued', 'streaming', 'running'].includes(liveEntry.status) && h('section', { style: card, 'aria-live': 'polite', ref: answerRef },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8 } },
            h('strong', null, liveEntry.status === 'queued' ? 'Queued — waiting for a worker…' : liveEntry.streaming === false ? 'Preparing answer…' : 'Streaming answer…'),
            h('span', { style: muted }, `${liveEntry.model_route || ''}${liveEntry.streaming === false ? ' · completed mode' : ''}`)),
          h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 8 } }, liveEntry.answer || 'Contacting your sources…'),
          ...((liveEntry.warnings || []).map((w, i) => h('p', { key: `lw-${i}`, style: muted }, w)))),
        result && h('section', { style: card, 'aria-live': 'polite', ref: answerRef },
          h('div', { style: { display: 'flex', justifyContent: 'space-between' } }, h('strong', null, 'Suggested answer'), h('span', { style: muted }, `${Number.isFinite(result.elapsed_seconds) ? `${result.elapsed_seconds.toFixed(1)}s` : ''}${result.model_route ? ` · ${result.model_route}` : ''}`)),
          h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 10 } }, result.answer),
          result.web_search?.used && h('p', { style: muted }, `${result.web_search.mode === 'automatic' ? 'Automatic web fallback' : 'Explicit web search'}: ${result.web_search.query}`),
          ...(result.warnings || []).map((w, i) => h('p', { key: `warning-${i}`, style: muted }, w)),
          result.answer && /partial|interrupt/i.test((result.warnings || []).join(' ')) && (result.request || lastAsked) && h('div', { style: { paddingTop: 8 } }, h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(result.request || { question: lastAsked.question, ...(lastAsked.scope || {}) }) }, 'Retry for the complete response')),
          h('details', { style: { marginTop: 12 } }, h('summary', { style: { cursor: 'pointer', ...muted } }, `Evidence · ${(result.sources || []).length} sources`),
            ...(result.sources || []).map((s, i) => h('div', { key: s.id || i, style: { ...muted, marginTop: 8, padding: '8px 10px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, overflowWrap: 'anywhere' } },
              h('strong', null, `[${i + 1}] ${s.title || s.source || 'Source'}`),
              /^https?:\/\//.test(s.source || '') ? h('a', { href: s.source, target: '_blank', rel: 'noopener noreferrer', style: { display: 'block', color: 'var(--ui-accent)' } }, s.source) : h('div', null, s.source),
              s.updated_at && h('div', null, `Updated: ${typeof s.updated_at === 'number' ? new Date(s.updated_at * 1000).toLocaleString() : s.updated_at}`),
              h('div', { style: { whiteSpace: 'pre-wrap' } }, (s.text || '').slice(0, 900)))))),
        h('details', { open: showTranscript, onToggle: e => setShowTranscript(e.currentTarget.open) },
          h('summary', { style: { cursor: 'pointer', ...muted } }, `Live transcript · ${transcript.length} segments`),
          ...(() => {
            const palette = ['var(--ui-accent)', '#16a34a', '#ea8600', '#db2777', '#0284c7', '#9333ea'];
            const assigned = {};
            for (const seg of transcript) {
              const name = String(seg.speaker || 'Speaker');
              if (!(name in assigned)) assigned[name] = palette[Object.keys(assigned).length % palette.length];
            }
            return Object.keys(assigned).length > 1 ? [h('div', { key: 'speakers', style: { display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 } },
              ...Object.entries(assigned).map(([name, color]) => h('span', { key: name, style: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 999, padding: '2px 9px', fontSize: 12 } },
                h('span', { style: { color } }, '\u25cf '), name)))] : [];
          })(),
          h('div', { style: { maxHeight: 280, overflowY: 'auto', marginTop: 8, padding: 10, display: 'flex', flexDirection: 'column', gap: 8, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, background: 'var(--ui-bg-card)' } },
            ...(transcript.length ? transcript.slice(-30).map((s, i) => {
              const palette = ['var(--ui-accent)', '#16a34a', '#ea8600', '#db2777', '#0284c7', '#9333ea'];
              const order = [];
              for (const seg of transcript) { const n = String(seg.speaker || 'Speaker'); if (!order.includes(n)) order.push(n); }
              const color = palette[Math.max(0, order.indexOf(String(s.speaker || 'Speaker'))) % palette.length];
              return h('p', { key: `${i}-${s.timestamp}`, style: { margin: 0, padding: '8px 10px', borderRadius: 8, border: '1px solid var(--ui-stroke-secondary)', borderLeft: `3px solid ${color}`, background: `color-mix(in srgb, ${color} 7%, transparent)`, lineHeight: 1.5 } },
              h('strong', { style: { color } }, `${s.speaker}: `), s.text,
              s.partial_tail && h('span', { title: 'Transcribed from trailing audio captured during shutdown' }, ' \u00b7 trailing audio'));
            })
              : [h('p', { key: 'empty', style: { ...muted, margin: 0 } }, 'No transcript segments yet — attach or start listening, then speak.')]))),
        (Array.isArray(data.recent) && data.recent.length > 0) && h('details', null, h('summary', { style: { cursor: 'pointer', ...muted } }, `Recent answers · ${data.recent.length}`),
          h('p', { style: muted }, 'Kept for this session only — a restart, new meeting, or new answer session clears older entries. History evidence shows source titles only; full excerpts appear with the live answer.'),
          ...[...data.recent].reverse().map((entry, i) => h('div', { key: entry.request_id || i, style: { ...card, marginTop: 8 } },
            h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8 } },
              h('strong', null, (entry.question || '').slice(0, 200) || 'Answer'),
              h('span', { style: muted }, `${entry.status || ''}${entry.model_route ? ` · ${entry.model_route}` : ''}${entry.answer_shortened ? ' · shortened' : ''}`)),
            h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 8 } }, entry.answer || '(no answer text)'),
            entry.answer_shortened && h('p', { style: muted }, 'History keeps a shortened copy; the full answer appeared here when first delivered.'),
            ...((entry.warnings || []).map((w, j) => h('p', { key: `rw-${j}`, style: muted }, w))),
            (entry.sources || []).length > 0 && h('details', { style: { marginTop: 8 } }, h('summary', { style: { cursor: 'pointer', ...muted } }, `Evidence · ${entry.sources.length} sources${entry.sources.length > 6 ? ' (showing first 6)' : ''}`),
              ...(entry.sources || []).slice(0, 6).map((s, k) => h('div', { key: s.id || k, style: { ...muted, paddingTop: 6, overflowWrap: 'anywhere' } }, h('strong', null, `[${k + 1}] ${s.title || s.source || 'Source'}`)))),
            h('div', { style: { paddingTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' } },
              entry.answer && h(Button, { variant: 'secondary', onClick: async () => { if (!usable()) return; try { await navigator.clipboard.writeText(entry.answer); } catch (e) { setError('Clipboard unavailable. Select and copy the answer manually.'); } } }, 'Copy this answer'),
              entry.request && h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(entry.request) }, /partial|interrupt|unavailable|failed|error/i.test((entry.warnings || []).join(' ') + ' ' + (entry.status || '')) ? 'Retry for the complete response' : 'Ask again'))))),
        h('details', { open: setupOpen, onToggle: e => setSetupOpen(e.currentTarget.open), style: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 12, padding: '2px 14px 14px', background: 'var(--ui-bg-card)' } },
        h('summary', { style: { cursor: 'pointer', fontWeight: 700, color: 'var(--ui-text-primary)', padding: '12px 0', fontSize: 14 } }, setupOpen ? '\u25be Answer setup — scope, models, overlay, web' : '\u25b8 Answer setup — scope, models, overlay, web'),
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 14, paddingTop: 4 } },
        h('p', { style: { ...muted, margin: 0 } }, webFallback ? 'Your projects and past sessions first. Automatic public web fallback is ON: when local evidence is insufficient, a generic public query may be drafted (best-effort filtering, not a privacy guarantee).' : 'Your projects and past sessions first. Automatic public web fallback is OFF — answers use local evidence and your explicit web query only.'),
        h('label', { style: { display: 'grid', gap: 6 } }, h('strong', { id: 'mc-scope-label' }, 'Search scope'),
          h('select', { value: project, onChange: e => setProject(e.target.value), style: selectStyle, 'aria-labelledby': 'mc-scope-label', disabled: projects.isPending },
            h('option', { value: '' }, 'Past sessions only'),
            h('option', { value: '__all_projects__', disabled: !data.capabilities?.all_projects }, 'All projects + sessions'),
            ...entries.map(p => { const value = typeof p === 'string' ? p : (p.path || p.name); return h('option', { value, key: value }, typeof p === 'string' ? p.split(/[\\/]/).pop() : (p.name || value)); }))),
        projects.error && h('div', { role: 'alert', style: muted }, `Project lookup unavailable: ${projects.error.message}`),
        allProjects && h('p', { style: { ...muted, margin: 0 } }, 'Searches supported text files across up to 100 eligible project folders plus past sessions, not your whole computer. Evidence can include unrelated projects; review it before sharing.'),
        h('section', { style: { ...card, display: 'grid', gap: 10 } },
          h('span', { id: 'mc-model-label', style: { fontWeight: 700 } }, 'Answer model'),
          h('div', { style: { display: 'grid', gap: 8 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-provider-label' }, 'Answer provider'),
              h('select', { style: selectStyle, value: answerProvider, disabled: busy || !data.capabilities?.independent_models, onChange: e => { setAnswerProvider(e.target.value); setAnswerModel(''); }, 'aria-labelledby': 'mc-provider-label' },
                h('option', { value: '' }, providers.some(p => p.id === 'meta') ? 'Use Meta Muse Spark (default)' : 'Use Hermes configured model'), ...providers.map(p => h('option', { value: p.id, key: p.id }, p.label || p.id)))),
            answerProvider && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Answer model'),
              h('select', { style: selectStyle, value: answerModel, disabled: busy, onChange: e => setAnswerModel(e.target.value), 'aria-label': 'Answer model' },
                h('option', { value: '' }, 'Choose answer model…'), ...answerModels.map(m => h('option', { value: m.id, key: m.id }, m.label || m.id))))),
          modelCatalog.error && h('p', { role: 'alert', style: { ...muted, margin: 0 } }, 'Model catalog unavailable. Use the configured model or check provider setup.'),
          h('p', { style: { ...muted, margin: 0 } }, 'Answer selection applies only to this copilot request; it does not change your chat model or transcription provider.')),
        h('label', { style: { ...muted, display: 'flex', gap: 8, alignItems: 'flex-start', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '10px 12px', background: 'var(--ui-bg-card)', cursor: 'pointer' } },
          h('input', { type: 'checkbox', checked: webFallback, onChange: e => setWebFallback(e.target.checked), 'aria-label': 'Automatic web fallback' }),
          h('span', null, webFallback ? 'Automatic web fallback is ON — a generic public query may be drafted when local evidence is insufficient. Turn off for sensitive calls.' : 'Automatic web fallback is OFF — answers use local evidence and your explicit web query only.')),
        h('section', { style: { ...card, display: 'grid', gap: 10 } },
          h('strong', null, 'Meeting overlay (macOS)'),
          h('p', { style: { ...muted, marginBottom: 0 } }, 'Display-only window. Tap a question row to stream its answer into that row — nothing is sent until you tap. Press the overlay shortcut (default Ctrl+Option+Cmd+O) to interact; it returns to click-through after about 10 seconds, or press the shortcut again to return early. The overlay may be visible in screen shares.'),
          web.trim() && h('p', { role: 'status', style: { ...muted, marginBottom: 0 } }, `Explicit web query “${web.trim().slice(0, 120)}” applies to overlay row requests until you clear it.`),
          scopeStale && overlayOn && h('p', { role: 'status', style: { ...muted, margin: 0 } }, 'Settings changed — syncing overlay scope…'),
          overlayMsg && h('p', { role: 'status', style: { ...muted, margin: 0 } }, overlayMsg),
          !data.capabilities?.overlay && h('p', { role: 'status', style: { ...muted, margin: 0 } }, 'Overlay helper unavailable. Check the macOS helper build and restart the gateway, then try Show overlay again.'),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
            h(Button, { variant: overlayOn ? 'secondary' : undefined, disabled: pending || (!data.capabilities?.overlay && !overlayNeedsHide), onClick: toggleOverlay }, overlayNeedsHide ? 'Retry hide overlay' : overlayOn ? 'Hide overlay' : 'Show overlay'),
            h(Button, { variant: 'secondary', disabled: pending || !overlayOn, onClick: async () => {
              setPending(true); setOverlayMsg('');
              scopeSeq.current += 1;
              const seq = scopeSeq.current;
              overlayActionAt.current.at = Date.now();
              try { await syncOverlayScope(seq); }
              finally { setPending(false); }
            } }, scopeStale ? 'Sync scope •' : 'Sync scope'))),
        h('p', { style: { ...muted, margin: 0 } }, 'If needed, the model drafts a generic public query. Filtering is best-effort, not a privacy guarantee; turn this off for sensitive calls. Selected context still goes to your configured answer model.'),
        h('details', null, h('summary', { style: { cursor: 'pointer', ...muted, padding: '4px 0' } }, 'Search online with a specific query'),
          h('div', { style: { display: 'grid', gap: 8, paddingTop: 10 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Specific public web query'),
              h(Input, { value: web, onChange: e => setWeb(e.target.value), placeholder: 'Public query — no client names or meeting details', 'aria-label': 'Specific public web query' })),
            h('span', { style: muted }, 'This explicit query overrides automatic fallback for this request. Do not include private details.'),
            h(Button, { variant: 'secondary', disabled: busy || unsupported || !web.trim(), onClick: () => assistNow({ web_query: web.trim() }) }, 'Check sources + web'))),
        )),
        h('p', { style: { ...muted, marginTop: 'auto' } }, 'Read-only assistance. Nothing is spoken, posted, or changed for you. Check suggestions before using them.'));
    }
    const paneId = `${ID}:pane`;
    const visibility = typeof host.paneVisibility === 'function' ? host.paneVisibility(paneId) : null;
    const registerPane = () => {
      const entry = { active: true, life: null };
      paneRegistration = entry;
      const remove = ctx.register({ id: 'pane', area: 'panes', title: 'Meeting Copilot', data: { placement: 'right', width: '440px' },
        render: () => registered() && entry.active && paneRegistration === entry ? h(MeetingPane, { registration: entry }) : null });
      return () => {
        if (!registered() || paneRegistration !== entry || !entry.active) return;
        entry.active = false;
        paneRegistration = null;
        if (entry.life) { entry.life.mounted = false; releaseOverlayPane(entry.life); }
        remove();
      };
    };
    let removePane = registerPane();
    const openPane = () => {
      if (!registered()) return;
      if (!removePane) removePane = registerPane();
      if (typeof host.revealPane === 'function') host.revealPane(paneId);
      else host.notify({ kind: 'error', message: 'Update Hermes Desktop to reopen contributed panes.' });
    };
    const togglePane = () => {
      if (!registered()) return;
      // Read current host state on click, not a potentially stale render closure.
      if (visibility?.get() && typeof removePane === 'function') {
        removePane();
        removePane = null;
      } else openPane();
    };
    async function restartPlugin() {
      if (!registered()) return;
      const entry = paneRegistration, remove = removePane;
      if (!entry) { openPane(); return; }
      if (entry.restarting) return;
      entry.restarting = true;
      entry.retire?.('Restart in progress. Capture pause is not confirmed; all actions are disabled.');
      const life = entry.life;
      const current = () => registered() && paneRegistration === entry && removePane === remove && entry.life === life;
      try {
        if (life) {
          // Retire callbacks/heartbeats BEFORE awaiting. A new pane may open
          // meanwhile; this continuation never owns that registration.
          life.mounted = false;
          if (life.claim) await life.claim;
          if (life.pane) {
            const released = await releaseOverlayPane(life);
            if (released?.accepted !== true) throw new Error('Pane ownership changed; release was not accepted.');
            if (released?.stop_confirmed === false || released?.stopping) throw new Error('Capture stop is not confirmed; the backend is retrying stop.');
            if (released?.overlay_stop_confirmed === false) throw new Error(released.overlay_cleanup_error || 'Overlay shutdown is not confirmed. Retry hide overlay before restarting.');
          }
        }
        if (!current()) return;
        remove(); removePane = null;
        openPane();
        notify('info', 'Meeting Copilot pane reopened. Its owned capture was paused before reopening; Resume requires consent.');
      } catch (e) {
        if (current()) {
          const message = `Restart was not completed: ${String(e.message || e)} Capture pause is not confirmed. Lease expiry initiates stop, but cannot guarantee physical shutdown.`;
          entry.retire?.(message);
          notify('warning', message + ' Close and reopen the pane to check status and Retry stop.');
        }
      } finally { entry.restarting = false; }
    };
    function StatusButton() {
      const visible = visibility ? useValue(visibility) : false;
      const [menu, setMenu] = useState(null);
      const title = `${visible ? 'Hide' : 'Open'} Meeting Copilot`;
      const closeMenu = () => setMenu(null);
      useEffect(() => {
        if (!menu) return undefined;
        const onKey = e => { if (e.key === 'Escape') { closeMenu(); focusToggle(); } };
        const timer = typeof window !== 'undefined' && window.requestAnimationFrame
          ? window.requestAnimationFrame(() => { const el = typeof document !== 'undefined' && document.querySelector('[data-mc-menu] button'); if (el) el.focus(); })
          : 0;
        window.addEventListener('keydown', onKey);
        return () => { window.removeEventListener('keydown', onKey); if (typeof window !== 'undefined' && window.cancelAnimationFrame) window.cancelAnimationFrame(timer); };
      }, [!!menu]);
      const menuStyle = pos => {
        if (typeof window === 'undefined') return { position: 'fixed', left: pos.x, top: pos.y };
        const w = 230, h = 120;
        // Position the menu's top-left corner directly; no translate offset.
        return { position: 'fixed', left: Math.max(8, Math.min(pos.x, window.innerWidth - w)), top: Math.max(8, Math.min(pos.y, window.innerHeight - h)), zIndex: 9999, minWidth: 210, maxWidth: w, background: 'var(--ui-bg-elevated)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: 4, boxShadow: '0 8px 24px rgba(0,0,0,0.25)' };
      };
      const focusToggle = () => {
        try {
          const el = typeof document !== 'undefined' && document.querySelector('[data-mc-toggle]');
          if (el) el.focus();
        } catch (e) {}
      };
      return h('div', { style: { position: 'relative' } },
        h('button', { type: 'button', title, 'aria-label': title, 'aria-pressed': visible, 'data-mc-toggle': '1', className: 'px-1.5 text-[0.6875rem] text-(--ui-text-secondary) hover:text-(--ui-text-primary)', onClick: togglePane, onContextMenu: e => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }); } }, 'Meeting Copilot'),
        menu && h('div', { onClick: () => { closeMenu(); focusToggle(); }, onContextMenu: e => { e.preventDefault(); closeMenu(); focusToggle(); }, style: { position: 'fixed', inset: 0, zIndex: 9998 } },
          h('div', { role: 'menu', 'aria-label': 'Meeting Copilot options', 'data-mc-menu': '1', onClick: e => e.stopPropagation(), onKeyDown: e => { if (e.key === 'Escape') { closeMenu(); focusToggle(); } }, style: menuStyle(menu) },
            h('button', { role: 'menuitem', type: 'button', onClick: () => { closeMenu(); restartPlugin(); }, style: { display: 'block', width: '100%', textAlign: 'left', padding: '8px 10px', borderRadius: 6, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--ui-text-primary)', fontSize: 12 } }, 'Restart plugin'),
            h('div', { style: { ...muted, padding: '6px 10px 4px', fontSize: 11 } }, 'Tip: ⌘K → Reload desktop plugins refreshes every plugin.'))));
    }
    ctx.register({ id: 'opener', area: 'statusBar.right', order: 130, render: () => h(StatusButton) });
    ctx.register({ id: 'open', area: PALETTE_AREA, data: { id: `${ID}.open`, label: 'Open Meeting Copilot', keywords: ['meeting', 'copilot', 'show', 'restore'], run: openPane } });
    ctx.register({ id: 'restart', area: PALETTE_AREA, data: { id: `${ID}.restart`, label: 'Restart Meeting Copilot', keywords: ['meeting', 'copilot', 'restart', 'reload'], run: restartPlugin } });
    ctx.register({ id: 'meeting-help', area: PALETTE_AREA, data: { label: 'Meeting Copilot: usage', run: () => host.notify({ kind: 'info', message: 'Open Meeting Copilot. Choose one project, All projects + sessions, or sessions only. Web fallback is off by default and can be enabled. Confirm consent and attach your recording, then Suggest answer.' }) } });
    return () => {
      for (const life of lifecycles) {
        life.mounted = false;
        releaseOverlayPane(life);
      }
      active = false;
    };
  },
};
