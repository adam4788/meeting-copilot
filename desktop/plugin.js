import { createElement as h, useState, useEffect, useRef } from 'react';
import { host, useValue, useQuery, useQueryClient, Button, Input, Textarea, Badge, PALETTE_AREA } from '@hermes/plugin-sdk';

const ID = 'meeting-copilot-live';
const speakerPalette = ['var(--ui-accent)', 'var(--ui-green, var(--ui-accent))', 'var(--ui-orange, var(--ui-accent))', 'var(--ui-red, var(--ui-accent))', 'var(--ui-cyan, var(--ui-accent))', 'var(--ui-purple, var(--ui-accent))'];
const muted = { color: 'var(--ui-text-secondary)', fontSize: 12, lineHeight: 1.5 };
const card = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 14, minWidth: 0, boxSizing: 'border-box', background: 'var(--ui-bg-card)' };
const selectStyle = { padding: 8, borderRadius: 6, width: '100%', maxWidth: '100%', minWidth: 0, color: 'var(--ui-text-primary)', background: 'var(--ui-bg-card)', border: '1px solid var(--ui-stroke-secondary)' };

// Portable evidence is assembled only from the displayed answer, never current
// meeting state, credentials, or request snapshots. Clipboard remains explicit.
function evidenceReport(result) {
  const text = (value, max) => typeof value === 'string' ? value.slice(0, max) : '';
  const lines = ['MEETING COPILOT — ANSWER + EVIDENCE',
    'AI-generated suggestion. Review against the excerpts; sources are not independent verification.',
    'This is not a complete meeting transcript or a verified record of decisions.', '',
    'Reported model route: ' + (text(result.model_route, 300) || 'unspecified'), '',
    'ANSWER', text(result.answer, 64000)];
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  if (warnings.length) lines.push('', 'WARNINGS', ...warnings.slice(0, 30).map(w => text(w, 2000)));
  const sources = Array.isArray(result.sources) ? result.sources : [];
  lines.push('', 'EVIDENCE EXCERPTS',
    'Numbering matches retrieved sources in the answer pane, not meeting segments or timestamps. Excerpts are limited to 900 characters each.');
  if (!sources.length) lines.push('No retrieved source excerpts accompanied this answer.');
  for (const [i, source] of sources.slice(0, 100).entries()) {
    const s = source || {};
    lines.push('', `[${i + 1}] ${text(s.title, 1000) || text(s.source, 2000) || 'Source'}`,
      'Source: ' + (text(s.source, 2000) || 'unspecified'));
    if (s.updated_at !== undefined && s.updated_at !== null) {
      const date = typeof s.updated_at === 'number' ? new Date(s.updated_at * 1000) : null;
      const updated = date ? (Number.isFinite(date.getTime()) ? date.toISOString() : '') : text(s.updated_at, 300);
      if (updated) lines.push('Source updated (not meeting time): ' + updated);
    }
    lines.push('Excerpt:', text(s.text, 900) || '(No excerpt available)');
  }
  if (sources.length > 100) lines.push('', 'Additional sources omitted from this bounded export.');
  lines.push('', 'Privacy: this copy contains source excerpts and may contain confidential material. Review before sharing.');
  return lines.join('\n');
}
export default {
  id: ID,
  name: 'Meeting Copilot',
  description: 'Live answers grounded in your projects, prior sessions, and optional web research.',
  version: '0.1.0',
  register(ctx) {
    function MetaKeySettings() {
      return h('section', { style: card }, h('strong', null, 'Bring your own API key'),
        h('p', { style: muted }, 'Configure OpenAI or OpenRouter in Hermes Settings → Plugins → meeting-copilot-live (default profile). Keys stay in Hermes-managed secret settings. Never enter keys in chat or model fields. Reopen this pane after setup. Local transcription does not use an answer key.'));
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
          if (typeof parsed.excludeHistory === 'boolean') out.excludeHistory = parsed.excludeHistory;
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
          // Reset routes the current pane cannot display; a blank select must never
          // retain a hidden provider/model pair from an earlier version.
          if (!['openai', 'openrouter'].includes(out.answerProvider)) {
            delete out.answerProvider; delete out.answerModel;
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
      const [error, setErrorValue] = useState('');
      function setError(value) {
        setErrorValue(value);
        // A new local message retires the previous message's retry authority.
        // Answer submission catches explicitly opt back in; progress-only
        // functional clears leave unrelated error provenance unchanged.
        if (typeof value !== 'function') setAssistFailed(false);
      }
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
      // New users start with project-only evidence; saved explicit choices persist.
      const [excludeHistory, setExcludeHistory] = useState(saved.excludeHistory !== undefined ? saved.excludeHistory : !(saved.webFallback || saved.web));

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
      const answerProviderAvailable = ['openai', 'openrouter'].includes(answerProvider) && providers.some(p => p.id === answerProvider);
      const busy = pending || data.busy;
      const allProjects = project === '__all_projects__';
      // Suggest answers stay local-first; explicit web queries stay in the pane.
      const scope = { project: allProjects ? null : project || null, all_projects: allProjects, web_fallback: webFallback, ...(excludeHistory ? { include_history: false } : {}), ...(answerProvider && answerModel ? { answer_provider: answerProvider, answer_model: answerModel } : {}) };
      const overlayScopeKey = JSON.stringify(scope);
      const unsupportedReasons = [
        allProjects && !data.capabilities?.all_projects ? 'All-projects search needs the updated backend (restart the gateway when other sessions are idle).' : null,
        webFallback && !data.capabilities?.automatic_web_fallback ? 'Automatic web fallback needs the updated backend.' : null,
        (!answerProviderAvailable || !answerModel || !data.capabilities?.independent_models) ? 'Answer model selection needs the updated backend and a chosen model.' : null,
        excludeHistory && !data.capabilities?.history_opt_out ? 'Excluding past sessions needs the updated backend. Restart the gateway when other work is idle.' : null,
        excludeHistory && (!project || allProjects || webFallback || web.trim()) ? 'To exclude past sessions, choose one project and turn off web search.' : null,
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
          const res = await api('/overlay/scope', { scope: { ...scope, seq, owner } });
          if (!current()) return;
          if (res?.accepted === false) throw new Error('Overlay scope was rejected.');
          life.syncedKey = scopeKey; setScopeStale(false);
          setOverlayMsg('Read-only overlay updated. Ask questions in the pane.');
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
      const [answerUncertain, setAnswerUncertain] = useState(false);
      const [progressPaused, setProgressPaused] = useState(false);
      useEffect(() => {
        try {
          if (typeof localStorage !== 'undefined') localStorage.setItem(SCOPE_KEY, JSON.stringify({ project, backend, excludeHistory, webFallback, transcriptionModel, audioSource, answerProvider, answerModel, transcriptionProvider, transcriptionChoiceVersion: 1, asrMode }));
        } catch (e) { setPersistWarning('Preferences could not be saved in this browser (storage unavailable). Settings still apply for this session.'); }
      }, [project, backend, excludeHistory, webFallback, transcriptionModel, audioSource, answerProvider, answerModel, transcriptionProvider, asrMode]);
      // Capability discovery never opts a meeting into cloud audio. Only an
      // explicit selection or persisted provider choice may select Meta.
      function resetMeetingInputs() {
        setQuestion(''); setWeb(''); setLastAsked(null); setLiveEntry(null);
        setTrackedRequest(null); setError(''); setAssistFailed(false);
        setAnswerUncertain(false); setProgressPaused(false);
      }
      const cloudUnavailable = backend === 'macos' && transcriptionProvider === 'meta' && !data.capabilities?.meta_asr;
      const windowsUnavailable = backend === 'native' && data.capabilities?.windows_transcription === false;
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
            const shown = await api('/overlay/show', { pane, action, scope: { ...scope, seq: scopeSeq.current } });
            if (!life.mounted || life.action !== action) return;
            if (shown?.accepted === false || !shown?.owner) throw new Error('This pane no longer owns the overlay. Reopen the pane to take ownership.');
            life.owner = shown.owner; life.syncedKey = scopeKey;
            setOverlayOn(true); setScopeStale(false);
            setOverlayMsg('Read-only overlay shown. Ask questions in the Meeting Copilot pane.');
          }
          await qc.invalidateQueries({ queryKey: [ID, 'status'] });
        } catch (e) { if (life.mounted) {
          setOverlayMsg('Overlay action not confirmed. Retry Show / Hide overlay; if it still fails, close and reopen this pane. Check the macOS helper and backend connection. A window may still be visible: verify before screen-sharing.');
          if (!overlayOn) setOverlayOn(false);
        } }
        finally { if (life.mounted) setPending(false); }
      }
      async function startCapture(newMeeting = false) {
        if (cloudUnavailable || windowsUnavailable || overlayNeedsHide) return false;
        const retained = !!((data.segments || []).length || data.result || (data.recent || []).length || data.error_request);
        const last = data.last_session;
        const sourceChanged = last && (
          last.backend !== backend || (backend === 'meetily'
            ? last.recording_id !== (recording || null)
            : last.transcription_model !== transcriptionModel || last.audio_source !== audioSource
              || (backend === 'macos' && ((last.transcription_provider || 'local') !== transcriptionProvider || (last.asr_mode || 'ENDPOINTING') !== asrMode))));
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
            transcription_provider: backend === 'native' ? 'local' : transcriptionProvider,
            asr_mode: backend === 'native' ? 'ENDPOINTING' : asrMode });
          if (life.mounted && ok && (newMeeting || sourceChanged)) { resetMeetingInputs(); retireLocalOverlay(); }
          return ok;
        } catch (e) {
          if (life.mounted) setError(lifecycleErrors.has(e) ? e.message : 'Start was not confirmed. Check the backend connection; close and reopen this pane to retry authorization.');
          return false;
        } finally { if (life.mounted) setPending(false); }
      }
      const answerSubmitting = useRef(false);
      const answerBlocked = !!(busy || answerUncertain || trackedRequest || Object.keys(data.requests || {}).length);
      // Bound every outgoing answer string, including stored legacy replays.
      // Reject oversized saved snapshots/identifiers rather than silently
      // changing exact replay context or routing to a truncated project/model.
      const boundedAnswer = body => {
        const limits = { question: 4000, transcript: 16000, project: 1000, web_query: 300,
          answer_provider: 100, answer_model: 200, row_id: 200, request_id: 100, replay_request_id: 100 };
        for (const [key, limit] of Object.entries(limits)) {
          if (body[key] != null && (typeof body[key] !== 'string' || body[key].length > limit)) {
            setError('Answer was not sent: question, saved context, or selected settings exceed supported field limits. Edit the question/settings and ask explicitly.');
            setAssistFailed(false);
            return null;
          }
        }
        return body;
      };
      const answerFailure = () => 'Answer failed. Check answer setup, credentials, and backend connection, then Retry if you want to resend this question and its saved context.';
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
        setAnswerUncertain(false); setProgressPaused(false);
        setLiveEntry(null); setTrackedRequest(res.request_id);
      };
      async function assistNow(extra = {}, questionOverride, scopeOverride) {
        if (!usable() || !overlayActionAt.current.mounted || answerBlocked || answerSubmitting.current || unsupported) return;
        const asked = questionOverride !== undefined ? questionOverride : question;
        const scopeUsed = scopeOverride || scope;
        const body = boundedAnswer({ question: asked, ...scopeUsed, ...extra, response_contract: 'answer-admission-v1' });
        if (!body) return;
        answerSubmitting.current = true;
        if (questionOverride !== undefined) setQuestion(questionOverride);
        setPending(true); setError(''); setAssistFailed(false);
        // Remember for explicit partial replay, not automatic transport retry.
        setLastAsked({ question: asked, extra, scope: { ...scopeUsed } });
        // Request outcome and status refresh are separate: an accepted
        // assist whose refresh fails must not be reported as failed (and
        // retried into a duplicate) — the tracked request already exists.
        try {
          const res = await api('/assist', body);
          acceptAnswer(res);
        }
        catch (e) { setAnswerUncertain(true); setError('Answer submission was not confirmed. Check the backend connection and recheck status; the request may already be running or complete. Review the result before asking again.'); setAssistFailed(true); }
        finally { answerSubmitting.current = false; setPending(false); }
        try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
        catch (e) {}
      }
      // Exact replay: resend a stored request verbatim (question, scope,
      // transcript snapshot) instead of whatever the pane currently shows.
      async function replayRequest(req) {
        if (!usable() || !overlayActionAt.current.mounted || !req || answerBlocked || answerSubmitting.current) return;
        if (excludeHistory && req.include_history !== false) {
          setError('This saved answer used past sessions. Ask a new question to keep past sessions excluded.');
          return;
        }
        if (req.include_history === false && !data.capabilities?.history_opt_out) {
          setError('History exclusion needs the updated backend. Restart the gateway when other work is idle.');
          return;
        }
        const body = boundedAnswer({ response_contract: 'answer-admission-v1', question: req.question || '', project: req.project ?? null,
          all_projects: !!req.all_projects, web_fallback: !!req.web_fallback,
          ...(req.include_history === false ? { include_history: false } : {}),
          ...(req.web_query ? { web_query: req.web_query } : {}),
          ...(req.answer_provider ? { answer_provider: req.answer_provider, answer_model: req.answer_model } : {}),
          ...(typeof req.transcript === 'string' ? { transcript: req.transcript } : {}),
          ...(req.replay_request_id ? { replay_request_id: req.replay_request_id } : {}),
          ...(req.row_id ? { row_id: req.row_id } : {}) });
        if (!body) return;
        answerSubmitting.current = true;
        if (typeof req.question === 'string') setQuestion(req.question);
        setPending(true); setError(''); setAssistFailed(false);
        // A lost replay response may still mean the backend accepted it.
        setLastAsked({ question: req.question || '', extra: { web_query: req.web_query }, scope: req });
        try {
          const res = await api('/assist', body);
          acceptAnswer(res);
        }
        catch (e) { setAnswerUncertain(true); setError('Answer submission was not confirmed. Check the backend connection and recheck status; the request may already be running or complete. Review the result before asking again.'); setAssistFailed(true); }
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
            setProgressPaused(false);
            setLiveEntry(entry);
            // A recovered poll clears a stale progress error; other errors stay.
            setError(prev => (typeof prev === 'string' && (prev.startsWith('Answer progress') || prev.startsWith('That answer is no longer'))) ? '' : prev);
            if (entry && (entry.status === 'done' || entry.status === 'error' || entry.status === 'cancelled')) {
              setAnswerUncertain(false); setAssistFailed(false);
              setError(current => /^(Answer submission|Cancellation was not confirmed|Answer progress unavailable)/.test(current) ? '' : current);
              setTrackedRequest(null);
              // TanStack invalidate resolves on refetch failure by default.
              // Keep terminal progress independently of the status cache.
              try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }, { throwOnError: true }); } catch (ignored) {}
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
              setProgressPaused(true);
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
      // A verified terminal GET is an answer even when status refetch fails.
      // Never carry it across a backend meeting-generation boundary.
      const currentLive = liveEntry && (data.generation == null || liveEntry.generation == null || data.generation === liveEntry.generation) ? liveEntry : null;
      const result = currentLive?.status === 'done' ? currentLive : data.result;
      useEffect(() => {
        if (!liveEntry || !['done', 'error', 'cancelled'].includes(liveEntry.status)) return;
        // Once status has actually observed this terminal slot, the canonical
        // result owns the view again.
        const reconciled = !status.error && (data.recent || []).some(entry => entry.request_id === liveEntry.request_id && ['done', 'error', 'cancelled'].includes(entry.status));
        if (!currentLive || reconciled) setLiveEntry(null);
      }, [liveEntry, data.generation, data.recent, !!status.error]);
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
      // Each message owns its action. A legacy summary or a retained request
      // alone is never evidence that the displayed error came from an answer.
      const answerError = data.answer_error || (data.error_source === 'answer' ? data.error : null);
      const stopError = data.stop_error || (data.error_source === 'stop' ? data.error : null);
      const legacyError = !('error_source' in data) && data.error &&
        ![data.overlay_cleanup_error, data.meetily_clear_error, answerError, stopError, data.capture_error].includes(data.error)
        ? (data.error_request ? answerFailure() : data.error) : null;
      const captureError = data.capture_error || (data.error_source === 'capture' ? data.error : null);
      const recheckStatus = async () => {
        try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }, { throwOnError: true }); }
        catch (_) { setError('Backend unavailable. Check the backend connection, installation, and plugin enablement.'); }
      };
      const errorCard = (message, label, onClick, disabled = false) => message && h('div', { role: 'alert', style: card },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' } },
          h('span', null, message), h(Button, { variant: 'secondary', disabled: pending || disabled, onClick }, label)));
      // A lost submission response cannot authorize replay. Progress failures
      // likewise recheck without borrowing another message's retry request.
      const localRecheck = assistFailed || (typeof error === 'string' && /^(Answer progress|Answer submission|Cancellation was not confirmed)/.test(error));
      const restartCapture = !data.listening && !data.stopping;
      const captureBlocked = overlayNeedsHide || cloudUnavailable || windowsUnavailable || !consent || (backend === 'meetily' && !recording)
        || (backend === 'macos' && !data.capabilities?.macos_transcription);
      const hasRecentTranscript = Array.isArray(data.segments) && data.segments.slice(-40)
        .some(segment => typeof segment?.text === 'string' && segment.text.trim());
      const contextActions = [
        ['Catch me up', 'Summarize only the recent available meeting transcript. Use sections: Summary, Decisions, Open questions. Distinguish tentative proposals from confirmed decisions. Say when the transcript does not establish something; do not imply this covers the whole meeting.'],
        ['Extract actions', 'Extract action items only from the recent available meeting transcript. For each item show Action, Owner, Deadline. Include an owner or deadline only if explicitly stated; otherwise write unspecified. Distinguish proposed actions from agreed commitments. If none are stated, say so.'],
        ['Suggest a question', 'Suggest one useful clarifying question grounded in the recent available meeting transcript, followed by one short reason to ask it. Do not invent facts, decisions, or commitments. If context is insufficient, say so.'],
      ];
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
          windowsUnavailable && h('p', { role: 'status', style: muted }, data.capabilities?.windows_detail || 'Windows capture needs the optional audio dependencies. See the Windows setup guide.'),
          !data.listening && !data.stopping && (pending || !consent || (backend === 'meetily' && !recording) || (backend === 'macos' && !data.capabilities?.macos_transcription)) && h('p', { style: { ...muted, margin: 0 }, role: 'status' },
            pending ? 'Starting…' : !consent ? 'Confirm permission with the check above to enable Start.' : (backend === 'meetily' && !recording) ? 'Select an active Meetily recording below to enable Attach.' : 'Independent macOS transcription is unavailable here — see the source card below.'),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
            data.stopping
              ? h(Button, { variant: 'secondary', disabled: pending, onClick: () => act('/pause', {}) }, 'Retry stop')
              : data.listening
              ? h(Button, { variant: 'secondary', onClick: () => act('/pause', {}) }, data.backend === 'meetily' ? 'Detach' : 'Pause listening')
              : h(Button, { disabled: pending || captureBlocked, onClick: () => startCapture() }, ((data.segments || []).length || result || (data.recent || []).length) ? (backend === 'meetily' ? 'Resume' : 'Resume listening') : (backend === 'meetily' ? 'Attach to Meetily' : 'Start listening')),
            !data.listening && !data.stopping && ((data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending || captureBlocked, title: 'Starts a fresh meeting; the earlier transcript, answers, and retry data are cleared', onClick: () => startCapture(true) }, 'New meeting'),
            (data.overlay_cleanup_error || (data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending, title: 'Clears past Q&A, transcript segments, and cached snippets for this meeting', onClick: async () => { if (typeof window !== 'undefined' && window.confirm && !window.confirm('Clear this meeting? Past questions, answers, and transcript segments are removed from the pane.')) return; if (await act('/clear')) { setQuestion(''); setLastAsked(null); setLiveEntry(null); setTrackedRequest(null); setError(''); setAssistFailed(false); } } }, 'Clear meeting'),
            h(Button, { variant: 'secondary', disabled: pending || (!data.capabilities?.overlay && !overlayNeedsHide), title: 'Show or hide the read-only meeting overlay (macOS)', onClick: toggleOverlay }, overlayNeedsHide ? 'Retry hide overlay' : overlayOn ? 'Hide overlay' : 'Show overlay')),
          h('p', { style: { ...muted, margin: 0 } }, 'Screen-share privacy: the overlay may be visible in screenshots or screen sharing. Hide it before sharing; capture exclusion is best-effort, not a guarantee.'),
          h('p', { role: 'status', style: { ...muted, margin: 0 } }, 'The overlay is read-only. Ask questions in the Meeting Copilot pane.'),
          (data.overlay_cleanup_error || overlayMsg) && h('p', { role: 'status', style: { ...muted, margin: 0 } }, data.overlay_cleanup_error || overlayMsg),
          overlayNeedsHide && h('p', { role: 'alert', style: { ...muted, margin: 0 } }, 'Overlay shutdown is not confirmed. Retry hide overlay and verify the window is gone before screen-sharing.'),
          !data.capabilities?.overlay && h('p', { role: 'status', style: { ...muted, margin: 0 } }, backend === 'native' ? 'The floating overlay is macOS-only. Use this Meeting Copilot pane on Windows.' : 'Overlay helper unavailable. Check the macOS helper build and restart the gateway when other sessions are idle.'),
          h('p', { style: { ...muted, margin: 0 } }, 'No extra audio files are saved. Disconnect starts a pause or detach; check the stop status before assuming audio is off. Resume keeps this meeting. New meeting, different settings or Clear starts fresh. Pausing may discard the last few seconds.'),
          data.capture && h('div', { style: muted }, data.capture.message || data.capture.state || ''),
          data.capture && (data.capture.warning || lossText(data.capture)) && h('div', { role: 'status', style: muted },
            ['Transcript may be incomplete',
             lossText(data.capture),
             data.capture.warning ? `: ${data.capture.warning}` : ''].join(''))),
        data.overlay_cleanup_error && h('div', { role: 'alert', style: card },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' } },
            h('span', null, data.overlay_cleanup_error),
            h(Button, { variant: 'secondary', disabled: pending, onClick: toggleOverlay }, 'Retry hide overlay'))),
        errorCard(error, localRecheck ? 'Recheck status' : 'Dismiss', async () => {
          if (localRecheck) {
            // Resume GET-only progress checks; never replay an uncertain POST.
            setAssistFailed(false); setProgressPaused(false);
            setProgressRetry(value => value + 1);
            try {
              await qc.invalidateQueries({ queryKey: [ID, 'status'] }, { throwOnError: true });
              // Only recovered progress/terminal evidence clears this message.
              // A successful status refresh alone does not prove recovery.
            } catch (_) { setError(data.stopping ? 'Backend status unavailable. Check the connection and recheck status; stopping is not yet confirmed.' : 'Answer progress unavailable. Recheck status or explicitly reset local tracking after reviewing the uncertainty.'); }
          } else setError('');
        }),
        errorCard(answerError ? answerFailure() : null, data.error_request ? 'Retry' : 'Recheck status', () =>
          data.error_request ? replayRequest(data.error_request) : recheckStatus(), !!data.error_request && answerBlocked),
        errorCard(stopError, 'Recheck status', recheckStatus),
        errorCard(legacyError, 'Recheck status', recheckStatus),
        errorCard(captureError, restartCapture ? 'Restart capture' : 'Recheck status',
          restartCapture ? () => startCapture() : recheckStatus, restartCapture && captureBlocked),
        errorCard(data.meetily_clear_error, 'Clear again', async () => {
          if (await act('/clear')) resetMeetingInputs();
        }),
        status.error && errorCard('Backend unavailable. Check the backend connection, installation, and plugin enablement.',
          'Retry', recheckStatus),
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
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-backend-label' }, 'Capture source'),
            h('select', { value: backend, disabled: data.listening, onChange: e => setBackend(e.target.value), style: selectStyle, 'aria-labelledby': 'mc-backend-label' },
              h('option', { value: 'meetily' }, 'Meetily — use your existing recording'),
              h('option', { value: 'macos', disabled: !data.capabilities?.macos_transcription, title: data.capabilities?.macos_detail || '' }, 'macOS — without Meetily'),
              h('option', { value: 'native', disabled: data.capabilities?.windows_transcription === false, title: data.capabilities?.windows_detail || 'Windows only · experimental' }, 'Windows — experimental')))),
          backend === 'macos' && !data.capabilities?.macos_transcription && h('p', { role: 'status', style: muted }, data.capabilities?.macos_detail || 'macOS transcription is unavailable on this machine.'),
          (backend === 'macos' || backend === 'native') && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Audio source'), h('select', { style: selectStyle, value: audioSource, disabled: data.listening, onChange: e => setAudioSource(e.target.value), 'aria-label': 'Audio source' },
              h('option', { value: 'microphone' }, 'Microphone'), h('option', { value: 'system' }, 'System audio'), h('option', { value: 'both' }, 'Microphone + system audio'))),
            backend === 'macos' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Transcription provider'), h('select', { style: selectStyle, value: transcriptionProvider, disabled: data.listening, onChange: e => setTranscriptionProvider(e.target.value), 'aria-label': 'Transcription provider' },
              h('option', { value: 'local' }, 'Local — faster-whisper on this Mac'),
              h('option', { value: 'meta', disabled: !data.capabilities?.meta_asr, title: data.capabilities?.meta_asr ? '' : 'Set the Meta API key in Hermes Settings → Plugins → meeting-copilot-live, then reopen this pane.' }, 'Meta Muse Voice — cloud transcription'))),
            (backend === 'native' || transcriptionProvider === 'local') && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Local transcription model'), h('select', { style: selectStyle, value: transcriptionModel, disabled: data.listening, onChange: e => setTranscriptionModel(e.target.value), 'aria-label': 'Local transcription model' },
              h('option', { value: 'tiny.en' }, 'Whisper tiny.en — fastest'), h('option', { value: 'base.en' }, 'Whisper base.en — balanced'), h('option', { value: 'small.en' }, 'Whisper small.en — higher accuracy'))),
            backend === 'native' && h('span', { style: muted }, 'Local transcription uses the default microphone and all audio on the default output, according to Audio source. First Start may download Whisper weights. ', data.capabilities?.windows_detail || 'Experimental; hardware is unverified.'),
            backend === 'macos' && transcriptionProvider === 'local' && h('span', { style: muted }, 'Runs on this Mac without Meetily. First use may download the model. Microphone or screen and system audio permission is required.'),
            backend === 'macos' && transcriptionProvider === 'meta' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Meta ASR mode'), h('select', { style: selectStyle, value: asrMode, disabled: data.listening, onChange: e => setAsrMode(e.target.value), 'aria-label': 'Meta ASR mode' },
              h('option', { value: 'ENDPOINTING' }, 'Endpointing — one segment per utterance'),
              h('option', { value: 'DIARIZATION' }, 'Diarization — speaker labels per source'),
              h('option', { value: 'PUSH_TO_TALK' }, 'Push-to-talk — single-turn finals'))),
            backend === 'macos' && transcriptionProvider === 'meta' && h('span', { role: 'status', style: muted }, 'Cloud transcription: meeting audio is sent to Meta (muse-voice-transcribe) while listening. Only final turns are kept; the API key stays on the gateway and is never shown here.')),
          backend === 'meetily' && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('span', { style: muted }, 'Start recording in Meetily, then select it here. Only the selected transcript is read.'),
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-rec-label' }, 'Active Meetily recording'),
              h('select', { value: recording, disabled: data.listening || recordings.isPending, onChange: e => setRecording(e.target.value), style: selectStyle, 'aria-labelledby': 'mc-rec-label' },
                h('option', { value: '' }, recordings.isPending ? 'Looking for active Meetily recordings…' : (Array.isArray(recordings.data) ? recordings.data : []).filter(r => r.status === 'recording').length ? 'Choose an active Meetily recording…' : 'No active Meetily recordings found — start one in Meetily'),
                ...(Array.isArray(recordings.data) ? recordings.data : []).filter(r => r.status === 'recording').map(r => h('option', { value: r.id, key: r.id }, r.title || r.id)))),
            recordings.error && h('span', { role: 'alert', style: muted }, `Meetily lookup unavailable: ${recordings.error.message}. Start or check Meetily, then retry.`),
            h('span', { style: muted }, 'Meetily saves its own recordings and transcripts. Pausing here detaches the copilot; it does not stop the Meetily recording.'))),
        h('label', { ref: composerRef, style: { display: 'grid', gap: 6 } }, h('strong', { id: 'mc-q-label' }, 'What should I say?'),
          h(Textarea, { value: question, maxLength: 4000, onChange: e => setQuestion(e.target.value.slice(0, 4000)), placeholder: 'Leave blank to answer the latest question, or type a question… (⌘/Ctrl+Enter to send)', rows: 3, 'aria-labelledby': 'mc-q-label', onKeyDown: e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !e.repeat && !e.nativeEvent?.isComposing && !busy && !unsupported) { e.preventDefault(); assistNow(); } } })),
        h('section', { 'aria-label': 'Recent meeting quick actions', style: { display: 'grid', gap: 8 } },
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
            ...contextActions.map(([label, prompt]) => h(Button, { key: label, variant: 'secondary',
              disabled: !hasRecentTranscript || answerBlocked || unsupported,
              title: 'Sends an explicit request to your selected answer model using recent available context',
              onClick: () => { if (!hasRecentTranscript) return; return assistNow({}, prompt); } }, label))),
          h('p', { style: { ...muted, margin: 0 }, 'data-recent-context-disclosure': true },
            'Each action sends a request to your selected model using recent available transcript and your selected source settings, not the whole meeting. Review the result; missing owners or deadlines remain unspecified.'),
          !hasRecentTranscript && h('p', { style: { ...muted, margin: 0 }, role: 'status' },
            'Quick actions become available after transcript text arrives. You can still type a project question above.')),
        ...(unsupported ? [h('p', { role: 'alert', style: muted }, `Suggest is unavailable: ${unsupportedReasons.join(' ')} See Answer setup below, then restart the gateway when other sessions are idle.`)] : []),
        excludeHistory && !unsupported && h('p', { role: 'status', style: muted, 'data-history-excluded': true }, 'Past sessions and web search are excluded. Only the selected project is searched.'),
        h('p', { style: { ...muted, margin: 0 } }, 'Your question, part of the transcript and selected source snippets may be sent to the answer model.'),
        webFallback && h('p', { 'data-web-fallback-disclosure': true, role: 'status', style: { ...muted, margin: 0 } }, 'Automatic web fallback is on: if local evidence is insufficient, a public query may be drafted. Filtering is not a privacy guarantee; turn it off for sensitive calls.'),
        h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
          h(Button, { disabled: answerBlocked || unsupported, onClick: () => assistNow() }, busy ? 'Checking your sources…' : 'Suggest answer'),
          (answerUncertain || progressPaused || (assistFailed && trackedRequest)) && h(Button, { variant: 'secondary', disabled: pending, onClick: () => {
            if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Reset only local answer tracking? The earlier request may still be running or complete. This does not cancel it or send anything. Review available answers first. Asking again afterwards may resend the same meeting context and create a duplicate.')) return;
            setTrackedRequest(null); setLiveEntry(null); setAnswerUncertain(false);
            setProgressPaused(false); setAssistFailed(false); setError('');
          } }, 'Reset local answer tracking'),
          cancellableId && h(Button, { variant: 'secondary', onClick: async () => {
              try { await api('/assist/cancel', { request_id: cancellableId }); }
              catch (e) { setError('Cancellation was not confirmed. The answer may still be running; recheck status before asking again.'); setAssistFailed(true); return; }
              setTrackedRequest(null); setLiveEntry(null);
              try { await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
              catch (e) { setError('Answer progress unavailable after cancellation. Recheck status before asking again.'); }
            } }, 'Cancel answer'),
          h(Button, { variant: 'secondary', disabled: !result?.answer, onClick: async () => { if (!usable()) return; try { await navigator.clipboard.writeText(result.answer); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch (e) { setError('Clipboard unavailable. Select and copy the answer manually.'); } } }, copied ? 'Copied ✓' : 'Copy'),
          question && h(Button, { variant: 'secondary', disabled: busy, onClick: () => { setQuestion(''); composerRef.current?.querySelector('textarea')?.focus(); } }, 'Clear question')),
        currentLive && ['error', 'cancelled'].includes(currentLive.status) && !data.error && h('section', { style: card, 'aria-live': 'polite', 'data-terminal-answer': true },
          h('p', null, currentLive.status === 'error' ? answerFailure() : 'Answer cancelled.'),
          currentLive.request && h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(currentLive.request) }, 'Retry this answer')),
        liveEntry && ['queued', 'streaming', 'running'].includes(liveEntry.status) && h('section', { style: card, 'aria-live': 'polite', ref: answerRef },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8 } },
            h('strong', null, liveEntry.status === 'queued' ? 'Queued — waiting for a worker…' : liveEntry.streaming === false ? 'Preparing answer…' : 'Streaming answer…'),
            h('span', { style: muted }, `${liveEntry.model_route || ''}${liveEntry.streaming === false ? ' · completed mode' : ''}`)),
          h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 8 } }, liveEntry.answer || 'Contacting your sources…'),
          ...((liveEntry.warnings || []).map((w, i) => h('p', { key: `lw-${i}`, style: muted }, w)))),
        result && h('section', { style: card, 'aria-live': 'polite', ref: answerRef, 'data-answer-result': true },
          h(Button, { variant: 'secondary', disabled: !result.answer, onClick: async () => {
            if (!usable()) return;
            if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Copy this answer and its source excerpts? The clipboard may include confidential project text or links. Review before sharing. No new model request is sent.')) return;
            try { await navigator.clipboard.writeText(evidenceReport(result)); notify('info', 'Answer and evidence copied. Review before sharing.'); }
            catch (_) { setError('Clipboard unavailable. Select and copy the answer and evidence manually.'); }
          } }, 'Copy answer + evidence'),
          h('div', { style: { display: 'flex', justifyContent: 'space-between' } }, h('strong', null, 'Suggested answer'), h('span', { style: muted }, `${Number.isFinite(result.elapsed_seconds) ? `${result.elapsed_seconds.toFixed(1)}s` : ''}${result.model_route ? ` · ${result.model_route}` : ''}`)),
          h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 10 } }, result.answer),
          result.transcript_snapshot?.schema === 'transcript-snapshot-v1' && h('details', { style: { marginTop: 12 } },
            h('summary', { style: { cursor: 'pointer', ...muted } }, 'Transcript context sent with this answer'),
            h('p', { style: muted }, 'Frozen recent context, not the current transcript or a complete meeting. References identify excerpts, not verified claims. Backend times are uninterpreted; no exact utterance time or playback offset is verified. Missing owners, dates and timestamps must not be inferred.'),
            h('p', { style: muted }, `${result.transcript_snapshot.omitted_segments || 0} segment(s) omitted by the context limit.`),
            ...(result.transcript_snapshot.segments || []).map((s, i) => h('div', { key: i, style: { ...muted, whiteSpace: 'pre-wrap', paddingTop: 8 } },
              h('strong', null, `[${s.ref}] ${s.speaker}: `), s.text,
              s.shortened && h('span', null, ' (excerpt shortened)'),
              h('div', null, s.timestamp == null ? 'Time unavailable' : `${s.timestamp_kind}: ${s.timestamp} (backend metadata, not verified utterance time)`))),
            h(Button, { variant: 'secondary', disabled: typeof result.transcript_snapshot.provider_json !== 'string' || result.transcript_snapshot.provider_json.length > 16000, onClick: async () => {
              if (!usable()) return;
              if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Copy the exact transcript context sent for this answer? It can contain confidential meeting text and metadata. Clipboard history or sync may retain it. Review before sharing.')) return;
              try { await navigator.clipboard.writeText(result.transcript_snapshot.provider_json); notify('info', 'Sent transcript context copied. Review before sharing.'); }
              catch (_) { setError('Clipboard unavailable. Select and copy the sent context manually.'); }
            } }, 'Copy sent transcript context')),

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
            const assigned = {};
            for (const seg of transcript) {
              const name = String(seg.speaker || 'Speaker');
              if (!(name in assigned)) assigned[name] = speakerPalette[Object.keys(assigned).length % speakerPalette.length];
            }
            return Object.keys(assigned).length > 1 ? [h('div', { key: 'speakers', style: { display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 } },
              ...Object.entries(assigned).map(([name, color]) => h('span', { key: name, style: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 999, padding: '2px 9px', fontSize: 12 } },
                h('span', { style: { color } }, '\u25cf '), name)))] : [];
          })(),
          h('div', { style: { maxHeight: 280, overflowY: 'auto', marginTop: 8, padding: 10, display: 'flex', flexDirection: 'column', gap: 8, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, background: 'var(--ui-bg-card)' } },
            ...(transcript.length ? transcript.slice(-30).map((s, i) => {
              const order = [];
              for (const seg of transcript) { const n = String(seg.speaker || 'Speaker'); if (!order.includes(n)) order.push(n); }
              const color = speakerPalette[Math.max(0, order.indexOf(String(s.speaker || 'Speaker'))) % speakerPalette.length];
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
        h('summary', { style: { cursor: 'pointer', fontWeight: 700, color: 'var(--ui-text-primary)', padding: '12px 0', fontSize: 14 } }, setupOpen ? '\u25be Answer setup' : '\u25b8 Answer setup'),
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 14, paddingTop: 4 } },

        h('label', { style: { display: 'grid', gap: 6 } }, h('strong', { id: 'mc-scope-label' }, 'Search scope'),
          h('select', { value: project, onChange: e => setProject(e.target.value), style: selectStyle, 'aria-labelledby': 'mc-scope-label', disabled: projects.isPending },
            h('option', { value: '' }, 'Past sessions only'),
            h('option', { value: '__all_projects__', disabled: !data.capabilities?.all_projects }, 'All projects + sessions'),
            ...entries.map(p => { const value = typeof p === 'string' ? p : (p.path || p.name); return h('option', { value, key: value }, typeof p === 'string' ? p.split(/[\\/]/).pop() : (p.name || value)); }))),
        projects.error && h('div', { role: 'alert', style: muted }, `Project lookup unavailable: ${projects.error.message}`),
        allProjects && h('p', { style: { ...muted, margin: 0 } }, 'Searches supported text files across up to 100 eligible project folders plus past sessions, not your whole computer. Evidence can include unrelated projects; review it before sharing.'),
        h('label', { style: { ...muted, display: 'flex', gap: 8, alignItems: 'flex-start' } },
          h('input', { type: 'checkbox', checked: excludeHistory, disabled: answerBlocked || overlayOn || data.overlay_shown === true || data.overlay_shown === null || overlayNeedsHide, onChange: e => setExcludeHistory(e.target.checked), 'aria-label': 'Exclude past sessions' }),
          h('span', null, 'Exclude past sessions. Choose one project and turn off web search. Hide the overlay and finish any answer before changing this setting.')),
        h('section', { style: { ...card, display: 'grid', gap: 10 } },
          h('span', { id: 'mc-model-label', style: { fontWeight: 700 } }, 'Answer model'),
          h('div', { style: { display: 'grid', gap: 8 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-provider-label' }, 'Answer provider'),
              h('select', { style: selectStyle, value: answerProvider, disabled: busy || !data.capabilities?.independent_models, onChange: e => { setAnswerProvider(e.target.value); setAnswerModel(''); }, 'aria-labelledby': 'mc-provider-label' },
                h('option', { value: '' }, 'Choose provider…'), ...providers.map(p => h('option', { value: p.id, key: p.id }, p.label || p.id)))),
            answerProvider && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Answer model'),
              h('input', { style: selectStyle, value: answerModel, disabled: busy, maxLength: 200, placeholder: 'Exact model ID from your provider', onChange: e => setAnswerModel(e.target.value), 'aria-label': 'Answer model', autoComplete: 'off' }))),
          answerProvider && h('p', { role: 'status', style: muted }, providers.find(p => p.id === answerProvider)?.status || 'Provider unavailable; choose OpenAI or OpenRouter.'),
          modelCatalog.error && h('p', { role: 'alert', style: { ...muted, margin: 0 } }, 'Provider readiness unavailable. Check Hermes plugin secret settings.'),
          h('p', { style: { ...muted, margin: 0 } }, 'This choice is only for Copilot answers, not chat or transcription.')),
        h('label', { style: { ...muted, display: 'flex', gap: 8, alignItems: 'flex-start', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '10px 12px', background: 'var(--ui-bg-card)', cursor: 'pointer' } },
          h('input', { type: 'checkbox', checked: webFallback, onChange: e => setWebFallback(e.target.checked), 'aria-label': 'Automatic web fallback' }),
          h('span', null, webFallback ? 'Automatic web fallback: on. A public query may be drafted if local evidence is insufficient. Filtering is best-effort, not a privacy guarantee; turn off for sensitive calls.' : 'Automatic web fallback: off. Turn on to search the web when local evidence is insufficient.')),
        h('section', { style: { ...card, display: 'grid', gap: 10 } },
          h('strong', null, 'Meeting overlay (macOS)'),
          h('p', { style: { ...muted, marginBottom: 0 } }, 'The overlay displays the transcript and detected questions. Ask and view answers in the Meeting Copilot pane. The overlay may appear in screen shares.'),

          scopeStale && overlayOn && h('p', { role: 'status', style: { ...muted, margin: 0 } }, 'Settings changed — syncing overlay scope…'),
          (data.overlay_cleanup_error || overlayMsg) && h('p', { role: 'status', style: { ...muted, margin: 0 } }, data.overlay_cleanup_error || overlayMsg),
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
        h('details', null, h('summary', { style: { cursor: 'pointer', ...muted, padding: '4px 0' } }, 'Search online with a specific query'),
          h('div', { style: { display: 'grid', gap: 8, paddingTop: 10 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Specific public web query'),
              h(Input, { value: web, maxLength: 300, onChange: e => setWeb(e.target.value.slice(0, 300)), placeholder: 'Public query — no client names or meeting details', 'aria-label': 'Specific public web query' })),
            h('span', { 'data-web-query-disclosure': true, style: muted }, 'This query is sent to the web and overrides automatic fallback for this request. Do not include private details.'),
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
        return { position: 'fixed', left: Math.max(8, Math.min(pos.x, window.innerWidth - w)), top: Math.max(8, Math.min(pos.y, window.innerHeight - h)), zIndex: 9999, minWidth: 210, maxWidth: w, background: 'var(--ui-bg-elevated)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: 4, boxShadow: '0 8px 24px color-mix(in srgb, var(--ui-text-primary) 25%, transparent)' };
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
