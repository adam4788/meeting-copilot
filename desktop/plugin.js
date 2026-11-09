import { createElement as h, useState, useEffect, useRef, useSyncExternalStore } from 'react';
import { host, atom, useValue, useQuery, useQueryClient, Button, Input, Textarea, Badge, PALETTE_AREA } from '@hermes/plugin-sdk';

const ID = 'meeting-copilot-live';
const speakerPalette = ['var(--ui-accent)', 'var(--ui-green, var(--ui-accent))', 'var(--ui-orange, var(--ui-accent))', 'var(--ui-red, var(--ui-accent))', 'var(--ui-cyan, var(--ui-accent))', 'var(--ui-purple, var(--ui-accent))'];
const muted = { color: 'var(--ui-text-secondary)', fontSize: 12, lineHeight: 1.5 };
const card = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 14, minWidth: 0, boxSizing: 'border-box', background: 'var(--ui-bg-card)' };
const selectStyle = { padding: 8, borderRadius: 6, width: '100%', maxWidth: '100%', minWidth: 0, boxSizing: 'border-box', color: 'var(--ui-text-primary)', background: 'var(--ui-bg-card)', border: '1px solid var(--ui-stroke-secondary)' };

// Portable evidence is assembled only from the displayed answer, never current
// meeting state, credentials, or request snapshots. Clipboard remains explicit.
// Python bounds model evidence by Unicode code points; JS string.slice counts
// UTF-16 units instead. Iterate only to the bound, even for a huge input.
function evidenceExcerpt(value) {
  const chars = [];
  for (const char of (typeof value === 'string' ? value : '')) {
    if (chars.length === 2050) return { text: chars.join(''), truncated: true };
    chars.push(char);
  }
  return { text: chars.join(''), truncated: false };
}
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
    'Numbering matches retrieved sources in the answer pane, not meeting segments or timestamps. Excerpts are limited to 2,050 characters each.');
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
    const excerpt = evidenceExcerpt(s.text);
    lines.push('Excerpt:', excerpt.text || '(No excerpt available)');
    if (excerpt.truncated) lines.push('[Excerpt limited to 2,050 characters.]');
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
        h('p', { style: muted }, 'Configure OpenAI or OpenRouter in Hermes Settings → Plugins → meeting-copilot-live (default profile; Agent settings on newer hosts). Keys stay in Hermes-managed secret settings. Never enter keys in chat or model fields. Reopen the pane after changing keys. Local transcription does not use an answer key.'));
    }
    const request = (path, body) => ctx.rest(path, body === undefined ? {} : { method: 'POST', body });
    // Survives plugin unregister/re-register in the SAME renderer. Neither a
    // wall clock nor HTTP arrival can establish pane creation order.
    const orderKey = Symbol.for('meeting-copilot.pane-creation.v1');
    const order = globalThis[orderKey] ||= { client: globalThis.crypto?.randomUUID?.(), next: 0 };
    const registration = order.registration = (order.registration || 0) + 1;
    let active = true;
    let paneRegistration = null;
    let paneIds = 0;
    const registered = () => active && order.registration === registration;
    const lifecycles = new Set();
    const notify = (kind, message) => { try { host.notify({ kind, message }); } catch (e) {} };
    const SCOPE_KEY = 'meeting-copilot.scope.v2';
    const PREFERENCE_KEY = 'preferences-v1';
    const preferenceFields = ['project', 'backend', 'excludeHistory', 'webFallback', 'transcriptionModel', 'audioSource', 'answerProvider', 'answerModel', 'transcriptionProvider', 'transcriptionChoiceVersion', 'asrMode'];
    // Match the backend's credential-like model rejection before persistence.
    // This is defense in depth, not a general secret detector.
    const secretLike = value => typeof value === 'string' && /sk-|ghp_|github_pat_|bearer|api[_-]?key|password|access[_-]?token|refresh[_-]?token|eyJ[A-Za-z0-9_-]{10,}/i.test(value);
    // Paths are not model IDs: words like "tokenizer" and "password-manager"
    // can be legitimate names. Reject only credential-shaped path segments.
    const secretPath = value => typeof value === 'string' && /(?:^|[\\/])(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}/i.test(value);
    const safeModel = value => value === '' || (typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,199}$/.test(value) && !value.includes('://') && !secretLike(value));
    function loadPreferences() {
      try {
        let stored = null, current = null, legacy = null;
        try { stored = ctx.storage?.get(PREFERENCE_KEY, null); } catch (_) {}
        try { if (typeof localStorage !== 'undefined') current = localStorage.getItem(SCOPE_KEY); } catch (_) {}
        try { if (typeof localStorage !== 'undefined') legacy = localStorage.getItem('meeting-copilot.scope'); } catch (_) {}
        const parsed = stored && typeof stored === 'object' ? stored : JSON.parse(current || '{}');
        const out = {};
        for (const k of ['project', 'backend', 'transcriptionModel', 'audioSource', 'answerProvider', 'answerModel', 'transcriptionProvider', 'asrMode']) {
          if (typeof parsed?.[k] === 'string') out[k] = parsed[k].slice(0, 500);
        }
        for (const k of ['webFallback', 'excludeHistory']) if (typeof parsed?.[k] === 'boolean') out[k] = parsed[k];
        if (parsed?.transcriptionChoiceVersion === 1) out.transcriptionChoiceVersion = 1;
        if (!stored && !current && legacy) {
          try {
            const old = JSON.parse(legacy);
            // Never migrate legacy web defaults or one-shot queries as opt-in.
            for (const k of ['project', 'backend', 'transcriptionModel', 'audioSource', 'answerProvider', 'answerModel', 'transcriptionProvider', 'asrMode'])
              if (typeof old?.[k] === 'string') out[k] = old[k].slice(0, 500);
          } catch (_) {}
        }
        if (!['openai', 'openrouter'].includes(out.answerProvider)) { delete out.answerProvider; delete out.answerModel; }
        for (const key of Object.keys(out)) if (key === 'project' ? secretPath(out[key]) : secretLike(out[key])) delete out[key];
        if (out.answerModel && !safeModel(out.answerModel)) delete out.answerModel;
        for (const [key, choices] of Object.entries({ backend: ['meetily', 'macos', 'native'], audioSource: ['microphone', 'system', 'both'],
          transcriptionModel: ['tiny.en', 'base.en', 'small.en'], transcriptionProvider: ['local', 'meta'], asrMode: ['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK'] })) {
          if (out[key] !== undefined && !choices.includes(out[key])) delete out[key];
        }
        if (out.transcriptionProvider === 'meta' && parsed?.transcriptionChoiceVersion !== 1) out.transcriptionProvider = 'local';
        return out;
      } catch (_) { return {}; }
    }
    const loaded = loadPreferences();
    const preferences = atom({ project: '', backend: 'meetily', webFallback: false, transcriptionModel: 'base.en', audioSource: 'microphone', answerProvider: '', answerModel: '', transcriptionProvider: 'local', asrMode: 'ENDPOINTING', ...loaded,
      excludeHistory: loaded.excludeHistory !== undefined ? loaded.excludeHistory : !loaded.webFallback });
    const storageFailed = atom(false);
    let captureRevision = 0; // memory-only consent fence; never persist consent
    function savePreferences(patch) {
      if (Object.entries(patch).some(([key, value]) => (key === 'project' ? secretPath(value) : secretLike(value)) || (key === 'answerModel' && !safeModel(value)))) {
        notify('warning', 'Do not enter API keys or secret-like values in preference fields. Use Hermes-managed Agent settings for keys.');
        return;
      }
      const previous = preferences.get();
      const next = { ...previous, ...patch };
      if (['backend', 'audioSource', 'transcriptionModel', 'transcriptionProvider', 'asrMode'].some(key => previous[key] !== next[key])) captureRevision++;
      preferences.set(next);
      const safe = {};
      for (const key of preferenceFields) if (next[key] !== undefined) safe[key] = next[key];
      let saved = false;
      try {
        const encoded = JSON.stringify(safe);
        if (ctx.storage?.set && ctx.storage?.get) {
          ctx.storage.set(PREFERENCE_KEY, safe);
          saved = JSON.stringify(ctx.storage.get(PREFERENCE_KEY, null)) === encoded;
        } else if (typeof localStorage !== 'undefined') {
          localStorage.setItem(SCOPE_KEY, encoded);
          saved = localStorage.getItem(SCOPE_KEY) === encoded;
        }
      } catch (_) { /* Restricted storage and quota failures stay session-only. */ }
      if (!saved && !storageFailed.get()) notify('warning', 'Preferences could not be saved. They apply only for this session.');
      if (storageFailed.get() !== !saved) storageFailed.set(!saved);
    }
    // Synchronous local locks cover the gaps before React renders or a status
    // poll can observe a Show/answer admission. They never hold meeting data.
    const safety = { overlayAction: false, overlayVisible: false, overlayUnknown: false, overlayToken: 0, answer: false, answerKnown: false, answerToken: 0,
      captureAction: false, captureUnknown: false, captureToken: 0, captureReleasePending: false, captureReleased: false };
    const safetySignal = atom(0);
    const signalSafety = () => safetySignal.set(safetySignal.get() + 1);
    async function checkedStatus(api) {
      // Only a GET begun after the operation can settle a local uncertainty.
      const overlayToken = safety.overlayToken, answerToken = safety.answerToken, captureToken = safety.captureToken;
      const overlayWasActive = safety.overlayAction, captureWasActive = safety.captureAction || safety.captureReleasePending;
      const result = await api('/status');
      let changed = false;
      if (overlayToken === safety.overlayToken && !overlayWasActive && !safety.overlayAction && (safety.overlayUnknown || safety.overlayVisible) &&
          result?.overlay_shown === false && result?.overlay_stop_confirmed === true) {
        safety.overlayUnknown = false; safety.overlayVisible = false; changed = true;
      }
      if (answerToken === safety.answerToken && safety.answer && safety.answerKnown && result?.busy === false &&
          result.requests && typeof result.requests === 'object' && !Array.isArray(result.requests) && Object.keys(result.requests).length === 0) {
        safety.answer = false; changed = true;
      }
      if (captureToken === safety.captureToken && !captureWasActive && !safety.captureAction && !safety.captureReleasePending && safety.captureUnknown &&
          result?.listening === false && result?.stopping === false) {
        safety.captureUnknown = false; changed = true;
      }
      if (captureToken === safety.captureToken && !captureWasActive && !safety.captureAction && !safety.captureReleasePending && safety.captureReleased &&
          (result?.listening === true || result?.stopping === true)) {
        // Newer active evidence supersedes release proof and any older GET.
        safety.captureReleased = false; safety.captureUnknown = true; safety.captureToken += 1; changed = true;
      }
      if (changed) signalSafety();
      return result;
    }
    function hasMountedPane() {
      return !!(registered() && paneRegistration?.active && paneRegistration.life?.mounted);
    }
    function mayChangePreferences(qc, status, live = () => true) {
      if (!registered() || !live()) return false;
      // With no mounted pane these are deferred defaults only. Do not poll the
      // legacy /status endpoint: before a pane claim it can renew capture.
      if (!hasMountedPane()) return true;
      const latest = typeof qc.getQueryState === 'function' ? qc.getQueryState([ID, 'status']) : null;
      const state = latest || status;
      const data = state?.data;
      return !!(!state?.error && state?.status !== 'error' && state?.status !== 'pending' && !state?.isPending
        && data?.overlay_shown === false && data?.overlay_stop_confirmed === true && data?.busy === false && data.requests && typeof data.requests === 'object'
        && !Array.isArray(data.requests) && Object.keys(data.requests).length === 0 && !data.overlay_cleanup_error
        && !safety.overlayAction && !safety.overlayVisible && !safety.overlayUnknown && !safety.answer);
    }
    const updatePreferences = (patch, qc, status, live) => {
      if (!mayChangePreferences(qc, status, live)) return false;
      savePreferences(patch);
      return true;
    };
    function mayChangeCapture(qc, status, live = () => true) {
      if (!registered() || !live() || safety.captureAction || safety.captureUnknown) return false;
      const state = qc.getQueryState?.([ID, 'status']) || status;
      if (!hasMountedPane()) return safety.captureReleased || (state?.data?.listening !== true && state?.data?.stopping !== true); // deferred defaults, never a capture action
      return captureStatusIdle(state);
    }
    function captureStatusIdle(state) {
      return !!(!state?.error && state?.status !== 'error' && state?.status !== 'pending' && !state?.isPending &&
        (!state?.fetchStatus || state.fetchStatus === 'idle') &&
        state?.data?.listening === false && state.data.stopping === false);
    }
    const captureKey = prefs => JSON.stringify([prefs.backend, prefs.audioSource, prefs.transcriptionModel, prefs.transcriptionProvider, prefs.asrMode]);
    function answerScope(prefs) {
      const all = prefs.project === '__all_projects__';
      return { project: all ? null : prefs.project || null, all_projects: all, web_fallback: prefs.webFallback,
        ...(prefs.excludeHistory ? { include_history: false } : {}),
        ...(prefs.answerProvider && prefs.answerModel ? { answer_provider: prefs.answerProvider, answer_model: prefs.answerModel } : {}) };
    }
    let settingsAvailable = false;
    // A disabled useQuery can still replace the shared query's queryFn. Observe
    // the cache directly so Settings never installs a /status fetch that could
    // renew an old capture lease after its pane has gone away.
    const emptyQuery = Object.freeze({ data: undefined, error: null, status: 'pending' });
    function useCachedQuery(qc, kind) {
      const snapshot = () => qc.getQueryState?.([ID, kind]) || emptyQuery;
      const state = useSyncExternalStore(listener => qc.getQueryCache?.()?.subscribe(listener) || (() => {}), snapshot, snapshot);
      return { ...state, isPending: !!state.isPending || state.status === 'pending' };
    }
    function AnswerPreferences({ prefs, status, projects, models, qc, live }) {
      const data = status.data || {};
      const unsaved = useValue(storageFailed);
      const { project, excludeHistory, answerProvider, answerModel, webFallback } = prefs;
      const allProjects = project === '__all_projects__';
      const mounted = hasMountedPane();
      const providers = mounted ? (models.data?.providers || []) : [{ id: 'openai', label: 'OpenAI API' }, { id: 'openrouter', label: 'OpenRouter' }];
      const entries = Array.isArray(projects.data) ? projects.data : (projects.data?.projects || []);
      const locked = !mayChangePreferences(qc, status, live);
      const change = patch => updatePreferences(patch, qc, status, live);
      return h('div', { style: { display: 'grid', gap: 14 }, 'aria-label': 'Answer preferences' },
        h('p', { style: { ...muted, margin: 0 } }, 'These preferences apply to future answers. Changing them does not start capture or send a request.'),
        unsaved && h('p', { role: 'alert', style: { ...muted, margin: 0 } }, 'Preferences could not be saved. They apply only for this session.'),
        !mounted && h('p', { role: 'status', style: { ...muted, margin: 0 } }, unsaved
          ? 'The pane is closed. Changes apply only while this session remains open; they do not modify an existing overlay or answer. Open the pane to check backend status and available projects.'
          : 'The pane is closed. Changes are saved as defaults for the next pane; they do not modify an existing overlay or answer. Open the pane to check backend status and available projects.'),
        locked && h('p', { role: 'status', style: { ...muted, margin: 0 } }, 'To change answer settings, hide the overlay, finish or cancel any answer, and wait for backend status to confirm both are idle. If status is unavailable or shutdown is unconfirmed, reopen the pane and check it.'),
        h('label', { style: { display: 'grid', gap: 6 } }, h('strong', { id: 'mc-scope-label' }, 'Search scope'),
          h('select', { value: project, style: selectStyle, 'aria-labelledby': 'mc-scope-label', disabled: locked || (mounted && projects.isPending),
            onChange: e => {
              const value = e.target.value;
              const known = entries.some(p => (typeof p === 'string' ? p : (p.path || p.name)) === value);
              const isMounted = hasMountedPane();
              const latest = typeof qc.getQueryState === 'function' ? qc.getQueryState([ID, 'status']) : status;
              if ((!isMounted || !projects.isPending) && (value === '' || (value === '__all_projects__' && (!isMounted || latest?.data?.capabilities?.all_projects)) || known || value === project)) change({ project: value });
            } },
            h('option', { value: '' }, 'Past sessions only'),
            h('option', { value: '__all_projects__', disabled: mounted && !data.capabilities?.all_projects }, 'All projects + sessions'),
            project && !allProjects && !entries.some(p => (typeof p === 'string' ? p : (p.path || p.name)) === project) && h('option', { value: project }, project),
            ...entries.map(p => { const value = typeof p === 'string' ? p : (p.path || p.name); return h('option', { value, key: value }, typeof p === 'string' ? p.split(/[\\/]/).pop() : (p.name || value)); }))),
        !mounted && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Project path (optional)'),
          h('input', { style: selectStyle, value: allProjects ? '' : project, disabled: locked, maxLength: 500, placeholder: 'Project folder path', 'aria-label': 'Project path', onChange: e => { if (!hasMountedPane()) change({ project: e.target.value.slice(0, 500) }); } })),
        projects.error && h('div', { role: 'alert', style: muted }, 'Project lookup unavailable. Check the backend connection.'),
        allProjects && h('p', { style: { ...muted, margin: 0 } }, 'Searches supported text files across up to 100 eligible project folders plus past sessions, not your whole computer. Evidence can include unrelated projects; review it before sharing.'),
        h('label', { style: { ...muted, display: 'flex', gap: 8, alignItems: 'flex-start' } },
          h('input', { type: 'checkbox', checked: excludeHistory, disabled: locked, onChange: e => change({ excludeHistory: e.target.checked }), 'aria-label': 'Exclude past sessions' }),
          h('span', null, 'Exclude past sessions. Choose one project and turn off web search.')),
        h('section', { style: { ...card, display: 'grid', gap: 10 } },
          h('strong', null, 'Answer model'),
          h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-provider-label' }, 'Answer provider'),
            h('select', { style: selectStyle, value: answerProvider, disabled: locked || (mounted && !data.capabilities?.independent_models), 'aria-labelledby': 'mc-provider-label',
              onChange: e => { if ((!hasMountedPane() || data.capabilities?.independent_models) && ['', 'openai', 'openrouter'].includes(e.target.value)) change({ answerProvider: e.target.value, answerModel: '' }); } },
              h('option', { value: '' }, 'Choose provider…'), ...providers.map(p => h('option', { value: p.id, key: p.id }, p.label || p.id)))),
          answerProvider && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Answer model'),
            h('input', { style: selectStyle, value: answerModel, disabled: locked, maxLength: 200, placeholder: 'Exact model ID from your provider', onChange: e => change({ answerModel: e.target.value.slice(0, 200) }), 'aria-label': 'Answer model', autoComplete: 'off' })),
          answerProvider && h('p', { role: 'status', style: muted }, mounted ? (providers.find(p => p.id === answerProvider)?.status || 'Provider unavailable; choose OpenAI or OpenRouter.') : 'Provider readiness will be checked when you open the pane.'),
          models.error && h('p', { role: 'alert', style: { ...muted, margin: 0 } }, 'Provider readiness unavailable. Check Hermes plugin secret settings.'),
          h('p', { style: { ...muted, margin: 0 } }, 'Only for Copilot answers, not chat or transcription.')),
        h('label', { style: { ...muted, display: 'flex', gap: 8, alignItems: 'flex-start', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '10px 12px', background: 'var(--ui-bg-card)' } },
          h('input', { type: 'checkbox', checked: webFallback, disabled: locked, onChange: e => change({ webFallback: e.target.checked }) }),
          h('span', null, webFallback ? 'Automatic web fallback: on. A public query may be drafted if local evidence is insufficient. Filtering is best-effort, not a privacy guarantee; turn off for sensitive calls.' : 'Automatic web fallback: off. Turn on to search the web when local evidence is insufficient.')),
        h(MetaKeySettings));
    }
    function CapturePreferences({ prefs, status, qc, live }) {
      const mounted = hasMountedPane();
      const data = status.data || {};
      const { backend, audioSource, transcriptionProvider, transcriptionModel, asrMode } = prefs;
      const locked = !mayChangeCapture(qc, status, live);
      const change = (field, value, choices) => {
        if (!choices.includes(value) || !mayChangeCapture(qc, status, live)) return;
        const latest = qc.getQueryState?.([ID, 'status']) || status;
        const caps = latest?.data?.capabilities || {};
        if (hasMountedPane() && ((field === 'backend' && value === 'macos' && !caps.macos_transcription) ||
            (field === 'backend' && value === 'native' && caps.windows_transcription !== true) ||
            (field === 'transcriptionProvider' && value === 'meta' && !caps.meta_asr))) return;
        savePreferences({ [field]: value, ...(field === 'transcriptionProvider' ? { transcriptionChoiceVersion: 1 } : {}) });
      };
      return h('section', { style: { ...card, display: 'grid', gap: 10 }, 'aria-label': 'Transcript source preferences' },
        h('strong', null, 'Transcript source'),
        h('p', { style: { ...muted, margin: 0 } }, 'These defaults apply to the next capture. Changing them does not start capture. Select the active meeting and confirm permission in the pane.'),
        !mounted && h('p', { role: 'status', style: muted }, 'The pane is closed. Source and provider readiness will be checked when you open it.'),
        locked && h('p', { role: 'status', style: muted }, 'Pause capture and wait for confirmed idle status before changing transcript settings. If status is unavailable, reopen the pane and check it.'),
        h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-backend-label' }, 'Capture source'),
          h('select', { value: backend, disabled: locked, style: selectStyle, 'aria-labelledby': 'mc-backend-label', onChange: e => change('backend', e.target.value, ['meetily', 'macos', 'native']) },
            h('option', { value: 'meetily' }, 'Meetily — use your existing recording'),
            h('option', { value: 'macos', disabled: mounted && !data.capabilities?.macos_transcription, title: data.capabilities?.macos_detail || '' }, 'macOS — without Meetily'),
            h('option', { value: 'native', disabled: mounted && data.capabilities?.windows_transcription !== true, title: data.capabilities?.windows_detail || 'Windows only · experimental' }, 'Windows — experimental'))),
        mounted && !status.isPending && !status.error && !data.capabilities?.macos_transcription && h('p', { role: 'status', style: muted }, data.capabilities?.macos_detail || 'macOS transcription is unavailable on this machine.'),
        mounted && !status.isPending && !status.error && data.capabilities?.windows_transcription !== true && h('p', { role: 'status', style: muted }, data.capabilities?.windows_detail || 'Windows transcription is unavailable or readiness was not reported.'),
        (backend === 'macos' || backend === 'native') && h('div', { style: { display: 'grid', gap: 8 } },
          h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Audio source'),
            h('select', { style: selectStyle, value: audioSource, disabled: locked, 'aria-label': 'Audio source', onChange: e => change('audioSource', e.target.value, ['microphone', 'system', 'both']) },
              h('option', { value: 'microphone' }, 'Microphone'), h('option', { value: 'system' }, 'System audio'), h('option', { value: 'both' }, 'Microphone + system audio'))),
          backend === 'macos' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Transcription provider'),
            h('select', { style: selectStyle, value: transcriptionProvider, disabled: locked, 'aria-label': 'Transcription provider', onChange: e => change('transcriptionProvider', e.target.value, ['local', 'meta']) },
              h('option', { value: 'local' }, 'Local — faster-whisper on this Mac'),
              h('option', { value: 'meta', disabled: mounted && !data.capabilities?.meta_asr, title: mounted && !data.capabilities?.meta_asr ? 'Set the Meta API key in Hermes Settings → Plugins → meeting-copilot-live, then reopen the pane.' : '' }, 'Meta Muse Voice — cloud transcription'))),
          backend === 'macos' && mounted && !status.isPending && !status.error && !data.capabilities?.meta_asr && h('p', { role: 'status', style: muted }, 'Meta transcription is unavailable. Set the Meta API key in managed Agent settings, then reopen the pane to recheck readiness.'),
          (backend === 'native' || transcriptionProvider === 'local') && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Local transcription model'),
            h('select', { style: selectStyle, value: transcriptionModel, disabled: locked, 'aria-label': 'Local transcription model', onChange: e => change('transcriptionModel', e.target.value, ['tiny.en', 'base.en', 'small.en']) },
              h('option', { value: 'tiny.en' }, 'Whisper tiny.en — fastest'), h('option', { value: 'base.en' }, 'Whisper base.en — balanced'), h('option', { value: 'small.en' }, 'Whisper small.en — higher accuracy'))),
          backend === 'native' && h('span', { style: muted }, 'Local transcription uses the default microphone and all audio on the default output, according to Audio source. First Start may download Whisper weights. ', data.capabilities?.windows_detail || 'Experimental; hardware is unverified.'),
          backend === 'macos' && transcriptionProvider === 'local' && h('span', { style: muted }, 'Runs on this Mac without Meetily. First use may download the model. Microphone or screen and system audio permission is required.'),
          backend === 'macos' && transcriptionProvider === 'meta' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Meta ASR mode'),
            h('select', { style: selectStyle, value: asrMode, disabled: locked, 'aria-label': 'Meta ASR mode', onChange: e => change('asrMode', e.target.value, ['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK']) },
              h('option', { value: 'ENDPOINTING' }, 'Endpointing — one segment per utterance'), h('option', { value: 'DIARIZATION' }, 'Diarization — speaker labels per source'), h('option', { value: 'PUSH_TO_TALK' }, 'Push-to-talk — single-turn finals'))),
          backend === 'macos' && transcriptionProvider === 'meta' && h('span', { role: 'status', style: muted }, 'Cloud transcription: meeting audio is sent to Meta (muse-voice-transcribe) while listening. Only final turns are kept; the API key stays in managed secret settings.')));
    }
    function SettingsPage() {
      const qc = useQueryClient();
      const live = useRef(true);
      useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
      useValue(safetySignal);
      const prefs = useValue(preferences);
      // Observe the pane's cache, but never cause Settings to fetch on its own.
      const status = useCachedQuery(qc, 'status');
      const projects = useCachedQuery(qc, 'projects');
      const models = useCachedQuery(qc, 'models');
      return h('section', { style: { display: 'grid', gap: 14, maxWidth: 720, padding: 16 } },
        h('h2', { style: { margin: 0 } }, 'Meeting Copilot settings'),
        h(CapturePreferences, { prefs, status, qc, live: () => live.current }),
        h('h3', { style: { margin: 0 } }, 'Answer setup'),
        h(AnswerPreferences, { prefs, status, projects, models, qc, live: () => live.current }));
    }
    if (typeof ctx.registerSettingsPage === 'function') {
      try {
        const dispose = ctx.registerSettingsPage({ id: 'settings', title: 'Meeting Copilot', icon: 'comment-discussion', render: () => h(SettingsPage) });
        settingsAvailable = typeof dispose === 'function';
      } catch (_) { /* Older/partial hosts keep the complete inline controls. */ }
    }
    function releaseOverlayPane(life) {
      lifecycles.delete(life);
      if (life.release) return life.release;
      if (!life.pane) return Promise.resolve({ accepted: false });
      const overlayToken = ++safety.overlayToken;
      // A late claim from an older lifecycle may release its own pane but must
      // not alter capture evidence belonging to a newer pane.
      const captureToken = registered() && life.creation === order.next ? ++safety.captureToken : null;
      if (captureToken !== null) {
        safety.captureReleasePending = true; safety.captureUnknown = true; safety.captureReleased = false;
      }
      if (safety.overlayVisible || safety.overlayAction || life.owner) safety.overlayUnknown = true;
      signalSafety();
      life.released = true;
      try { life.release = Promise.resolve(request('/overlay/release', { pane: life.pane, action: ++life.action })); }
      catch (error) { life.release = Promise.reject(error); }
      // Observe cleanup failures too; Restart still receives the rejection.
      const warnStop = () => notify('warning', 'Capture stop is not confirmed. Lease expiry initiates stop but cannot guarantee physical shutdown. Reopen Meeting Copilot to check status and Retry stop; verify the OS audio indicator.');
      const finishCaptureRelease = result => {
        if (captureToken === null || captureToken !== safety.captureToken) return;
        safety.captureReleasePending = false;
        if (result?.accepted === true && result?.stop_confirmed === true && !result?.stopping) {
          safety.captureUnknown = false; safety.captureReleased = true;
        }
        signalSafety();
      };
      life.release.then(result => {
        finishCaptureRelease(result);
        if (overlayToken === safety.overlayToken && result?.accepted === true && result?.overlay_stop_confirmed === true) {
          safety.overlayVisible = false; safety.overlayUnknown = false;
          signalSafety();
        }
        if (result?.stop_confirmed === false || result?.stopping) warnStop();
        if (result?.overlay_stop_confirmed === false) notify('warning', result.overlay_cleanup_error || 'Overlay shutdown is not confirmed. Reopen the pane and Retry hide overlay.');
      }, () => { finishCaptureRelease(null); warnStop(); });
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
      const api = (path, body) => (active && (usable() || (path === '/status' && body === undefined))) ? request(path, body)
        : Promise.reject(new Error('This pane is retired. Close and reopen it.'));
      const qc = useQueryClient();
      const saved = preferences.get();
      const prefs = useValue(preferences);
      const { project, webFallback, answerProvider, answerModel, excludeHistory } = prefs;
      const [question, setQuestion] = useState('');
      const [web, setWeb] = useState('');
      const [consent, setConsent] = useState(false);
      const [fallbackBackend, setBackend] = useState(saved.backend || 'meetily');
      const backend = settingsAvailable ? prefs.backend : fallbackBackend;
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
      const [fallbackTranscriptionModel, setTranscriptionModel] = useState(saved.transcriptionModel || 'base.en');
      const [fallbackAudioSource, setAudioSource] = useState(saved.audioSource || 'microphone');
      const transcriptionModel = settingsAvailable ? prefs.transcriptionModel : fallbackTranscriptionModel;
      const audioSource = settingsAvailable ? prefs.audioSource : fallbackAudioSource;
      const [overlayOn, setOverlayOn] = useState(false);
      const [overlayMsg, setOverlayMsg] = useState('');
      const [copied, setCopied] = useState(false);
      const [persistWarning, setPersistWarning] = useState('');


      const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => checkedStatus(api), refetchInterval: 2500, retry: 1 });
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
      const scope = answerScope(prefs);
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
        signalSafety(); // a new pane invalidates deferred release evidence
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
        // Retire older release/GET evidence before the new pane can claim.
        safety.captureToken += 1; safety.captureReleasePending = false; safety.captureReleased = false;
        paneEntry.retired = null;  // a genuinely new effect owns its own recovery
        paneEntry.retire?.(null);
        paneEntry.life = life;
        lifecycles.add(life);
        return life;
      }
      const overlayActionAt = useRef(null);
      if (!overlayActionAt.current) overlayActionAt.current = newOverlayLifecycle();
      const actionLife = overlayActionAt.current;
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
          if (path === '/clear') { retireLocalOverlay(response?.overlay_stop_confirmed === true); setOverlayMsg(response?.overlay_cleanup_error ? 'Overlay shutdown is not confirmed. Retry hide overlay and verify the window is gone before sharing.' : ''); }
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
      const [fallbackTranscriptionProvider, setTranscriptionProvider] = useState(saved.transcriptionProvider === 'meta' ? 'meta' : 'local');
      const [fallbackAsrMode, setAsrMode] = useState(['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK'].includes(saved.asrMode) ? saved.asrMode : 'ENDPOINTING');
      const transcriptionProvider = settingsAvailable ? prefs.transcriptionProvider : fallbackTranscriptionProvider;
      const asrMode = settingsAvailable ? prefs.asrMode : fallbackAsrMode;
      const [setupOpen, setSetupOpen] = useState(false);
      const [answerUncertain, setAnswerUncertain] = useState(false);
      const [progressPaused, setProgressPaused] = useState(false);
      useEffect(() => {
        // Patch the current shared snapshot, never a captured answer setting:
        // a late pane effect must not roll back a newer Settings edit.
        if (!settingsAvailable && usable()) savePreferences({ backend, transcriptionModel, audioSource, transcriptionProvider, transcriptionChoiceVersion: 1, asrMode });
      }, [backend, transcriptionModel, audioSource, transcriptionProvider, asrMode]);
      // Capability discovery never opts a meeting into cloud audio. Only an
      // explicit selection or persisted provider choice may select Meta.
      function resetMeetingInputs() {
        setQuestion(''); setWeb(''); setLastAsked(null); setLiveEntry(null);
        setTrackedRequest(null); setError(''); setAssistFailed(false);
        setAnswerUncertain(false); setProgressPaused(false);
        if (safety.answer && safety.answerKnown) { safety.answerToken += 1; signalSafety(); }
      }
      const cloudUnavailable = backend === 'macos' && transcriptionProvider === 'meta' && !data.capabilities?.meta_asr;
      const windowsUnavailable = backend === 'native' && (settingsAvailable ? data.capabilities?.windows_transcription !== true : data.capabilities?.windows_transcription === false);
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
      function retireLocalOverlay(confirmed = false) {
        const life = overlayActionAt.current;
        const wasUnsafe = safety.overlayVisible || safety.overlayUnknown || safety.overlayAction || !!life.owner;
        life.overlayGeneration += 1;
        life.owner = null; life.syncedKey = null; life.credentialShutdownUnconfirmed = false;
        safety.overlayVisible = false; safety.overlayUnknown = wasUnsafe && !confirmed;
        signalSafety();
        scopeSeq.current += 1;
        setOverlayOn(false); setScopeStale(false); setOverlayMsg('');
      }
      async function toggleOverlay() {
        const life = overlayActionAt.current;
        if (!life.mounted || safety.overlayAction) return;
        if (!overlayOn && !overlayNeedsHide && JSON.stringify(answerScope(preferences.get())) !== overlayScopeKey) return;
        safety.overlayAction = true;
        safety.overlayToken += 1;
        signalSafety();
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
            retireLocalOverlay(hidden?.overlay_stop_confirmed === true);
          } else {
            // Fence even a lost/in-flight Show response: it may create a new
            // owner before a delayed credential response reaches this pane.
            life.overlayGeneration += 1;
            const shown = await api('/overlay/show', { pane, action, scope: { ...scope, seq: scopeSeq.current } });
            if (!life.mounted || life.action !== action) return;
            if (shown?.accepted === false || !shown?.owner) throw new Error('This pane no longer owns the overlay. Reopen the pane to take ownership.');
            life.owner = shown.owner; life.syncedKey = scopeKey;
            safety.overlayVisible = true; safety.overlayUnknown = false;
            setOverlayOn(true); setScopeStale(false);
            setOverlayMsg('Read-only overlay shown. Ask questions in the Meeting Copilot pane.');
          }
          await qc.invalidateQueries({ queryKey: [ID, 'status'] });
        } catch (e) { safety.overlayUnknown = true; if (life.mounted) {
          setOverlayMsg('Overlay action not confirmed. Retry Show / Hide overlay; if it still fails, close and reopen this pane. Check the macOS helper and backend connection. A window may still be visible: verify before screen-sharing.');
          if (!overlayOn) setOverlayOn(false);
        } }
        finally { safety.overlayAction = false; signalSafety(); if (life.mounted) setPending(false); }
      }
      async function startCapture(newMeeting = false) {
        const canStartCurrent = token => {
          if (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife) return false;
          if (token === undefined ? (safety.captureAction || safety.captureUnknown)
              : (!safety.captureAction || safety.captureToken !== token)) return false;
          const current = settingsAvailable ? preferences.get() : captureInput.current.source;
          const latest = qc.getQueryState?.([ID, 'status']) || status;
          const caps = latest?.data?.capabilities || {};
          return !!(captureStatusIdle(latest) && !latest?.data?.overlay_cleanup_error && !actionLife.credentialShutdownUnconfirmed &&
            consentPrefs.current && captureKey(current) === captureKey(renderedCapture) && captureInput.current.recording === recording &&
            (!settingsAvailable || (captureRevision === renderedCaptureRevision &&
              consentPrefs.current.revision === captureRevision && consentPrefs.current.key === captureKey(current))) &&
            (backend !== 'meetily' || recording) && (backend !== 'macos' || (caps.macos_transcription && (transcriptionProvider !== 'meta' || caps.meta_asr))) &&
            (backend !== 'native' || (settingsAvailable ? caps.windows_transcription === true : caps.windows_transcription !== false)));
        };
        if (!canStartCurrent()) {
          if (usable()) setError('Capture settings changed or status is uncertain. Review the source and confirm permission again before starting.');
          return false;
        }
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
        if (!canStartCurrent()) return false;
        const life = overlayActionAt.current;
        const permission = consentPrefs.current, inputRevision = captureInput.current.revision, action = life.action;
        safety.captureAction = true; safety.captureUnknown = true; safety.captureReleased = false; const token = ++safety.captureToken; signalSafety();
        let posted = false;
        setPending(true);
        try {
          const pane = await claimOverlayPane(life);
          if (!canStartCurrent(token) || consentPrefs.current !== permission || captureInput.current.revision !== inputRevision || life.action !== action) {
            if (life.mounted) setError('Capture settings, permission, or status changed. Review and start again.');
            return false;
          }
          posted = true;
          const ok = await act('/start', { pane, action: ++life.action, consent: true, backend, recording_id: recording || null,
            transcription_model: transcriptionModel, audio_source: audioSource, new_meeting: newMeeting,
            transcription_provider: backend === 'native' ? 'local' : transcriptionProvider,
            asr_mode: backend === 'native' ? 'ENDPOINTING' : asrMode });
          if (life.mounted && ok && (newMeeting || sourceChanged)) { resetMeetingInputs(); retireLocalOverlay(); }
          return ok;
        } catch (e) {
          if (life.mounted) setError(lifecycleErrors.has(e) ? e.message : 'Start was not confirmed. Check the backend connection; close and reopen this pane to retry authorization.');
          return false;
        } finally {
          if (!posted && safety.captureToken === token) safety.captureUnknown = false;
          safety.captureAction = false; signalSafety(); if (life.mounted) setPending(false);
        }
      }
      const answerSubmitting = useRef(false);
      const answerBlocked = !!(busy || answerUncertain || trackedRequest || Object.keys(data.requests || {}).length);
      const answerSafetyToken = safety.answerToken;
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
          safety.answer = false;
          signalSafety();
          setError({ invalid_request: 'Answer was not accepted. Enter a question or start listening; check the selected scope.',
            busy: 'Answer was not accepted because another answer is active. Wait for it to finish or cancel it, then ask again.',
            capacity: 'Answer was not accepted because workers are still busy. Wait for them to finish, then ask again.' }[res.reason]);
          setAssistFailed(true);
          return;
        }
        if (!res?.request_id) throw new Error('Unconfirmed answer submission');
        safety.answerKnown = true; safety.answerToken += 1; signalSafety();
        setAnswerUncertain(false); setProgressPaused(false);
        setLiveEntry(null); setTrackedRequest(res.request_id);
      };
      async function assistNow(extra = {}, questionOverride, scopeOverride) {
        if (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife || answerBlocked || answerSubmitting.current || safety.answer) return;
        // Read the shared atom at the action, not the pane render that created
        // this callback: Settings and pane are independent React roots.
        const currentPrefs = preferences.get();
        const latestState = typeof qc.getQueryState === 'function' ? qc.getQueryState([ID, 'status']) : status;
        const currentData = latestState?.data || data;
        const catalog = typeof qc.getQueryData === 'function' ? qc.getQueryData([ID, 'models']) : null;
        const currentProviders = catalog?.providers || providers;
        const isAll = currentPrefs.project === '__all_projects__';
        const invalid = (isAll && !currentData.capabilities?.all_projects) ||
          (currentPrefs.webFallback && !currentData.capabilities?.automatic_web_fallback) ||
          !['openai', 'openrouter'].includes(currentPrefs.answerProvider) || !currentPrefs.answerModel ||
          !currentProviders.some(p => p.id === currentPrefs.answerProvider) || !currentData.capabilities?.independent_models ||
          (currentPrefs.excludeHistory && (!currentData.capabilities?.history_opt_out || !currentPrefs.project || isAll || currentPrefs.webFallback || web.trim() || extra.web_query || (scopeOverride && scopeOverride.include_history !== false)));
        if (invalid || latestState?.error || currentData.busy || Object.keys(currentData.requests || {}).length) {
          setError('Answer was not sent. Check the current answer settings and wait for any active answer to finish.');
          return;
        }
        const asked = questionOverride !== undefined ? questionOverride : question;
        const scopeUsed = scopeOverride || answerScope(currentPrefs);
        const body = boundedAnswer({ question: asked, ...scopeUsed, ...extra, response_contract: 'answer-admission-v1' });
        if (!body) return;
        answerSubmitting.current = true; safety.answer = true; safety.answerKnown = false; safety.answerToken += 1; signalSafety();
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
        if (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife || !req || answerBlocked || answerSubmitting.current || safety.answer) return;
        if (preferences.get().excludeHistory && req.include_history !== false) {
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
        answerSubmitting.current = true; safety.answer = true; safety.answerKnown = false; safety.answerToken += 1; signalSafety();
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
              safety.answer = false;
              signalSafety();
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
              safety.answer = false;
              signalSafety();
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
      const renderedCapture = { backend, audioSource, transcriptionModel, transcriptionProvider, asrMode };
      const renderedCaptureRevision = captureRevision;
      const consentPrefs = useRef(consent ? { key: captureKey(renderedCapture), revision: renderedCaptureRevision } : null);
      // Event-time authority, independent of React's next render. A source or
      // recording change also invalidates a Start already awaiting its claim.
      const captureInput = useRef({ source: renderedCapture, recording, revision: 0 });
      const changeCaptureInput = (field, value, setter) => {
        if (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife) return;
        if (field === 'recording') captureInput.current.recording = value;
        else captureInput.current.source = { ...captureInput.current.source, [field]: value };
        captureInput.current.revision += 1;
        setter(value);
      };
      const [, setConsentRevision] = useState(0);
      const consentValid = consent && (!settingsAvailable || (consentPrefs.current?.key === captureKey(renderedCapture) && consentPrefs.current?.revision === renderedCaptureRevision));
      paneEntry.retire = message => { paneEntry.retired = message; setRetirement(message); };
      if (paneEntry.retired) return h('section', { role: 'alert', style: card },
        h('strong', null, 'Meeting Copilot pane retired — actions disabled'),
        h('p', null, paneEntry.retired),
        h('p', null, 'Close this pane and reopen Meeting Copilot. Check capture status and Retry stop if needed before giving fresh consent. If audio remains active, quit the capture process or revoke audio permissions and verify the OS indicator.'));
      const transcriptContext = snapshot => h('details', { style: { marginTop: 12 } },
        h('summary', { style: { cursor: 'pointer', ...muted } }, 'Transcript context sent with this answer'),
        h('p', { style: muted }, 'Frozen recent context, not the current transcript or a complete meeting. References identify excerpts, not verified claims. Backend times are uninterpreted; no exact utterance time or playback offset is verified. Missing owners, dates and timestamps must not be inferred.'),
        h('p', { style: muted }, `${snapshot.omitted_segments || 0} segment(s) omitted by the context limit.`),
        ...(Array.isArray(snapshot.segments) ? snapshot.segments : []).map((s, i) => h('div', { key: i, style: { ...muted, whiteSpace: 'pre-wrap', paddingTop: 8 } },
          h('strong', null, `[${s.ref}] ${s.speaker}: `), s.text,
          s.shortened && h('span', null, ' (excerpt shortened)'),
          h('div', null, s.timestamp == null ? 'Time unavailable' : `${s.timestamp_kind}: ${s.timestamp} (backend metadata, not verified utterance time)`))),
        h(Button, { variant: 'secondary', disabled: typeof snapshot.provider_json !== 'string' || snapshot.provider_json.length > 16000, onClick: async () => {
          if (!usable() || typeof snapshot.provider_json !== 'string' || snapshot.provider_json.length > 16000) return;
          if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Copy the exact transcript context sent for this answer? It can contain confidential meeting text and metadata. Clipboard history or sync may retain it. Review before sharing.')) return;
          try { await navigator.clipboard.writeText(snapshot.provider_json); notify('info', 'Sent transcript context copied. Review before sharing.'); }
          catch (_) { setError('Clipboard unavailable. Select and copy the sent context manually.'); }
        } }, 'Copy sent transcript context'));
      const evidenceItem = (s, i) => {
        const excerpt = evidenceExcerpt(s.text);
        return h('div', { key: s.id || i, style: { ...muted, marginTop: 8, padding: '8px 10px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, overflowWrap: 'anywhere' } },
          h('strong', null, `[${i + 1}] ${s.title || s.source || 'Source'}`),
          /^https?:\/\//.test(s.source || '') ? h('a', { href: s.source, target: '_blank', rel: 'noopener noreferrer', style: { display: 'block', color: 'var(--ui-accent)' } }, s.source) : h('div', null, s.source),
          s.updated_at && h('div', null, `Updated: ${typeof s.updated_at === 'number' ? new Date(s.updated_at * 1000).toLocaleString() : s.updated_at}`),
          h('div', { style: { whiteSpace: 'pre-wrap' } }, excerpt.text || '(No excerpt available)'),
          excerpt.truncated && h('div', null, 'Excerpt limited to 2,050 characters.'));
      };
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
      const cloudAudioSelected = backend === 'macos' && transcriptionProvider === 'meta';
      const cloudAudioActive = data.listening && data.last_session?.backend === 'macos' && data.last_session?.transcription_provider === 'meta';
      const cloudAudioDisclosed = cloudAudioSelected || cloudAudioActive;
      const sourceSummary = backend === 'meetily' ? 'Meetily — choose an active recording below'
        : `${backend === 'macos' ? 'macOS' : 'Windows'} · ${{ microphone: 'Microphone', system: 'System audio', both: 'Microphone + system audio' }[audioSource] || audioSource} · ${cloudAudioSelected ? `Meta (${asrMode})` : `Local transcription (${transcriptionModel})`}`;
      const captureBlocked = overlayNeedsHide || cloudUnavailable || windowsUnavailable || !consentValid || (backend === 'meetily' && !recording)
        || (settingsAvailable && !mayChangeCapture(qc, status, () => usable() && actionLife.mounted && overlayActionAt.current === actionLife))
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
        settingsAvailable && h('div', { style: { ...muted, display: 'flex', gap: 8, alignItems: 'center' } },
          'Capture and answer defaults: Settings → Plugins → Meeting Copilot.',
          typeof host.navigate === 'function' && h('button', { type: 'button', onClick: () => { if (usable()) host.navigate(`/settings?tab=plugins&plugin=${ID}`); }, style: { color: 'var(--ui-accent)', background: 'transparent', border: 0, cursor: 'pointer' } }, 'Open settings')),
        // Capture session first: the permission check and the primary
        // start/pause actions live at the top, before any setup detail.
        h('section', { style: { ...card, display: 'grid', gap: 10 }, 'aria-label': 'Capture session' },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
            h('strong', null, 'Capture session'),
            h('span', { style: muted, role: 'status' }, settingsAvailable && status.error ? 'Capture status unavailable — check the backend connection.' : settingsAvailable && status.isPending ? 'Checking capture status…' : data.stopping ? 'Stop is not confirmed — audio may still be active.' : data.listening
              ? `Listening via ${data.backend || backend}${lossText(data.capture)}${data.capture?.warning ? ' · transcript may be incomplete' : ''}`
              : ((data.segments || []).length || result || (data.recent || []).length) ? 'Paused — resume this meeting or start a fresh one.' : 'Not capturing yet.')),
          settingsAvailable && h('p', { style: { ...muted, margin: 0 } }, `Capture source: ${sourceSummary} (next capture).`),
          h('label', { style: { display: 'flex', gap: 8, alignItems: 'flex-start', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '10px 12px', background: 'var(--ui-bg-card)' } },
            h('input', { type: 'checkbox', checked: consentValid, disabled: data.listening, onChange: e => {
              if (e.target.checked && (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife ||
                  captureInput.current.recording !== recording || (!settingsAvailable && captureKey(captureInput.current.source) !== captureKey(renderedCapture)) ||
                  (settingsAvailable && (captureRevision !== renderedCaptureRevision || captureKey(preferences.get()) !== captureKey(renderedCapture))))) return;
              consentPrefs.current = e.target.checked ? { key: captureKey(renderedCapture), revision: renderedCaptureRevision } : null;
              setConsent(e.target.checked);
              setConsentRevision(value => value + 1);
            }, 'aria-describedby': cloudAudioDisclosed ? `${paneEntry.a11yId}-cloud-audio` : undefined }),
            h('span', null, h('strong', null, 'Permission check. '), 'I have permission to capture/read this meeting and use its context with my configured model.')),
          cloudAudioDisclosed && h('p', { id: `${paneEntry.a11yId}-cloud-audio`, 'data-cloud-capture-disclosure': true, role: 'status', style: { ...muted, margin: 0 } }, `Cloud transcription ${cloudAudioActive ? 'active' : 'selected'}: meeting audio is sent to Meta while listening. ${data.listening ? 'Pause to change' : 'Change'} the provider in ${settingsAvailable ? 'Meeting Copilot settings' : 'Transcript source below'}.`),
          data.listening && data.backend === 'macos' && !data.last_session?.transcription_provider && h('p', { role: 'status', style: muted }, 'The active transcription provider is not confirmed. Check capture status and pause if unsure.'),
          cloudUnavailable && h('p', { role: 'status', style: muted }, `Meta cloud transcription is unavailable. Choose Local in ${settingsAvailable ? 'Meeting Copilot settings' : 'Transcript source'}, or configure a Meta key and recheck backend readiness before Start.`),
          windowsUnavailable && h('p', { role: 'status', style: muted }, data.capabilities?.windows_detail || 'Windows capture needs the optional audio dependencies. See the Windows setup guide.'),
          !data.listening && !data.stopping && (pending || !consentValid || (backend === 'meetily' && !recording) || (backend === 'macos' && !data.capabilities?.macos_transcription)) && h('p', { style: { ...muted, margin: 0 }, role: 'status' },
            pending ? 'Starting…' : !consentValid ? (consent && settingsAvailable ? 'Capture settings changed. Confirm permission again before starting.' : 'Confirm permission with the check above to enable Start.') : (backend === 'meetily' && !recording) ? 'Select an active Meetily recording below to enable Attach.' : `Independent macOS transcription is unavailable here — see ${settingsAvailable ? 'Meeting Copilot settings' : 'the source card below'}.`),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
            data.stopping
              ? h(Button, { variant: 'secondary', disabled: pending, onClick: () => act('/pause', {}) }, 'Retry stop')
              : data.listening
              ? h(Button, { variant: 'secondary', onClick: () => act('/pause', {}) }, data.backend === 'meetily' ? 'Detach' : 'Pause listening')
              : h(Button, { disabled: pending || captureBlocked, onClick: () => startCapture() }, ((data.segments || []).length || result || (data.recent || []).length) ? (backend === 'meetily' ? 'Resume' : 'Resume listening') : (backend === 'meetily' ? 'Attach to Meetily' : 'Start listening')),
            !data.listening && !data.stopping && ((data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending || captureBlocked, title: 'Starts a fresh meeting; the earlier transcript, answers, and retry data are cleared', onClick: () => startCapture(true) }, 'New meeting'),
            (data.overlay_cleanup_error || (data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending, title: 'Clears past Q&A, transcript segments, and cached snippets for this meeting', onClick: async () => { if (typeof window !== 'undefined' && window.confirm && !window.confirm('Clear this meeting? Past questions, answers, and transcript segments are removed from the pane.')) return; if (await act('/clear')) { setQuestion(''); setLastAsked(null); setLiveEntry(null); setTrackedRequest(null); setError(''); setAssistFailed(false); } } }, 'Clear meeting'),
            h(Button, { variant: 'secondary', disabled: pending || (!data.capabilities?.overlay && !overlayNeedsHide), title: 'Show or hide the read-only meeting overlay (macOS)', onClick: toggleOverlay }, overlayNeedsHide ? 'Retry hide overlay' : overlayOn ? 'Hide overlay' : 'Show overlay')),
          h('p', { style: { ...muted, margin: 0 } }, 'Screen-share privacy: the overlay may be visible in screenshots, recordings or screen sharing. Capture exclusion is best-effort, not a guarantee. Hide the overlay when it must not appear.'),
          h('p', { 'data-overlay-controls-help': true, style: { ...muted, margin: 0 } }, 'In the overlay, press ⌃⌥⌘O (Control + Option + Command + O) to drag the header, use position presets, or toggle Show in recordings. It returns to click-through automatically.'),
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

        !settingsAvailable && h(data.listening ? 'details' : 'section', data.listening ? null : { style: card },
          ...(data.listening ? [h('summary', { style: { cursor: 'pointer', ...muted } }, `Transcript source: ${data.backend || backend} — listening (pause to change settings)`)] : []),
          h('span', { id: 'mc-source-label', style: { fontWeight: 700 } }, 'Transcript source'),
          h('div', { style: { display: 'grid', gap: 6, paddingTop: 8, paddingBottom: 10 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-backend-label' }, 'Capture source'),
            h('select', { value: backend, disabled: data.listening, onChange: e => changeCaptureInput('backend', e.target.value, setBackend), style: selectStyle, 'aria-labelledby': 'mc-backend-label' },
              h('option', { value: 'meetily' }, 'Meetily — use your existing recording'),
              h('option', { value: 'macos', disabled: !data.capabilities?.macos_transcription, title: data.capabilities?.macos_detail || '' }, 'macOS — without Meetily'),
              h('option', { value: 'native', disabled: data.capabilities?.windows_transcription === false, title: data.capabilities?.windows_detail || 'Windows only · experimental' }, 'Windows — experimental')))),
          backend === 'macos' && !data.capabilities?.macos_transcription && h('p', { role: 'status', style: muted }, data.capabilities?.macos_detail || 'macOS transcription is unavailable on this machine.'),
          (backend === 'macos' || backend === 'native') && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Audio source'), h('select', { style: selectStyle, value: audioSource, disabled: data.listening, onChange: e => changeCaptureInput('audioSource', e.target.value, setAudioSource), 'aria-label': 'Audio source' },
              h('option', { value: 'microphone' }, 'Microphone'), h('option', { value: 'system' }, 'System audio'), h('option', { value: 'both' }, 'Microphone + system audio'))),
            backend === 'macos' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Transcription provider'), h('select', { style: selectStyle, value: transcriptionProvider, disabled: data.listening, onChange: e => changeCaptureInput('transcriptionProvider', e.target.value, setTranscriptionProvider), 'aria-label': 'Transcription provider' },
              h('option', { value: 'local' }, 'Local — faster-whisper on this Mac'),
              h('option', { value: 'meta', disabled: !data.capabilities?.meta_asr, title: data.capabilities?.meta_asr ? '' : 'Set the Meta API key in Hermes Settings → Plugins → meeting-copilot-live, then reopen this pane.' }, 'Meta Muse Voice — cloud transcription'))),
            (backend === 'native' || transcriptionProvider === 'local') && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Local transcription model'), h('select', { style: selectStyle, value: transcriptionModel, disabled: data.listening, onChange: e => changeCaptureInput('transcriptionModel', e.target.value, setTranscriptionModel), 'aria-label': 'Local transcription model' },
              h('option', { value: 'tiny.en' }, 'Whisper tiny.en — fastest'), h('option', { value: 'base.en' }, 'Whisper base.en — balanced'), h('option', { value: 'small.en' }, 'Whisper small.en — higher accuracy'))),
            backend === 'native' && h('span', { style: muted }, 'Local transcription uses the default microphone and all audio on the default output, according to Audio source. First Start may download Whisper weights. ', data.capabilities?.windows_detail || 'Experimental; hardware is unverified.'),
            backend === 'macos' && transcriptionProvider === 'local' && h('span', { style: muted }, 'Runs on this Mac without Meetily. First use may download the model. Microphone or screen and system audio permission is required.'),
            backend === 'macos' && transcriptionProvider === 'meta' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Meta ASR mode'), h('select', { style: selectStyle, value: asrMode, disabled: data.listening, onChange: e => changeCaptureInput('asrMode', e.target.value, setAsrMode), 'aria-label': 'Meta ASR mode' },
              h('option', { value: 'ENDPOINTING' }, 'Endpointing — one segment per utterance'),
              h('option', { value: 'DIARIZATION' }, 'Diarization — speaker labels per source'),
              h('option', { value: 'PUSH_TO_TALK' }, 'Push-to-talk — single-turn finals'))),
            backend === 'macos' && transcriptionProvider === 'meta' && h('span', { role: 'status', style: muted }, 'Cloud transcription: meeting audio is sent to Meta (muse-voice-transcribe) while listening. Only final turns are kept; the API key stays on the gateway and is never shown here.'))),
        backend === 'meetily' && h('section', { style: card }, h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('span', { style: muted }, 'Start recording in Meetily, then select it here. Only the selected transcript is read.'),
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-rec-label' }, 'Active Meetily recording'),
              h('select', { value: recording, disabled: data.listening || recordings.isPending, onChange: e => changeCaptureInput('recording', e.target.value, setRecording), style: selectStyle, 'aria-labelledby': 'mc-rec-label' },
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

        ...(unsupported ? [h('p', { role: 'alert', style: muted }, `Suggest is unavailable: ${unsupportedReasons.join(' ')} See ${settingsAvailable ? 'Settings → Plugins → Meeting Copilot' : 'Answer setup below'}, then restart the gateway when other sessions are idle.`)] : []),
        excludeHistory && !unsupported && h('p', { role: 'status', style: muted, 'data-history-excluded': true }, 'Past sessions and web search are excluded. Only the selected project is searched.'),
        h('p', { style: { ...muted, margin: 0 } }, 'Your question, part of the transcript and selected source snippets may be sent to the answer model. If enabled in Settings, automatic fallback may also send a public query.'),
        webFallback && h('p', { 'data-web-fallback-disclosure': true, role: 'status', style: { ...muted, margin: 0 } }, 'Automatic web fallback is on: if local evidence is insufficient, a public query may be drafted. Filtering is not a privacy guarantee; turn it off for sensitive calls.'),
        h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
          h(Button, { disabled: answerBlocked || unsupported, onClick: () => assistNow() }, busy ? 'Checking your sources…' : 'Suggest answer'),
          (answerUncertain || progressPaused || (assistFailed && trackedRequest)) && h(Button, { variant: 'secondary', disabled: pending, onClick: async () => {
            if (!usable() || answerSubmitting.current || safety.answerToken !== answerSafetyToken) return;
            if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Reset only local answer tracking? The earlier request may still be running or complete. This does not cancel it or send anything. Review available answers first. Asking again afterwards may resend the same meeting context and create a duplicate.')) return;
            setTrackedRequest(null); setLiveEntry(null); setAnswerUncertain(false);
            setProgressPaused(false); setAssistFailed(false); setError('');
            if (safety.answer) {
              // An explicit reset permits a new authoritative read, never an
              // automatic resend or a release based on a pre-admission cache.
              safety.answerKnown = true; safety.answerToken += 1; signalSafety();
              try { await checkedStatus(api); } catch (_) {}
            }
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
            if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Copy this answer and its source excerpts (up to 2,050 characters per source)? The clipboard may include confidential project text or links. Review before sharing. No new model request is sent.')) return;
            try { await navigator.clipboard.writeText(evidenceReport(result)); notify('info', 'Answer and evidence copied. Review before sharing.'); }
            catch (_) { setError('Clipboard unavailable. Select and copy the answer and evidence manually.'); }
          } }, 'Copy answer + evidence'),
          h('div', { style: { display: 'flex', justifyContent: 'space-between' } }, h('strong', null, 'Suggested answer'), h('span', { style: muted }, `${Number.isFinite(result.elapsed_seconds) ? `${result.elapsed_seconds.toFixed(1)}s` : ''}${result.model_route ? ` · ${result.model_route}` : ''}`)),
          h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 10 } }, result.answer),
          result.transcript_snapshot?.schema === 'transcript-snapshot-v1' && transcriptContext(result.transcript_snapshot),

          result.web_search?.used && h('p', { style: muted }, `${result.web_search.mode === 'automatic' ? 'Automatic web fallback' : 'Explicit web search'}: ${result.web_search.query}`),
          ...(result.warnings || []).map((w, i) => h('p', { key: `warning-${i}`, style: muted }, w)),
          result.answer && /partial|interrupt/i.test((result.warnings || []).join(' ')) && (result.request || lastAsked) && h('div', { style: { paddingTop: 8 } }, h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(result.request || { question: lastAsked.question, ...(lastAsked.scope || {}) }) }, 'Retry for the complete response')),
          h('details', { style: { marginTop: 12 } }, h('summary', { style: { cursor: 'pointer', ...muted } }, `Evidence · ${(result.sources || []).length} sources`),
            h('p', { style: muted }, 'Excerpts are limited to 2,050 characters per source.'),
            ...(result.sources || []).map(evidenceItem))),
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
          h('p', { style: muted }, 'Kept for this session only — a restart, new meeting, or new answer session clears older entries. Retained excerpts and sent context may contain confidential information.'),
          ...[...data.recent].reverse().map((entry, i) => h('div', { key: entry.request_id || i, style: { ...card, marginTop: 8 } },
            h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8 } },
              h('strong', null, (entry.question || '').slice(0, 200) || 'Answer'),
              h('span', { style: muted }, `${entry.status || ''}${entry.model_route ? ` · ${entry.model_route}` : ''}${entry.answer_shortened ? ' · shortened' : ''}`)),
            h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 8 } }, entry.answer || '(no answer text)'),
            entry.answer_shortened && h('p', { style: muted }, 'History keeps a shortened copy; the full answer appeared here when first delivered.'),
            ...((entry.warnings || []).map((w, j) => h('p', { key: `rw-${j}`, style: muted }, w))),
            entry.status === 'done' && entry.transcript_snapshot?.schema === 'transcript-snapshot-v1' && transcriptContext(entry.transcript_snapshot),
            typeof entry.answer === 'string' && /\[T\d+\]/.test(entry.answer)
              && (entry.status !== 'done' || entry.transcript_snapshot?.schema !== 'transcript-snapshot-v1')
              && h('p', { style: muted }, 'Transcript context for this answer is unavailable or could not be confirmed.'),
            (entry.sources || []).length > 0 && h('details', { style: { marginTop: 8 } }, h('summary', { style: { cursor: 'pointer', ...muted } }, `Evidence · ${entry.sources.length} sources${entry.sources.length > 9 ? ' (showing first 9)' : ''}`),
              h('p', { style: muted }, 'Excerpts are limited to 2,050 characters per source.'),
              ...(entry.sources || []).slice(0, 9).map(evidenceItem)),
            h('div', { style: { paddingTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' } },
              entry.answer && h(Button, { variant: 'secondary', onClick: async () => { if (!usable()) return; try { await navigator.clipboard.writeText(entry.answer); } catch (e) { setError('Clipboard unavailable. Select and copy the answer manually.'); } } }, 'Copy this answer'),
              entry.request && h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(entry.request) }, /partial|interrupt|unavailable|failed|error/i.test((entry.warnings || []).join(' ') + ' ' + (entry.status || '')) ? 'Retry for the complete response' : 'Ask again'))))),
        h('details', { open: setupOpen, onToggle: e => setSetupOpen(e.currentTarget.open), style: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 12, padding: '2px 14px 14px', background: 'var(--ui-bg-card)' } },
        h('summary', { style: { cursor: 'pointer', fontWeight: 700, color: 'var(--ui-text-primary)', padding: '12px 0', fontSize: 14 } }, `${setupOpen ? '\u25be' : '\u25b8'} ${settingsAvailable ? 'More actions' : 'Answer setup'}`),
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 14, paddingTop: 4 } },

        !settingsAvailable && h(AnswerPreferences, { prefs, status, projects, models: modelCatalog, qc, live: () => usable() && overlayActionAt.current.mounted }),
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
              h(Input, { value: web, maxLength: 300, onChange: e => { if (usable() && actionLife.mounted && overlayActionAt.current === actionLife) setWeb(e.target.value.slice(0, 300)); }, placeholder: 'Public query — no client names or meeting details', 'aria-label': 'Specific public web query', 'aria-describedby': `${paneEntry.a11yId}-web-query` })),
            h('span', { id: `${paneEntry.a11yId}-web-query`, 'data-web-query-disclosure': true, style: muted }, 'This query is sent to the web and overrides automatic fallback for this request. Do not include private details. This action stays in the pane so it uses this meeting and only runs when you press Check sources + web.'),
            h(Button, { variant: 'secondary', disabled: busy || unsupported || !web.trim(), onClick: () => assistNow({ web_query: web.trim() }) }, 'Check sources + web'))),
        )),
        h('p', { style: { ...muted, marginTop: 'auto' } }, 'Read-only assistance. Nothing is spoken, posted, or changed for you. Check suggestions before using them.'));
    }
    const paneId = `${ID}:pane`;
    const visibility = typeof host.paneVisibility === 'function' ? host.paneVisibility(paneId) : null;
    const registerPane = () => {
      const entry = { active: true, life: null, a11yId: `${ID}-pane-${registration}-${++paneIds}` };
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
    const dispose = () => {
      if (!active) return;
      active = false;
      for (const life of lifecycles) {
        life.mounted = false;
        releaseOverlayPane(life);
      }
    };
    // Hermes tracks explicit disposers; its runtime loader ignores register's
    // return value. Keep the return as a fallback for older/test hosts.
    if (typeof ctx.onDispose === 'function') ctx.onDispose(dispose);
    return dispose;
  },
};
