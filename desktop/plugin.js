import { createElement as h, useState, useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react';
import { host, atom, useValue, useQuery, useQueryClient, Button, Input, Textarea, Badge, PALETTE_AREA } from '@hermes/plugin-sdk';
import * as sdk from '@hermes/plugin-sdk';

const ID = 'meeting-copilot-live';
const speakerPalette = ['var(--ui-accent)', 'var(--ui-green, var(--ui-accent))', 'var(--ui-orange, var(--ui-accent))', 'var(--ui-red, var(--ui-accent))', 'var(--ui-cyan, var(--ui-accent))', 'var(--ui-purple, var(--ui-accent))'];
const muted = { color: 'var(--ui-text-secondary)', fontSize: 12, lineHeight: 1.5 };
const card = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 14, minWidth: 0, boxSizing: 'border-box', background: 'var(--ui-bg-card)' };
const selectStyle = { padding: 8, borderRadius: 6, width: '100%', maxWidth: '100%', minWidth: 0, boxSizing: 'border-box', color: 'var(--ui-text-primary)', background: 'var(--ui-bg-card)', border: '1px solid var(--ui-stroke-secondary)' };

// Content-free cumulative diagnostics. Unknown/missing fields never mean silence.
function audioDiagnosticText(value) {
  if (!value || typeof value !== 'object') return '';
  return ['microphone', 'system'].flatMap(source => {
    const row = value[source];
    if (!row || !Number.isSafeInteger(row.received_samples) || row.received_samples < 0 || row.received_samples > 1e12) return [];
    const label = source === 'microphone' ? 'Microphone' : 'System audio';
    let text = row.received_samples === 0 ? 'no samples received' : 'input received';
    if (row.received_samples > 0 && row.peak === 0 && row.rms === 0) text = 'zero-level input received';
    const count = key => Number.isSafeInteger(row[key]) && row[key] >= 0 && row[key] <= 1e12;
    if (count('completed') && count('empty') && row.completed > 0 && row.empty <= row.completed) {
      text += `; ${row.completed} chunk(s) transcribed, ${row.empty} with no words`;
    } else if (count('submitted') && row.submitted > 0) text += '; transcription pending';
    return [`${label}: ${text}`];
  }).join(' · ');
}

// Content-free, source-specific attention cue. Empty recognition does not
// establish silence or a broken model, and absent levels remain unknown.
function audioAttentionText(value) {
  if (!value || typeof value !== 'object') return '';
  return ['microphone', 'system'].flatMap(source => {
    const row = value[source];
    if (!row || !Number.isSafeInteger(row.received_samples) || row.received_samples <= 0 || row.received_samples > 1e12
      || !Number.isSafeInteger(row.completed) || row.completed <= 0 || row.completed > 1e12 || row.empty !== row.completed
      || ![row.peak, row.rms].some(level => Number.isFinite(level) && level > 0 && level <= 1)) return [];
    return [`${source === 'microphone' ? 'Microphone' : 'System audio'}: audio received, no words detected. Check the input, speech language and transcription model.`];
  }).join(' ');
}

// Scoped to the pane so host settings and other plugins keep their own design.
const workspaceStyles = `
.mc-workspace { container-type: inline-size; --mc-accent: var(--ui-accent); --mc-warn: var(--ui-orange, var(--ui-accent)); }
.mc-workspace p { margin: 0; }
.mc-rail { position: sticky; top: -12px; z-index: 3; display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin: -12px -12px 0; padding: 12px; background: var(--ui-bg-elevated, var(--background, Canvas)); border-bottom: 1px solid var(--ui-stroke-secondary); }
.mc-rail-actions, .mc-chips, .mc-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.mc-chip { display: inline-flex; align-items: center; gap: 5px; min-width: 0; max-width: 100%; padding: 4px 7px; border-radius: 6px; background: color-mix(in srgb, var(--ui-text-secondary) 7%, transparent); color: var(--ui-text-secondary); font-size: 11px; line-height: 1.4; overflow-wrap: anywhere; }
.mc-chip[data-tone="ready"] { color: var(--ui-green, var(--ui-accent)); }
.mc-chip[data-tone="warning"] { color: var(--mc-warn); }
.mc-notice { display: flex; align-items: flex-start; gap: 7px; padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--mc-accent) 7%, transparent); font-size: 12px; line-height: 1.5; }
.mc-notice svg { flex: 0 0 15px; margin-top: 2px; }
.mc-checks { border-top: 1px solid var(--ui-stroke-secondary); padding-top: 9px; display: grid; gap: 8px; }
.mc-checks-head { display: flex; align-items: center; justify-content: space-between; gap: 6px; flex-wrap: wrap; }
.mc-check-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 7px; }
.mc-check-list li { display: grid; grid-template-columns: 16px 1fr; gap: 6px; font-size: 12px; line-height: 1.5; }
.mc-check-list [data-check-status="blocked"], .mc-check-list [data-check-status="unknown"] { color: var(--mc-warn); }
.mc-check-list strong { display: block; color: var(--ui-text-primary); font-weight: 550; }
.mc-check-details { color: var(--ui-text-secondary); font-size: 11px; }
.mc-check-details[open] > summary { margin-bottom: 8px; }
.mc-secondary { border-top: 1px solid var(--ui-stroke-secondary); padding-top: 9px; }
.mc-answer-context { display: grid; gap: 6px; font-size: 11px; color: var(--ui-text-secondary); }
.mc-answer-actions > button:first-child { flex: 1; }
.mc-source-details { padding: 10px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; }
.mc-source-details > summary { font-size: 12px; font-weight: 550; }
.mc-transcript-row { display: grid; grid-template-columns: 3px 1fr; gap: 9px; padding: 6px 0; }
.mc-transcript-row > i { border-radius: 2px; }
.mc-transcript-row strong { display: block; font-size: 10px; font-weight: 600; margin-bottom: 3px; }
.mc-transcript-row p { margin: 0; font-size: 13px; line-height: 1.6; }
.mc-sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.mc-workspace :is(button, input, textarea, select, summary, a):focus-visible { outline: 2px solid var(--mc-accent); outline-offset: 3px; }
.mc-workspace button { white-space: normal; max-width: 100%; }
.mc-workspace :is(input, textarea, select) { max-width: 100%; }
.mc-workspace summary { min-height: 24px; }
.mc-workspace summary::marker { color: var(--ui-text-secondary); }
.mc-header { display: flex; align-items: center; gap: 12px; }
.mc-mark { display: grid; place-items: center; width: 30px; height: 30px; flex: 0 0 30px; border: 1px solid var(--ui-stroke-secondary); border-radius: 9px; background: color-mix(in srgb, var(--mc-accent) 12%, transparent); color: var(--mc-accent); }
.mc-eyebrow { color: var(--ui-text-secondary); font-size: 10px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.mc-title { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -.6px; line-height: 1.2; }
.mc-status { display: flex; align-items: center; gap: 7px; padding: 6px 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: 999px; font-size: 11px; }
.mc-status-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
.mc-status[data-state="listening"] { color: var(--mc-accent); background: color-mix(in srgb, var(--mc-accent) 9%, transparent); }
.mc-capture { border-radius: 12px !important; }
.mc-consent { font-size: 11px; line-height: 1.5; color: var(--ui-text-secondary); }
.mc-consent input { flex-shrink: 0; margin-top: 3px; accent-color: var(--mc-accent); }
.mc-composer { padding: 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 16px; background: var(--ui-bg-card); }
.mc-composer strong { font-size: 15px; letter-spacing: -.2px; }
.mc-composer textarea { min-height: 68px; line-height: 1.6; }
.mc-quick-actions { padding: 2px 0; }
.mc-quick-actions[open] > summary { margin-bottom: 12px; }
.mc-quick-actions button { flex: 1 1 100px; min-height: 40px; text-align: left; }
.mc-answer { border-radius: 16px !important; border-top: 3px solid var(--mc-accent) !important; background: color-mix(in srgb, var(--mc-accent) 5%, var(--ui-bg-card)) !important; }
.mc-answer-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
.mc-answer-heading > strong { font-size: 16px; letter-spacing: -.3px; }
.mc-answer-text { white-space: pre-wrap; font-size: 15px; line-height: 1.75; padding-top: 12px; }
.mc-evidence-card { border-radius: 12px !important; padding: 12px !important; }
.mc-evidence-card > strong { display: block; color: var(--ui-text-primary); margin-bottom: 5px; }
.mc-evidence { border-top: 1px solid var(--ui-stroke-secondary); padding-top: 12px; }
.mc-transcript, .mc-history { padding: 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 14px; background: var(--ui-bg-card); }
.mc-transcript > summary, .mc-history > summary { font-weight: 600; }
.mc-error { border-left: 3px solid var(--mc-accent) !important; }
.mc-empty { display: flex; align-items: center; gap: 9px; color: var(--ui-text-secondary); padding: 8px 12px; font-size: 12px; }
.mc-empty p { margin: 0; }
@container (max-width: 300px) { .mc-title { font-size: 15px; } .mc-header { gap: 8px; } .mc-quick-actions button { flex-basis: 100%; } .mc-answer-heading { display: grid; } }
@media (prefers-reduced-motion: reduce) { .mc-workspace *, .mc-workspace *::before, .mc-workspace *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; } }
`;
function WorkspaceMark() {
  return h('svg', { width: 24, height: 24, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round', 'aria-hidden': true },
    h('path', { d: 'M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4' }));
}

function PaneIcon({ name, size = 15 }) {
  const paths = {
    cloud: 'M7 18H6a4 4 0 1 1 1-7.9A5.5 5.5 0 0 1 18 9a4.5 4.5 0 0 1 0 9h-1M12 21V12m-3 3 3-3 3 3',
    check: 'm5 12 4 4L19 6',
    alert: 'M12 3 2 20h20L12 3Zm0 6v5m0 3v.1',
    source: 'M4 4h6l2 3h8v13H4V4Z',
    sparkle: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z',
  };
  return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true, focusable: false }, h('path', { d: paths[name] || paths.source }));
}

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
      return h('section', { style: card }, h('strong', null, 'Optional API key overrides'),
        h('p', { style: muted }, 'Follow Hermes default uses host-owned credentials when supported. For an explicit OpenAI or OpenRouter override, configure its key in Hermes Settings → Plugins → meeting-copilot-live (default profile; Agent settings on newer hosts). Keys stay in Hermes-managed secret settings. Never enter keys in chat or model fields. Reopen the pane after changing keys. Local transcription does not use an answer key.'));
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
    // ctx.rest always uses the host's *current* route; it cannot pin an old
    // source. Require both the canonical epoch and its reactive hook rather
    // than pretending a profile label can detect source swaps or away/back.
    const routeSupported = typeof sdk.gatewayActivationEpoch === 'function' && typeof sdk.useGatewayActivationEpoch === 'function'
      && typeof host.activeConnectionId === 'function' && typeof host.state?.profile?.get === 'function'
      && typeof host.state?.connectionId?.get === 'function';
    function currentBinding() {
      if (!routeSupported) return null;
      try {
        const epoch = sdk.gatewayActivationEpoch();
        const profile = host.state.profile.get();
        const connectionId = host.activeConnectionId();
        if (!Number.isSafeInteger(epoch) || typeof profile !== 'string' || !(connectionId === null || typeof connectionId === 'string')
            || sdk.gatewayActivationEpoch() !== epoch) return null;
        return { epoch, profile, connectionId };
      } catch (_) { return null; }
    }
    const sameBinding = (a, b) => !!a && !!b && a.epoch === b.epoch && a.profile === b.profile && a.connectionId === b.connectionId;
    const bindingCurrent = binding => sameBinding(binding, currentBinding());
    const bindingKey = (binding, kind) => binding ? [ID, kind, binding.connectionId, binding.profile, binding.epoch] : [ID, kind, 'inactive'];
    const cacheKey = kind => bindingKey(bindingCurrent(paneRegistration?.binding) ? paneRegistration.binding : null, kind);
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
        if (out.answerProvider !== undefined && !['hermes', 'openai', 'openrouter'].includes(out.answerProvider)) { out.answerProvider = ''; out.answerModel = ''; }
        for (const key of Object.keys(out)) if (key === 'project' ? secretPath(out[key]) : secretLike(out[key])) delete out[key];
        if (out.answerModel && !safeModel(out.answerModel)) delete out.answerModel;
        if (out.answerProvider === 'hermes') out.answerModel = 'default';
        else if (out.answerProvider !== undefined && !out.answerModel) out.answerModel = '';
        for (const [key, choices] of Object.entries({ backend: ['meetily', 'macos', 'native'], audioSource: ['microphone', 'system', 'both'],
          transcriptionModel: ['tiny.en', 'base.en', 'small.en'], transcriptionProvider: ['local', 'meta', 'google'], asrMode: ['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK'] })) {
          if (out[key] !== undefined && !choices.includes(out[key])) delete out[key];
        }
        if (['meta', 'google'].includes(out.transcriptionProvider) && parsed?.transcriptionChoiceVersion !== 1) out.transcriptionProvider = 'local';
        return out;
      } catch (_) { return {}; }
    }
    const loaded = loadPreferences();
    // An empty backend means no explicit choice. Resolve the running backend's
    // platform from its real readiness/details, not the renderer's OS (which
    // may be a remote client). Never save the inferred choice as a preference.
    function defaultBackend(caps) {
      const mac = caps?.macos_transcription === true || (typeof caps?.macos_detail === 'string' && caps.macos_detail.startsWith('On macOS:'));
      const windows = caps?.windows_transcription === true || (typeof caps?.windows_detail === 'string' && caps.windows_detail.startsWith('Windows capture needs optional audio dependencies in the gateway interpreter:'));
      return mac === windows ? '' : mac ? 'macos' : 'native';
    }
    const effectiveCapture = (prefs, caps) => ({ ...prefs, backend: prefs.backend || defaultBackend(caps), asrMode: prefs.transcriptionProvider === 'google' ? 'ENDPOINTING' : prefs.asrMode });
    const preferences = atom({ project: '', backend: '', webFallback: false, transcriptionModel: 'base.en', audioSource: 'both', answerProvider: 'hermes', answerModel: 'default', transcriptionProvider: 'local', asrMode: 'ENDPOINTING', ...loaded,
      excludeHistory: loaded.excludeHistory !== undefined ? loaded.excludeHistory : !loaded.webFallback });
    const storageFailed = atom(false);
    let answerRevision = 0;
    let captureRevision = 0; // memory-only consent fence; never persist consent
    function savePreferences(patch) {
      if (Object.entries(patch).some(([key, value]) => (key === 'project' ? secretPath(value) : secretLike(value)) || (key === 'answerModel' && !safeModel(value)))) {
        notify('warning', 'Do not enter API keys or secret-like values in preference fields. Use Hermes-managed Agent settings for keys.');
        return;
      }
      const previous = preferences.get();
      const next = { ...previous, ...patch };
      if (['backend', 'audioSource', 'transcriptionModel', 'transcriptionProvider', 'asrMode'].some(key => previous[key] !== next[key])) captureRevision++;
      if (['answerProvider', 'answerModel'].some(key => previous[key] !== next[key])) answerRevision++;
      preferences.set(next);
      const safe = {};
      for (const key of preferenceFields) if (next[key] !== undefined && !(key === 'backend' && !next[key])) safe[key] = next[key];
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
    const newSafety = () => ({ overlayAction: false, overlayVisible: false, overlayUnknown: false, overlayToken: 0, answer: false, answerKnown: false, answerToken: 0,
      permissionAction: false, captureAction: false, captureUnknown: false, captureToken: 0, captureReleasePending: false, captureReleased: false });
    let safety = newSafety(), safetyBinding = null;
    const safetySignal = atom(0);
    const signalSafety = () => safetySignal.set(safetySignal.get() + 1);
    async function checkedStatus(api, safety) {
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
      return !!(registered() && paneRegistration?.active && bindingCurrent(paneRegistration.binding) && paneRegistration.life?.mounted);
    }
    function mayChangePreferences(qc, status, live = () => true) {
      if (!registered() || !live()) return false;
      // With no mounted pane these are deferred defaults only. Do not poll the
      // legacy /status endpoint: before a pane claim it can renew capture.
      if (!hasMountedPane()) return true;
      const latest = typeof qc.getQueryState === 'function' ? qc.getQueryState(cacheKey('status')) : null;
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
      const state = qc.getQueryState?.(cacheKey('status')) || status;
      if (!hasMountedPane()) return safety.captureReleased || (state?.data?.listening !== true && state?.data?.stopping !== true); // deferred defaults, never a capture action
      return captureStatusIdle(state);
    }
    function captureStatusIdle(state) {
      return !!(!state?.error && state?.status !== 'error' && state?.status !== 'pending' && !state?.isPending &&
        (!state?.fetchStatus || state.fetchStatus === 'idle') &&
        state?.data?.listening === false && state.data.stopping === false);
    }
    const captureKey = prefs => JSON.stringify([prefs.backend, prefs.audioSource, prefs.transcriptionModel, prefs.transcriptionProvider, prefs.transcriptionProvider === 'google' ? 'ENDPOINTING' : prefs.asrMode]);
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
      const snapshot = () => qc.getQueryState?.(cacheKey(kind)) || emptyQuery;
      const state = useSyncExternalStore(listener => qc.getQueryCache?.()?.subscribe(listener) || (() => {}), snapshot, snapshot);
      return { ...state, isPending: !!state.isPending || state.status === 'pending' };
    }
    function AnswerPreferences({ prefs, status, projects, models, qc, live }) {
      const data = status.data || {};
      const unsaved = useValue(storageFailed);
      const { project, excludeHistory, answerProvider, answerModel, webFallback } = prefs;
      const allProjects = project === '__all_projects__';
      const mounted = hasMountedPane();
      const providers = mounted ? (models.data?.providers || []) : [{ id: 'hermes', label: 'Follow Hermes default' }, { id: 'openai', label: 'OpenAI API' }, { id: 'openrouter', label: 'OpenRouter' }];
      const entries = Array.isArray(projects.data) ? projects.data : (projects.data?.projects || []);
      const locked = !mayChangePreferences(qc, status, live);
      const change = patch => updatePreferences(patch, qc, status, live);
      const [discovery, setDiscovery] = useState(null);
      const discoveryRequest = useRef(null);
      const chosenProvider = providers.find(p => p.id === answerProvider);
      const currentDiscovery = discovery?.provider === answerProvider && discovery?.pane === paneRegistration
        && bindingCurrent(discovery?.binding) ? discovery : null;
      const listedModels = currentDiscovery?.models || [];
      async function loadModels() {
        const pane = paneRegistration, binding = pane?.binding, provider = preferences.get().answerProvider, revision = answerRevision;
        const allowed = () => hasMountedPane() && paneRegistration === pane && bindingCurrent(binding)
          && preferences.get().answerProvider === provider && answerRevision === revision && mayChangePreferences(qc, status, live);
        if (!['openai', 'openrouter'].includes(provider) || discoveryRequest.current || !allowed() || provider !== answerProvider || chosenProvider?.configured !== true
            || data.capabilities?.model_discovery !== true) return;
        const label = provider === 'openai' ? 'OpenAI' : 'OpenRouter';
        if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm(
          `Load model options from ${label}? This sends the selected provider's saved API key to its official HTTPS model-list endpoint. No meeting, question or project content is sent. ${provider === 'openrouter' ? 'OpenRouter returns a public catalog, not proof of your account access.' : 'The list does not verify Chat Completions access or credits.'}`)) return;
        if (!allowed()) return;
        const token = {}; discoveryRequest.current = token;
        setDiscovery({ provider, pane, binding, loading: true });
        try {
          const result = await request('/models/discover', { provider, consent: true });
          if (!allowed() || discoveryRequest.current !== token) return;
          if (result?.provider !== provider || !Array.isArray(result.models) || result.models.length > 2000
              || result.models.some(id => typeof id !== 'string' || !id || !safeModel(id))) throw new Error('Invalid model listing');
          setDiscovery({ provider, pane, binding, models: [...new Set(result.models)], status: result.status,
            empty: result.models.length === 0 });
        } catch (_) {
          if (allowed() && discoveryRequest.current === token) setDiscovery({ provider, pane, binding,
            error: 'Model options could not be loaded. Check provider setup and connection, then retry or enter an exact model ID manually.' });
        } finally { if (discoveryRequest.current === token) { discoveryRequest.current = null; setDiscovery(current => current?.loading ? null : current); } }
      }
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
              const latest = typeof qc.getQueryState === 'function' ? qc.getQueryState(cacheKey('status')) : status;
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
              onChange: e => { if ((!hasMountedPane() || data.capabilities?.independent_models) && ['', 'hermes', 'openai', 'openrouter'].includes(e.target.value)) change({ answerProvider: e.target.value, answerModel: e.target.value === 'hermes' ? 'default' : '' }); } },
              h('option', { value: '' }, 'Choose provider…'), ...providers.map(p => h('option', { value: p.id, key: p.id, disabled: mounted && p.configured !== true }, (p.label || p.id) + (mounted && p.configured !== true ? (p.id === 'hermes' ? ' — unavailable in this host' : ' — key unavailable') : ''))))),
          answerProvider && answerProvider !== 'hermes' && h(Button, { variant: 'secondary', disabled: locked || !mounted || chosenProvider?.configured !== true || data.capabilities?.model_discovery !== true || !!currentDiscovery?.loading,
            onClick: loadModels }, currentDiscovery?.loading ? 'Loading model options…' : 'Load model options…'),
          answerProvider && answerProvider !== 'hermes' && h('p', { style: muted }, 'Model options are loaded only when you ask. Your saved key goes only to the selected provider; no meeting or project content is sent. Access, credits and Chat Completions compatibility remain unverified.'),
          listedModels.length > 0 && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Listed model options'),
            h('select', { style: selectStyle, value: listedModels.includes(answerModel) ? answerModel : '', disabled: locked,
              'aria-label': 'Listed model options', onChange: e => { if (preferences.get().answerProvider === answerProvider && listedModels.includes(e.target.value) && currentDiscovery?.pane === paneRegistration && bindingCurrent(currentDiscovery?.binding)) change({ answerModel: e.target.value }); } },
              h('option', { value: '' }, 'Choose a listed model…'), ...listedModels.map(id => h('option', { key: id, value: id }, id)))),
          currentDiscovery?.status && h('p', { role: 'status', style: muted }, currentDiscovery.status),
          currentDiscovery?.empty && h('p', { role: 'status', style: muted }, 'No text model options were returned. You can enter an exact compatible model ID manually.'),
          currentDiscovery?.error && h('p', { role: 'alert', style: muted }, currentDiscovery.error),
          answerProvider === 'hermes' && h('p', { role: 'status', style: muted }, 'Answers follow your current Hermes default model and Hermes routing/fallback settings. Hermes keeps its own credentials; no separate answer key is needed. Changing the Hermes default also affects future answers and retries. If this host cannot provide the scoped route, choose an explicit provider below.'),
          answerProvider && answerProvider !== 'hermes' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Answer model'),
            h('input', { style: selectStyle, value: answerModel, disabled: locked, maxLength: 200, placeholder: 'Exact model ID from your provider', onChange: e => change({ answerModel: e.target.value.slice(0, 200) }), 'aria-label': 'Answer model', autoComplete: 'off' })),
          answerProvider && h('p', { role: 'status', style: muted }, mounted ? (providers.find(p => p.id === answerProvider)?.status || 'Answer route unavailable; choose a configured supported provider.') : 'Provider readiness will be checked when you open the pane.'),
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
      const { backend, audioSource, transcriptionProvider, transcriptionModel, asrMode } = effectiveCapture(prefs, data.capabilities);
      const locked = !mayChangeCapture(qc, status, live);
      const change = (field, value, choices) => {
        if (!choices.includes(value) || !mayChangeCapture(qc, status, live)) return;
        const latest = qc.getQueryState?.(cacheKey('status')) || status;
        const caps = latest?.data?.capabilities || {};
        if (hasMountedPane() && ((field === 'backend' && value === 'macos' && !caps.macos_transcription) ||
            (field === 'backend' && value === 'native' && caps.windows_transcription !== true) ||
            (field === 'transcriptionProvider' && value === 'meta' && !caps.meta_asr) ||
            (field === 'transcriptionProvider' && value === 'google' && !caps.google_asr))) return;
        savePreferences({ [field]: value, ...(field === 'transcriptionProvider' ? { transcriptionChoiceVersion: 1 } : {}) });
      };
      return h('section', { style: { ...card, display: 'grid', gap: 10 }, 'aria-label': 'Transcript source preferences' },
        h('strong', null, 'Transcript source'),
        h('p', { style: { ...muted, margin: 0 } }, 'These defaults apply to the next capture. Changing them does not start capture. Select the active meeting and confirm permission in the pane.'),
        !mounted && h('p', { role: 'status', style: muted }, 'The pane is closed. Source and provider readiness will be checked when you open it.'),
        locked && h('p', { role: 'status', style: muted }, 'Pause capture and wait for confirmed idle status before changing transcript settings. If status is unavailable, reopen the pane and check it.'),
        h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-backend-label' }, 'Capture source'),
          h('select', { value: backend, disabled: locked, style: selectStyle, 'aria-labelledby': 'mc-backend-label', onChange: e => change('backend', e.target.value, ['meetily', 'macos', 'native']) },
            h('option', { value: '', disabled: true }, 'Choose a capture source…'),
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
            h('select', { style: selectStyle, value: transcriptionProvider, disabled: locked, 'aria-label': 'Transcription provider', onChange: e => change('transcriptionProvider', e.target.value, ['local', 'meta', 'google']) },
              h('option', { value: 'local' }, 'Local — faster-whisper on this Mac'),
              h('option', { value: 'meta', disabled: mounted && !data.capabilities?.meta_asr, title: mounted && !data.capabilities?.meta_asr ? 'Set the Meta API key in Hermes Settings → Plugins → meeting-copilot-live, then reopen the pane.' : '' }, 'Meta Muse Voice — cloud transcription'),
              h('option', { value: 'google', disabled: mounted && !data.capabilities?.google_asr }, 'Google Gemini — 60-second cloud preview'))),
          backend === 'macos' && mounted && !status.isPending && !status.error && !data.capabilities?.meta_asr && h('p', { role: 'status', style: muted }, 'Meta transcription is unavailable. Set the Meta API key in managed Agent settings, then reopen the pane to recheck readiness.'),
          (backend === 'native' || transcriptionProvider === 'local') && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Local transcription model'),
            h('select', { style: selectStyle, value: transcriptionModel, disabled: locked, 'aria-label': 'Local transcription model', onChange: e => change('transcriptionModel', e.target.value, ['tiny.en', 'base.en', 'small.en']) },
              h('option', { value: 'tiny.en' }, 'Whisper tiny.en — fastest'), h('option', { value: 'base.en' }, 'Whisper base.en — balanced'), h('option', { value: 'small.en' }, 'Whisper small.en — higher accuracy'))),
          backend === 'native' && h('span', { style: muted }, 'Local transcription uses the default microphone and all audio on the default output, according to Audio source. Prepare Whisper weights before Start. ', data.capabilities?.windows_detail || 'Experimental; hardware is unverified.'),
          backend === 'macos' && transcriptionProvider === 'local' && h('span', { style: muted }, 'Runs on this Mac without Meetily. Prepare the selected model before Start. Microphone or screen and system audio permission is required.'),
          backend === 'macos' && transcriptionProvider === 'google' && h('span', { style: muted }, 'Source labels only · no speaker diarization'),
          backend === 'macos' && transcriptionProvider === 'google' && h('p', { role: 'status', style: muted }, 'Google Gemini 3.5 Transcribe Live: explicit 60-second cloud preview. Configure its dedicated key in Hermes plugin secrets. Audio is sent to Google; free-tier data may improve Google products. No automatic retries or session rollover.'),
          backend === 'macos' && transcriptionProvider === 'meta' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Meta ASR mode'),
            h('select', { style: selectStyle, value: asrMode, disabled: locked, 'aria-label': 'Meta ASR mode', onChange: e => change('asrMode', e.target.value, ['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK']) },
              h('option', { value: 'ENDPOINTING' }, 'Endpointing — one segment per utterance'), h('option', { value: 'DIARIZATION' }, 'Diarization — speaker labels per source'), h('option', { value: 'PUSH_TO_TALK' }, 'Push-to-talk — single-turn finals'))),
          backend === 'macos' && transcriptionProvider === 'meta' && h('span', { role: 'status', style: muted }, 'Cloud transcription: meeting audio is sent to Meta (muse-voice-transcribe) while listening. Only final turns are kept; the API key stays in managed secret settings.')));
    }
    function SettingsPage() {
      if (routeSupported) { sdk.useGatewayActivationEpoch(); useValue(host.state.profile); useValue(host.state.connectionId); }
      const settingsBinding = currentBinding();
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
        h(CapturePreferences, { prefs, status, qc, live: () => live.current && bindingCurrent(settingsBinding) }),
        h('h3', { style: { margin: 0 } }, 'Answer setup'),
        h(AnswerPreferences, { prefs, status, projects, models, qc, live: () => live.current && bindingCurrent(settingsBinding) }));
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
      if (!life.pane) {
        if (life.claimAttempted && !bindingCurrent(life.binding)) notify('warning', 'Gateway changed during pane authorization. The previous capture and overlay may still be active; verify the OS audio indicator and previous gateway.');
        return Promise.resolve({ accepted: false });
      }
      // There is no public route-pinned ctx.rest. Never let old teardown hit
      // the newly active backend; the old lease expires, but stop is unproved.
      if (!bindingCurrent(life.binding)) {
        notify('warning', 'Gateway changed; the previous capture and overlay could not be released on their original backend. Lease expiry initiates stop but cannot guarantee physical shutdown. Verify the OS audio indicator and previous gateway.');
        return Promise.resolve({ accepted: false, routeChanged: true });
      }
      const safety = life.safety;
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
        if (!bindingCurrent(life.binding)) return;
        finishCaptureRelease(result);
        if (overlayToken === safety.overlayToken && result?.accepted === true && result?.overlay_stop_confirmed === true) {
          safety.overlayVisible = false; safety.overlayUnknown = false;
          signalSafety();
        }
        if (result?.stop_confirmed === false || result?.stopping) warnStop();
        if (result?.overlay_stop_confirmed === false) notify('warning', result.overlay_cleanup_error || 'Overlay shutdown is not confirmed. Reopen the pane and Retry hide overlay.');
      }, () => { if (bindingCurrent(life.binding)) { finishCaptureRelease(null); warnStop(); } });
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
      const safety = paneEntry.safety;
      const cacheKey = kind => bindingKey(paneEntry.binding, kind);
      const usable = () => registered() && paneEntry.active && !paneEntry.retired && bindingCurrent(paneEntry.binding);
      const api = async (path, body) => {
        const statusRead = path === '/status' && body === undefined;
        const mayReadStatus = () => active && statusRead && bindingCurrent(paneEntry.binding);
        if (!usable() && !mayReadStatus()) throw new Error('This pane is retired. Close and reopen it.');
        const result = await request(path, body);
        // A late same-route claim must expose its exact owner to cleanup even
        // after unload. It must never be released through a different route.
        const lateClaim = path === '/overlay/claim' && bindingCurrent(paneEntry.binding);
        if (!usable() && !mayReadStatus() && !lateClaim) throw new Error('This pane is retired. Close and reopen it.');
        return result;
      };
      async function writeClipboard(text) {
        if (!usable()) throw new Error('This pane is retired.');
        if (typeof ctx.os?.writeClipboard === 'function') {
          // A false result (or rejection) is a failure, not permission to
          // bypass the host's clipboard decision via a second door.
          if (await ctx.os.writeClipboard(text) !== true) throw new Error('Clipboard unavailable.');
        } else {
          // Older hosts without the SDK door may expose the browser API.
          if (typeof navigator === 'undefined' || typeof navigator.clipboard?.writeText !== 'function') throw new Error('Clipboard unavailable.');
          await navigator.clipboard.writeText(text);
        }
      }
      const qc = useQueryClient();
      const saved = preferences.get();
      const prefs = useValue(preferences);
      const { project, webFallback, answerProvider, answerModel, excludeHistory } = prefs;
      const [question, setQuestion] = useState('');
      const [web, setWeb] = useState('');
      const [consent, setConsent] = useState(false);
      const [fallbackBackend, setBackend] = useState(saved.backend || '');
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
      const [fallbackAudioSource, setAudioSource] = useState(saved.audioSource || 'both');
      const transcriptionModel = settingsAvailable ? prefs.transcriptionModel : fallbackTranscriptionModel;
      const audioSource = settingsAvailable ? prefs.audioSource : fallbackAudioSource;
      const [overlayOn, setOverlayOn] = useState(false);
      const [overlayMsg, setOverlayMsg] = useState('');
      const [copied, setCopied] = useState(false);
      const [persistWarning, setPersistWarning] = useState('');


      const status = useQuery({ queryKey: cacheKey('status'), queryFn: () => checkedStatus(api, safety), refetchInterval: 2500, retry: 1 });
      const backend = (settingsAvailable ? prefs.backend : fallbackBackend) || defaultBackend(status.data?.capabilities);
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
      const projects = useQuery({ queryKey: cacheKey('projects'), queryFn: () => api('/projects'), staleTime: 60000, refetchInterval: query => query.state.error ? 5000 : false, retry: 3 });
      const recordings = useQuery({ queryKey: cacheKey('recordings'), queryFn: () => api('/recordings'), refetchInterval: backend === 'meetily' ? 5000 : false, enabled: backend === 'meetily', retry: 1 });
      const data = status.data || {};
      const modelCatalog = useQuery({ queryKey: cacheKey('models'), queryFn: () => api('/models'), enabled: !!data.capabilities?.independent_models, staleTime: 60000, retry: 1 });
      const providers = modelCatalog.data?.providers || [];
      const answerProviderAvailable = ['hermes', 'openai', 'openrouter'].includes(answerProvider) && !modelCatalog.error && providers.some(p => p.id === answerProvider && p.configured === true);
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
          binding: paneEntry.binding, safety,
          creation: ++order.next,
          alive: true, syncedKey: null, released: false,
          get mounted() { return this.alive && registered() && paneEntry.active && paneRegistration === paneEntry && bindingCurrent(this.binding); },
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
        try { await qc.invalidateQueries({ queryKey: cacheKey('status') }); }
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
      const [fallbackTranscriptionProvider, setTranscriptionProvider] = useState(['meta', 'google'].includes(saved.transcriptionProvider) ? saved.transcriptionProvider : 'local');
      const [fallbackAsrMode, setAsrMode] = useState(['ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK'].includes(saved.asrMode) ? saved.asrMode : 'ENDPOINTING');
      const transcriptionProvider = settingsAvailable ? prefs.transcriptionProvider : fallbackTranscriptionProvider;
      const asrMode = transcriptionProvider === 'google' ? 'ENDPOINTING' : (settingsAvailable ? prefs.asrMode : fallbackAsrMode);
      const [setupOpen, setSetupOpen] = useState(false);
      const [answerUncertain, setAnswerUncertain] = useState(false);
      const [progressPaused, setProgressPaused] = useState(false);
      // Older hosts persist only actual source edits, never inferred defaults.
      // Capability discovery never opts a meeting into cloud audio. Only an
      // explicit selection or persisted provider choice may select Meta.
      function resetMeetingInputs() {
        setQuestion(''); setWeb(''); setLastAsked(null); setLiveEntry(null);
        setTrackedRequest(null); setError(''); setAssistFailed(false);
        setAnswerUncertain(false); setProgressPaused(false);
        if (safety.answer && safety.answerKnown) { safety.answerToken += 1; signalSafety(); }
      }
      const cloudUnavailable = backend === 'macos' && ((transcriptionProvider === 'meta' && !data.capabilities?.meta_asr) || (transcriptionProvider === 'google' && (!data.capabilities?.google_asr || asrMode !== 'ENDPOINTING')));
      const windowsUnavailable = backend === 'native' && data.capabilities?.windows_transcription !== true;
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
          await qc.invalidateQueries({ queryKey: cacheKey('status') });
        } catch (e) { safety.overlayUnknown = true; if (life.mounted) {
          setOverlayMsg('Overlay action not confirmed. Retry Show / Hide overlay; if it still fails, close and reopen this pane. Check the macOS helper and backend connection. A window may still be visible: verify before screen-sharing.');
          if (!overlayOn) setOverlayOn(false);
        } }
        finally { safety.overlayAction = false; signalSafety(); if (life.mounted) setPending(false); }
      }
      async function startCapture(newMeeting = false) {
        const canStartCurrent = token => {
          if (!usable() || safety.permissionAction || !actionLife.mounted || overlayActionAt.current !== actionLife) return false;
          if (token === undefined ? (safety.captureAction || safety.captureUnknown)
              : (!safety.captureAction || safety.captureToken !== token)) return false;
          const latest = qc.getQueryState?.(cacheKey('status')) || status;
          const caps = latest?.data?.capabilities || {};
          const current = settingsAvailable ? effectiveCapture(preferences.get(), caps)
            : { ...captureInput.current.source, backend: captureInput.current.explicitBackend || defaultBackend(caps) };
          return !!(freshPreflight() && captureStatusIdle(latest) && !latest?.data?.overlay_cleanup_error && !actionLife.credentialShutdownUnconfirmed &&
            consentPrefs.current && captureKey(current) === captureKey(renderedCapture) && captureInput.current.recording === recording &&
            captureRevision === renderedCaptureRevision && consentPrefs.current.revision === captureRevision && consentPrefs.current.key === captureKey(current) &&
            sourceVersion.current.revision === renderedSourceRevision && consentPrefs.current.sourceRevision === renderedSourceRevision &&
            !!backend && (backend !== 'meetily' || recording) && (backend !== 'macos' || (caps.macos_transcription && (transcriptionProvider !== 'meta' || caps.meta_asr) && (transcriptionProvider !== 'google' || (caps.google_asr && asrMode === 'ENDPOINTING')))) &&
            (backend !== 'native' || caps.windows_transcription === true));
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
        const latestState = typeof qc.getQueryState === 'function' ? qc.getQueryState(cacheKey('status')) : status;
        const currentData = latestState?.data || data;
        const catalog = typeof qc.getQueryData === 'function' ? qc.getQueryData(cacheKey('models')) : null;
        const currentProviders = catalog?.providers || providers;
        const isAll = currentPrefs.project === '__all_projects__';
        const invalid = (isAll && !currentData.capabilities?.all_projects) ||
          (currentPrefs.webFallback && !currentData.capabilities?.automatic_web_fallback) ||
          !['hermes', 'openai', 'openrouter'].includes(currentPrefs.answerProvider) || !currentPrefs.answerModel ||
          !currentProviders.some(p => p.id === currentPrefs.answerProvider && p.configured === true) || !currentData.capabilities?.independent_models ||
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
        try { await qc.invalidateQueries({ queryKey: cacheKey('status') }); }
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
        try { await qc.invalidateQueries({ queryKey: cacheKey('status') }); }
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
            const entry = await api(`/assist/${trackedRequest}?missing=sentinel-v1`);
            if (!alive) return;
            if (entry?.contract === 'answer-progress-v1' && entry.status === 'missing') {
              safety.answer = false;
              signalSafety();
              setTrackedRequest(null); setLiveEntry(null);
              setError('That answer is no longer available (new meeting or restart cleared it). Ask again.');
              try { await qc.invalidateQueries({ queryKey: cacheKey('status') }); } catch (ignored) {}
              return;
            }
            if (!entry) {
              // The slot is gone (cleared by a newer session or restart): stop tracking
              // with an actionable explanation instead of stale progress.
              if (++misses >= 2) {
                setTrackedRequest(null); setLiveEntry(null);
                setError('That answer is no longer available (new meeting or restart cleared it). Ask again.');
                try { await qc.invalidateQueries({ queryKey: cacheKey('status') }); } catch (ignored) {}
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
              try { await qc.invalidateQueries({ queryKey: cacheKey('status') }, { throwOnError: true }); } catch (ignored) {}
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
      const captureLabel = data.listening ? (['loading', 'starting'].includes(data.capture?.state) ? 'Starting…' : 'Listening') : 'Paused';
      const statusLabel = status.isPending ? 'Connecting…' : status.error ? 'Unavailable' : data.google_fixture_active ? 'Google audio test' : data.stopping ? 'Stopping — unconfirmed' : failed ? (data.listening ? `${captureLabel} · needs attention` : 'Needs attention') : captureLabel;
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
          try {
            const reducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
            el.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'nearest' });
          } catch (ignored) {}
        }
      }, [result?.answer, liveEntry?.answer, liveEntry?.status]);
      const [, setRetirement] = useState('');
      const renderedCapture = { backend, audioSource, transcriptionModel, transcriptionProvider, asrMode };
      // Even a transient inferred source change revokes old consent; switching
      // back must not resurrect it or a Start waiting on an earlier claim.
      const sourceVersion = useRef({ backend, revision: 0 });
      if (sourceVersion.current.backend !== backend) {
        sourceVersion.current = { backend, revision: sourceVersion.current.revision + 1 };
      }
      const renderedSourceRevision = sourceVersion.current.revision;
      const renderedCaptureRevision = captureRevision;
      const consentPrefs = useRef(consent ? { key: captureKey(renderedCapture), revision: renderedCaptureRevision, sourceRevision: renderedSourceRevision } : null);
      // Event-time authority, independent of React's next render. A source or
      // recording change also invalidates a Start already awaiting its claim.
      const captureInput = useRef({ source: renderedCapture, explicitBackend: fallbackBackend, recording, revision: 0 });
      // A late authoritative discovery changes the consent-bound source too.
      if (!settingsAvailable && !fallbackBackend && captureInput.current.source.backend !== backend) {
        captureInput.current.source = { ...captureInput.current.source, backend };
        captureInput.current.revision += 1;
      }
      const changeCaptureInput = (field, value, setter) => {
        if (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife) return;
        if (field === 'recording') captureInput.current.recording = value;
        else {
          captureInput.current.source = { ...captureInput.current.source, [field]: value };
          if (field === 'backend') captureInput.current.explicitBackend = value;
          if (!settingsAvailable) savePreferences({ [field]: value, ...(field === 'transcriptionProvider' ? { transcriptionChoiceVersion: 1 } : {}) });
        }
        captureInput.current.revision += 1;
        setter(value);
      };
      const [, setConsentRevision] = useState(0);
      useLayoutEffect(() => {
        // Query notifications may be batched into one React render. Fence every
        // effective-source transition synchronously, including away-and-back.
        const observeSource = () => {
          if (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife) return;
          const latest = qc.getQueryState?.(cacheKey('status')) || status;
          const next = (settingsAvailable ? preferences.get().backend : captureInput.current.explicitBackend)
            || defaultBackend(latest?.data?.capabilities);
          if (sourceVersion.current.backend === next) return;
          sourceVersion.current = { backend: next, revision: sourceVersion.current.revision + 1 };
          setConsentRevision(value => value + 1);
        };
        const unsubscribe = qc.getQueryCache?.()?.subscribe(observeSource);
        observeSource();
        return () => unsubscribe?.();
      }, [qc, actionLife]);
      const consentValid = consent && consentPrefs.current?.key === captureKey(renderedCapture) && consentPrefs.current?.revision === renderedCaptureRevision && consentPrefs.current?.sourceRevision === renderedSourceRevision;
      const preflightBody = { consent: !!consentValid, backend, recording_id: recording || null,
        transcription_model: transcriptionModel, audio_source: audioSource,
        transcription_provider: backend === 'native' ? 'local' : transcriptionProvider,
        asr_mode: backend === 'native' ? 'ENDPOINTING' : asrMode };
      const preflightSignature = JSON.stringify([preflightBody, renderedCaptureRevision, renderedSourceRevision, captureInput.current.revision]);
      const preflightKey = [...cacheKey('preflight'), preflightSignature];
      const preflight = useQuery({ queryKey: preflightKey, enabled: !!backend && data.capabilities?.capture_preflight === 1 && !data.listening && !data.stopping && !status.error,
        queryFn: async () => {
          const result = await api('/preflight', preflightBody);
          return { ...result, receivedAt: Date.now(), signature: preflightSignature };
        }, staleTime: 15000, refetchOnWindowFocus: false, retry: false });
      function freshPreflight() {
        const current = qc.getQueryState?.(preflightKey) || preflight;
        const report = current?.data, age = Date.now() - report?.receivedAt;
        return (qc.getQueryState?.(cacheKey('status')) || status)?.data?.capabilities?.capture_preflight === 1 && !current?.error && current?.status !== 'error' && !current?.isPending
          && current?.fetchStatus !== 'fetching' && report?.version === 1 && report.ready === true
          && report.signature === preflightSignature && Number.isFinite(age) && age >= 0 && age < 15000
          && Array.isArray(report.checks) && report.checks.length > 0 && report.checks.every(c => c?.status === 'ready');
      }
      async function requestPermissions() {
        if (!usable() || safety.permissionAction || backend !== 'macos' || !captureStatusIdle(qc.getQueryState?.(cacheKey('status')) || status) || safety.captureAction) return;
        if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm(
          'Request macOS permissions for the selected audio source? macOS may show microphone and screen/system audio permission dialogs. No recording starts. Review each OS prompt yourself, then recheck required settings.')) return;
        const source = captureKey(renderedCapture), revision = captureRevision, life = overlayActionAt.current;
        safety.permissionAction = true; signalSafety(); setPending(true);
        try {
          const pane = await claimOverlayPane(life);
          const current = settingsAvailable ? effectiveCapture(preferences.get(), data.capabilities) : captureInput.current.source;
          if (!usable() || !life.mounted || life !== overlayActionAt.current || source !== captureKey(current) || revision !== captureRevision || !captureStatusIdle(qc.getQueryState?.(cacheKey('status')) || status)) return;
          const result = await api('/permissions/request', { pane, action: ++life.action, audio_source: audioSource, consent: true });
          if (usable() && life.mounted) { setError(result?.status === 'completed' ? 'Permission request finished. Review macOS settings, then recheck required settings.' : 'Permission setup is not confirmed. Finish or dismiss any existing OS prompt, check macOS Privacy & Security and recheck settings.'); await qc.invalidateQueries({ queryKey: cacheKey('preflight') }); }
        } catch (_) { if (usable() && life.mounted) setError('Permission setup was not confirmed. Check macOS Privacy & Security, then recheck required settings. No capture was requested.'); }
        finally { safety.permissionAction = false; signalSafety(); if (life.mounted) setPending(false); }
      }
      // Review notes stay in this mounted pane only, never in preferences or disk.
      // Fence both render and retained handlers at the backend meeting boundary.
      const [reviewNotes, setReviewNotes] = useState({ generation: null, items: [] });
      const notesGeneration = data.generation;
      const notesCurrent = () => usable() && !status.error && Number.isSafeInteger(notesGeneration)
        && (qc.getQueryState?.(cacheKey('status')) || status)?.data?.generation === notesGeneration;
      const noteItems = reviewNotes.generation === notesGeneration ? reviewNotes.items : [];
      useEffect(() => {
        setReviewNotes(old => old.generation === notesGeneration ? old : { generation: notesGeneration, items: [] });
      }, [notesGeneration]);
      const updateNotes = change => {
        if (!notesCurrent()) return;
        setReviewNotes(old => ({ generation: notesGeneration,
          items: change(old.generation === notesGeneration ? old.items : []) }));
      };
      const pinAnswer = () => {
        if (!notesCurrent() || typeof result?.answer !== 'string' || !result.answer) return;
        const key = result.request_id || result.answer;
        updateNotes(items => items.length >= 12 || items.some(n => n.key === key) ? items : [...items, {
          key, text: result.answer.slice(0, 12000), reviewed: false,
          // Snapshot only visible evidence, not hidden transcript context.
          evidence: evidenceReport({ ...result, answer: '(See original answer in the pane history.)' }).slice(0, 20000),
          shortened: result.answer.length > 12000,
        }]);
      };
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
          try { await writeClipboard(snapshot.provider_json); if (usable()) notify('info', 'Sent transcript context copied. Review before sharing.'); }
          catch (_) { if (usable()) setError('Clipboard unavailable. Select and copy the sent context manually.'); }
        } }, 'Copy sent transcript context'));
      const evidenceItem = (s, i) => {
        const excerpt = evidenceExcerpt(s.text);
        return h('div', { className: 'mc-evidence-card', key: s.id || i, style: { ...muted, marginTop: 8, padding: '8px 10px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, overflowWrap: 'anywhere' } },
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
        try { await qc.invalidateQueries({ queryKey: cacheKey('status') }, { throwOnError: true }); }
        catch (_) { setError('Backend unavailable. Check the backend connection, installation, and plugin enablement.'); }
      };
      const errorCard = (message, label, onClick, disabled = false) => message && h('div', { className: 'mc-error', role: 'alert', style: card },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' } },
          h('span', null, message), h(Button, { variant: 'secondary', disabled: pending || disabled, onClick }, label)));
      // A lost submission response cannot authorize replay. Progress failures
      // likewise recheck without borrowing another message's retry request.
      const localRecheck = assistFailed || (typeof error === 'string' && /^(Answer progress|Answer submission|Cancellation was not confirmed)/.test(error));
      const restartCapture = !data.listening && !data.stopping;
      const selectedCloudProvider = backend === 'macos' && ['meta', 'google'].includes(transcriptionProvider) ? transcriptionProvider : null;
      const activeCloudProvider = data.google_fixture_active ? 'google' : (data.listening || data.stopping) && data.last_session?.backend === 'macos' && ['meta', 'google'].includes(data.last_session?.transcription_provider) ? data.last_session.transcription_provider : null;
      const cloudName = (activeCloudProvider || selectedCloudProvider) === 'google' ? 'Google' : 'Meta';
      const cloudAudioSelected = !!selectedCloudProvider;
      const cloudAudioActive = data.listening && !!activeCloudProvider;
      const cloudAudioUnconfirmed = data.stopping && !!activeCloudProvider;
      const cloudAudioDisclosed = cloudAudioSelected || cloudAudioActive || cloudAudioUnconfirmed;
      const captureMessage = (data.capture?.error || data.capture?.warning) && typeof data.capture?.message === 'string' ? data.capture.message.slice(0, 500) : '';
      const audioAttention = audioAttentionText(data.capture?.audio_diagnostics);
      const sourceSummary = data.google_fixture_active ? 'Synthetic WAV → Google · no microphone or system capture' : !backend ? 'Not identified — choose a capture source'
        : backend === 'meetily' ? (recording ? 'Meetily · recording selected' : 'Meetily — choose an active recording below')
        : `${backend === 'macos' ? 'macOS' : 'Windows'} · ${{ microphone: 'Microphone', system: 'System audio', both: 'Microphone + system audio' }[audioSource] || audioSource} · ${cloudAudioSelected ? (selectedCloudProvider === 'google' ? 'Google Gemini (60s preview · source labels)' : `Meta (${asrMode})`) : `Local transcription (${transcriptionModel})`}`;
      const captureBlocked = !freshPreflight() || !captureStatusIdle(status) || !backend || overlayNeedsHide || cloudUnavailable || windowsUnavailable || !consentValid || (backend === 'meetily' && !recording)
        || (settingsAvailable && !mayChangeCapture(qc, status, () => usable() && actionLife.mounted && overlayActionAt.current === actionLife))
        || (backend === 'macos' && !data.capabilities?.macos_transcription);
      const startReady = !pending && !captureBlocked;
      const hasRecentTranscript = Array.isArray(data.segments) && data.segments.slice(-40)
        .some(segment => typeof segment?.text === 'string' && segment.text.trim());
      const contextActions = [
        ['Catch me up', 'Summarize only the recent available meeting transcript. Use sections: Summary, Decisions, Open questions. Distinguish tentative proposals from confirmed decisions. Say when the transcript does not establish something; do not imply this covers the whole meeting.'],
        ['Extract actions', 'Extract action items only from the recent available meeting transcript. For each item show Action, Owner, Deadline. Include an owner or deadline only if explicitly stated; otherwise write unspecified. Distinguish proposed actions from agreed commitments. If none are stated, say so.'],
        ['Suggest a question', 'Suggest one useful clarifying question grounded in the recent available meeting transcript, followed by one short reason to ask it. Do not invent facts, decisions, or commitments. If context is insufficient, say so.'],
      ];
      const checkRows = Array.isArray(preflight.data?.checks) ? preflight.data.checks : [];
      const passedChecks = checkRows.filter(c => c.status === 'ready');
      const blockedChecks = checkRows.filter(c => c.status !== 'ready');
      const checksLabel = startReady ? 'Ready to start' : status.error ? 'Unavailable' : data.google_fixture_active ? 'Google audio test' : data.stopping ? 'Stop unconfirmed'
        : status.isPending || preflight.isPending ? 'Checking…' : blockedChecks.length ? 'Needs setup' : freshPreflight() ? 'Start blocked' : 'Recheck required';
      const renderCheck = c => h('li', { key: c.id, 'data-check-status': c.status },
        h(PaneIcon, { name: c.status === 'ready' ? 'check' : 'alert' }),
        h('div', null, h('strong', null, `${c.status === 'ready' ? 'Ready' : c.status === 'blocked' ? 'Needs setup' : 'Not verified'}: ${c.label}`), c.detail));
      const stopControl = data.stopping
        ? h(Button, { variant: 'secondary', disabled: pending, onClick: () => act('/pause', {}) }, 'Retry stop')
        : data.listening ? h(Button, { variant: 'secondary', onClick: () => act('/pause', {}) }, data.backend === 'meetily' ? 'Detach' : 'Pause listening') : null;
      return h('div', { className: 'mc-workspace', style: { padding: 12, display: 'flex', flexDirection: 'column', gap: 10, height: '100%', minWidth: 0, width: '100%', boxSizing: 'border-box', overflowY: 'auto', overflowX: 'hidden', overflowWrap: 'anywhere', color: 'var(--ui-text-primary)' } },
        h('style', null, workspaceStyles),
        h('div', { className: 'mc-rail', 'data-capture-rail': true },
          h('header', { className: 'mc-header' }, h('div', { className: 'mc-mark' }, h(WorkspaceMark)),
            h('h1', { className: 'mc-title' }, 'Meeting Copilot')),
          h('div', { className: 'mc-rail-actions' },
            h('span', { className: 'mc-status', role: 'status', 'data-state': data.listening && !data.stopping && !status.error && !status.isPending ? 'listening' : 'idle' },
              h('span', { className: 'mc-status-dot', 'aria-hidden': true }), h(Badge, null, statusLabel)),
            stopControl,
            (data.listening || data.stopping) && data.backend === 'meetily' && h('span', { 'data-detach-disclosure': true, style: { ...muted, fontSize: 11 } }, 'Detach does not stop Meetily recording.'),
            settingsAvailable && typeof host.navigate === 'function' && h('button', { type: 'button', onClick: () => { if (usable()) host.navigate(`/settings?tab=plugins&plugin=${ID}`); }, style: { fontSize: 11, color: 'var(--ui-text-secondary)', background: 'transparent', border: 0, cursor: 'pointer' } }, 'Open settings'))),
        // Capture session first: the permission check and the primary
        // start/pause actions live at the top, before any setup detail.
        h('section', { className: 'mc-capture', style: { ...card, display: 'grid', gap: 10 }, 'aria-label': 'Capture session' },
          h('div', { className: 'mc-chips', 'aria-label': 'Capture source' },
            h('span', { className: 'mc-chip', title: 'Next capture' }, `${data.listening ? 'Next capture' : 'Capture source'}: ${sourceSummary}`)),
          (status.error || status.isPending || data.stopping) && h('p', { role: 'status', style: muted }, status.error ? 'Capture status unavailable. Check the backend connection.' : status.isPending ? 'Checking capture status…' : 'Stop is not confirmed — audio may still be active.'),
          cloudAudioDisclosed && h('p', { id: `${paneEntry.a11yId}-cloud-audio`, 'data-cloud-capture-disclosure': true, role: 'status', className: 'mc-notice' }, h(PaneIcon, { name: 'cloud' }), cloudAudioUnconfirmed ? `${cloudName} cloud transcription may still be active. Audio transmission is not confirmed stopped; retry stop.` : `Cloud transcription ${cloudAudioActive ? 'active' : 'selected'}: meeting audio is sent to ${cloudName} while listening. ${data.listening ? 'Pause to change.' : 'Next capture.'}`),
          (selectedCloudProvider === 'google' || activeCloudProvider === 'google') && h('p', { role: 'status', 'data-google-preview-disclosure': true, className: 'mc-notice' }, 'Google preview: 60 seconds per source, no speaker diarization. Free-tier audio may improve Google products; use a paid project for private meetings. Each source reserves $1 of the shared $4 local preview allowance, not an actual charge or guaranteed bill cap.'),
          h('label', { className: 'mc-consent', style: { display: 'flex', gap: 8, alignItems: 'flex-start', padding: '2px 0' } },
            h('input', { type: 'checkbox', checked: consentValid, disabled: data.listening, onChange: e => {
              if (e.target.checked && (!usable() || !actionLife.mounted || overlayActionAt.current !== actionLife ||
                  captureInput.current.recording !== recording || captureRevision !== renderedCaptureRevision || sourceVersion.current.revision !== renderedSourceRevision ||
                  (!settingsAvailable && captureKey(captureInput.current.source) !== captureKey(renderedCapture)) ||
                  (settingsAvailable && captureKey(effectiveCapture(preferences.get(), (qc.getQueryState?.(cacheKey('status')) || status)?.data?.capabilities)) !== captureKey(renderedCapture)))) return;
              consentPrefs.current = e.target.checked ? { key: captureKey(renderedCapture), revision: renderedCaptureRevision, sourceRevision: renderedSourceRevision } : null;
              setConsent(e.target.checked);
              setConsentRevision(value => value + 1);
            }, 'aria-describedby': cloudAudioDisclosed ? `${paneEntry.a11yId}-cloud-audio` : undefined }),
            h('span', null, 'I have permission to capture/read this meeting and use its context with my configured model.')),
          h('div', { className: 'mc-actions' },
            !data.listening && !data.stopping && h(Button, { disabled: pending || captureBlocked, onClick: () => startCapture() }, ((data.segments || []).length || result || (data.recent || []).length) ? (backend === 'meetily' ? 'Resume' : 'Resume listening') : (backend === 'meetily' ? 'Attach to Meetily' : 'Start listening')),
            !data.listening && !data.stopping && !!((data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending || captureBlocked, title: 'Starts a fresh meeting; the earlier transcript, answers, and retry data are cleared', onClick: () => startCapture(true) }, 'New meeting'),
            !!(data.overlay_cleanup_error || (data.segments || []).length || result || (data.recent || []).length) && h(Button, { variant: 'secondary', disabled: pending, title: 'Clears past Q&A, transcript segments, and cached snippets for this meeting', onClick: async () => { if (typeof window !== 'undefined' && window.confirm && !window.confirm('Clear this meeting? Past questions, answers, and transcript segments are removed from the pane.')) return; if (await act('/clear')) { setQuestion(''); setLastAsked(null); setLiveEntry(null); setTrackedRequest(null); setError(''); setAssistFailed(false); } } }, 'Clear meeting')),
          !data.listening && h('section', { className: 'mc-checks', 'aria-label': 'Required capture checks' },
            h('div', { className: 'mc-checks-head' },
              h('span', { className: 'mc-chip', 'data-tone': startReady ? 'ready' : 'warning', role: 'status' },
                h(PaneIcon, { name: startReady ? 'check' : 'alert' }), checksLabel),
              h(Button, { variant: 'secondary', 'aria-label': 'Recheck required settings', disabled: pending || !backend || status.error || data.capabilities?.capture_preflight !== 1,
                onClick: () => { if (usable()) return qc.invalidateQueries({ queryKey: cacheKey('preflight') }); } }, 'Recheck')),
            data.capabilities?.capture_preflight !== 1 ? h('p', { role: 'alert', style: muted }, 'Capture checks need an updated backend. Update the plugin and reopen this pane.')
              : preflight.error ? h('p', { role: 'alert', style: muted }, 'Required checks unavailable. Restore the connection, then recheck.')
              : preflight.isPending || !preflight.data ? h('p', { role: 'status', style: muted }, 'Checking required settings…')
              : h('ul', { className: 'mc-check-list', 'data-blocked-checks': true }, ...blockedChecks.map(renderCheck)),
            passedChecks.length > 0 && h('details', { className: 'mc-check-details', 'data-passed-checks': true },
              h('summary', { style: { cursor: 'pointer' } }, `${passedChecks.length} checks passed`),
              h('ul', { className: 'mc-check-list' }, ...passedChecks.map(renderCheck))),
            preflight.data && !freshPreflight() && !blockedChecks.length && h('p', { role: 'status', style: muted }, 'Recheck before Start. Checks expire after 15 seconds.'),
            h('details', { className: 'mc-check-details', open: blockedChecks.some(c => ['microphone_permission', 'system_permission'].includes(c.id)) }, h('summary', { style: { cursor: 'pointer' } }, 'Check details & permissions'),
              h('p', { style: muted }, 'Checks never record, request OS permission, download models or contact providers. Start checks again. Answer setup is separate.'),
              backend === 'macos' && h(Button, { variant: 'secondary', disabled: pending || !captureStatusIdle(status) || data.capabilities?.capture_preflight !== 1,
                onClick: requestPermissions }, 'Request macOS permissions…'))),
          data.listening && data.backend === 'macos' && !data.last_session?.transcription_provider && h('p', { role: 'status', style: muted }, 'The active transcription provider is not confirmed. Check capture status and pause if unsure.'),
          cloudUnavailable && h('p', { role: 'status', style: muted }, `${transcriptionProvider === 'google' ? 'Google preview' : 'Meta cloud transcription'} is unavailable. Choose Local in ${settingsAvailable ? 'Meeting Copilot settings' : 'Transcript source'}, or configure the selected provider key and supported mode, then recheck readiness before Start.`),
          backend === 'macos' && !data.capabilities?.macos_transcription && h('p', { role: 'status', style: muted }, data.capabilities?.macos_detail || 'macOS transcription readiness was not reported. Check the backend and macOS setup guide.'),
          windowsUnavailable && h('p', { role: 'status', style: muted }, data.capabilities?.windows_detail || 'Windows capture readiness was not reported. Check the backend and Windows setup guide.'),
          !data.listening && !data.stopping && (pending || !backend || !consentValid || (backend === 'meetily' && !recording)) && h('p', { style: { ...muted, margin: 0 }, role: 'status' },
            pending ? 'Starting…' : !backend ? `Choose a capture source in ${settingsAvailable ? 'Meeting Copilot settings' : 'Transcript source below'}.` : !consentValid ? (consent ? 'Capture settings changed. Confirm permission again before starting.' : 'Confirm permission to enable Start.') : 'Select an active Meetily recording below to enable Attach.'),
          h('div', { className: 'mc-secondary', role: overlayNeedsHide ? 'alert' : undefined, style: { display: 'grid', gap: 5 } },
            h(Button, { variant: 'secondary', disabled: pending || (!data.capabilities?.overlay && !overlayNeedsHide), title: 'Show or hide the read-only meeting overlay (macOS)', onClick: toggleOverlay }, overlayNeedsHide ? 'Retry hide overlay' : overlayOn ? 'Hide overlay' : 'Show overlay'),
            (data.overlay_cleanup_error || overlayMsg) && h('p', { role: overlayNeedsHide ? undefined : 'status', style: { ...muted, margin: 0 } }, data.overlay_cleanup_error || overlayMsg),
            h('p', { style: { ...muted, margin: 0 } }, 'Screen-share privacy: overlay may appear in screenshots, recordings or screen sharing. Exclusion is best-effort, not a guarantee.'),
            h('p', { 'data-overlay-controls-help': true, style: { ...muted, margin: 0 } }, 'Overlay is read-only; ask questions here. Interact: ⌃⌥⌘O.')),
          !data.capabilities?.overlay && h('p', { role: 'status', style: { ...muted, margin: 0 } }, backend === 'native' ? 'The floating overlay is macOS-only. Use this Meeting Copilot pane on Windows.' : 'Overlay helper unavailable. Check the macOS helper build and restart the gateway when other sessions are idle.'),
          captureMessage && h('p', { role: 'status', 'data-capture-message': true, style: muted }, captureMessage),
          audioAttention && h('p', { role: 'status', 'data-audio-attention': true, style: muted }, audioAttention),
          audioDiagnosticText(data.capture?.audio_diagnostics) && h('details', { className: 'mc-check-details', open: /zero-level|no samples/.test(audioDiagnosticText(data.capture.audio_diagnostics)) && !!data.listening },
            h('summary', { style: { cursor: 'pointer' } }, 'Audio signal'), h('div', { role: 'status', style: muted }, audioDiagnosticText(data.capture.audio_diagnostics))),
          data.capture && (data.capture.warning || lossText(data.capture)) && h('div', { role: 'status', style: muted },
            ['Transcript may be incomplete',
             lossText(data.capture),
             data.capture.warning ? `: ${data.capture.warning}` : ''].join(''))),

        errorCard(error, localRecheck ? 'Recheck status' : 'Dismiss', async () => {
          if (localRecheck) {
            // Resume GET-only progress checks; never replay an uncertain POST.
            setAssistFailed(false); setProgressPaused(false);
            setProgressRetry(value => value + 1);
            try {
              await qc.invalidateQueries({ queryKey: cacheKey('status') }, { throwOnError: true });
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

        !settingsAvailable && h('details', { className: 'mc-source-details', open: !backend },
          h('summary', { style: { cursor: 'pointer', ...muted } }, data.listening ? `Transcript source: ${data.backend || backend} — listening (pause to change settings)` : 'Transcript source settings'),
          h('span', { id: 'mc-source-label', style: { fontWeight: 700 } }, 'Transcript source'),
          h('div', { style: { display: 'grid', gap: 6, paddingTop: 8, paddingBottom: 10 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', { id: 'mc-backend-label' }, 'Capture source'),
            h('select', { value: backend, disabled: data.listening, onChange: e => changeCaptureInput('backend', e.target.value, setBackend), style: selectStyle, 'aria-labelledby': 'mc-backend-label' },
              h('option', { value: '', disabled: true }, 'Choose a capture source…'),
              h('option', { value: 'meetily' }, 'Meetily — use your existing recording'),
              h('option', { value: 'macos', disabled: !data.capabilities?.macos_transcription, title: data.capabilities?.macos_detail || '' }, 'macOS — without Meetily'),
              h('option', { value: 'native', disabled: data.capabilities?.windows_transcription !== true, title: data.capabilities?.windows_detail || 'Windows only · experimental' }, 'Windows — experimental')))),
          (backend === 'macos' || backend === 'native') && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Audio source'), h('select', { style: selectStyle, value: audioSource, disabled: data.listening, onChange: e => changeCaptureInput('audioSource', e.target.value, setAudioSource), 'aria-label': 'Audio source' },
              h('option', { value: 'microphone' }, 'Microphone'), h('option', { value: 'system' }, 'System audio'), h('option', { value: 'both' }, 'Microphone + system audio'))),
            backend === 'macos' && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Transcription provider'), h('select', { style: selectStyle, value: transcriptionProvider, disabled: data.listening, onChange: e => changeCaptureInput('transcriptionProvider', e.target.value, setTranscriptionProvider), 'aria-label': 'Transcription provider' },
              h('option', { value: 'local' }, 'Local — faster-whisper on this Mac'),
              h('option', { value: 'meta', disabled: !data.capabilities?.meta_asr, title: data.capabilities?.meta_asr ? '' : 'Set the Meta API key in Hermes Settings → Plugins → meeting-copilot-live, then reopen this pane.' }, 'Meta Muse Voice — cloud transcription'),
              h('option', { value: 'google', disabled: !data.capabilities?.google_asr }, 'Google Gemini — 60-second cloud preview'))),
            (backend === 'native' || transcriptionProvider === 'local') && h('label', { style: { display: 'grid', gap: 6 } }, h('span', null, 'Local transcription model'), h('select', { style: selectStyle, value: transcriptionModel, disabled: data.listening, onChange: e => changeCaptureInput('transcriptionModel', e.target.value, setTranscriptionModel), 'aria-label': 'Local transcription model' },
              h('option', { value: 'tiny.en' }, 'Whisper tiny.en — fastest'), h('option', { value: 'base.en' }, 'Whisper base.en — balanced'), h('option', { value: 'small.en' }, 'Whisper small.en — higher accuracy'))),
            backend === 'native' && h('span', { style: muted }, 'Local transcription uses the default microphone and all audio on the default output, according to Audio source. Prepare Whisper weights before Start. ', data.capabilities?.windows_detail || 'Experimental; hardware is unverified.'),
            backend === 'macos' && transcriptionProvider === 'local' && h('span', { style: muted }, 'Runs on this Mac without Meetily. Prepare the selected model before Start. Microphone or screen and system audio permission is required.'),
            backend === 'macos' && transcriptionProvider === 'google' && h('span', { style: muted }, 'Source labels only · no speaker diarization'),
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
        h('section', { className: 'mc-composer', ref: composerRef, style: { display: 'grid', gap: 9 }, 'aria-label': 'Answer composer' },
        h('label', { style: { display: 'grid', gap: 6 } }, h('span', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' } }, h('strong', { id: 'mc-q-label' }, 'What should I say?'),
          h('span', { id: `${paneEntry.a11yId}-answer-shortcut`, style: { ...muted, fontSize: 11 } }, '⌘/Ctrl+Enter to ask')),
          h(Textarea, { value: question, maxLength: 4000, onChange: e => setQuestion(e.target.value.slice(0, 4000)), placeholder: 'Ask a question, or leave blank for the latest…', title: 'Send with ⌘/Ctrl+Enter', rows: 2, 'aria-labelledby': 'mc-q-label', 'aria-keyshortcuts': 'Meta+Enter Control+Enter', 'aria-describedby': `${paneEntry.a11yId}-answer-shortcut`, onKeyDown: e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !e.repeat && !e.nativeEvent?.isComposing && !busy && !unsupported) { e.preventDefault(); assistNow(); } } })),
        h('details', { className: 'mc-quick-actions', 'aria-label': 'Recent meeting quick actions' },
          h('summary', { style: { cursor: 'pointer', ...muted } }, 'Quick actions'),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
            ...contextActions.map(([label, prompt]) => h(Button, { key: label, variant: 'secondary',
              disabled: !hasRecentTranscript || answerBlocked || unsupported,
              title: 'Sends an explicit request to your selected answer model using recent available context',
              onClick: () => { if (!hasRecentTranscript) return; return assistNow({}, prompt); } }, label))),
          h('p', { style: { ...muted, margin: 0 }, 'data-recent-context-disclosure': true },
            'Sends recent transcript and selected sources to your model, not the whole meeting. Review the result.'),
          !hasRecentTranscript && h('p', { style: { ...muted, margin: 0 }, role: 'status' },
            'Available when transcript text arrives.')),

        ...(unsupported ? [h('p', { role: 'alert', style: muted }, `Suggest is unavailable: ${unsupportedReasons.join(' ')} See ${settingsAvailable ? 'Meeting Copilot settings' : 'Answer setup below'}.`)] : []),
        h('div', { className: 'mc-chip', role: 'status', 'data-history-excluded': excludeHistory },
          excludeHistory ? (unsupported ? 'Past-session exclusion requested — resolve the warning.' : 'Selected project only · past sessions + web excluded')
            : `Search scope: ${project === '__all_projects__' ? 'all projects + sessions (may include unrelated projects)' : project ? `${project.split(/[\\/]/).pop()} + past sessions` : 'past sessions only'}${webFallback ? ' · automatic web on' : ' · automatic web off'}.`),
        h('div', { className: 'mc-answer-context', 'data-answer-sharing': true },
          h('span', { className: 'mc-chip' }, h(PaneIcon, { name: 'sparkle' }), answerProvider && answerModel ? `${answerProvider} / ${answerModel}` : 'Choose an answer model'),
          h('span', null, 'Your question, recent transcript and selected sources are sent to this model when you ask.')),
        webFallback && h('p', { 'data-web-fallback-disclosure': true, role: 'status', style: { ...muted, margin: 0 } }, 'Automatic web fallback is on: may send a public query. Filtering is not a privacy guarantee; turn off for sensitive calls.'),
        h('div', { className: 'mc-actions mc-answer-actions' },
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
              try { await checkedStatus(api, safety); } catch (_) {}
            }
          } }, 'Reset local answer tracking'),
          cancellableId && h(Button, { variant: 'secondary', onClick: async () => {
              try { await api('/assist/cancel', { request_id: cancellableId }); }
              catch (e) { setError('Cancellation was not confirmed. The answer may still be running; recheck status before asking again.'); setAssistFailed(true); return; }
              setTrackedRequest(null); setLiveEntry(null);
              try { await qc.invalidateQueries({ queryKey: cacheKey('status') }); }
              catch (e) { setError('Answer progress unavailable after cancellation. Recheck status before asking again.'); }
            } }, 'Cancel answer'),
          result?.answer && h(Button, { variant: 'secondary', onClick: async () => { if (!usable()) return; try { await writeClipboard(result.answer); if (usable()) { setCopied(true); setTimeout(() => { if (usable()) setCopied(false); }, 2000); } } catch (e) { if (usable()) setError('Clipboard unavailable. Select and copy the answer manually.'); } } }, copied ? 'Copied ✓' : 'Copy'),
          question && h(Button, { variant: 'secondary', disabled: busy, onClick: () => { setQuestion(''); composerRef.current?.querySelector('textarea')?.focus(); } }, 'Clear question'))),
        currentLive && ['error', 'cancelled'].includes(currentLive.status) && !data.error && h('section', { style: card, 'aria-live': 'polite', 'data-terminal-answer': true },
          h('p', null, currentLive.status === 'error' ? answerFailure() : 'Answer cancelled.'),
          currentLive.request && h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(currentLive.request) }, 'Retry this answer')),
        liveEntry && ['queued', 'streaming', 'running'].includes(liveEntry.status) && h('section', { className: 'mc-answer', style: card, 'aria-live': 'polite', ref: answerRef },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8 } },
            h('strong', null, liveEntry.status === 'queued' ? 'Queued — waiting for a worker…' : liveEntry.streaming === false ? 'Preparing answer…' : 'Streaming answer…'),
            h('span', { style: muted }, `${liveEntry.model_route || ''}${liveEntry.streaming === false ? ' · completed mode' : ''}`)),
          h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 8 } }, liveEntry.answer || 'Contacting your sources…'),
          ...((liveEntry.warnings || []).map((w, i) => h('p', { key: `lw-${i}`, style: muted }, w)))),
        result && h('section', { className: 'mc-answer', style: card, 'aria-live': 'polite', ref: answerRef, 'data-answer-result': true },
          h(Button, { variant: 'secondary', disabled: !notesCurrent() || !result.answer || noteItems.length >= 12 || noteItems.some(n => n.key === (result.request_id || result.answer)), onClick: pinAnswer }, 'Keep as note'),
          h(Button, { variant: 'secondary', disabled: !result.answer, onClick: async () => {
            if (!usable()) return;
            if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Copy this answer and its source excerpts (up to 2,050 characters per source)? The clipboard may include confidential project text or links. Review before sharing. No new model request is sent.')) return;
            try { await writeClipboard(evidenceReport(result)); if (usable()) notify('info', 'Answer and evidence copied. Review before sharing.'); }
            catch (_) { if (usable()) setError('Clipboard unavailable. Select and copy the answer and evidence manually.'); }
          } }, 'Copy answer + evidence'),
          h('div', { className: 'mc-answer-heading' }, h('strong', null, 'Suggested answer'), h('span', { style: muted }, `${Number.isFinite(result.elapsed_seconds) ? `${result.elapsed_seconds.toFixed(1)}s` : ''}${result.model_route ? ` · ${result.model_route}` : ''}`)),
          h('div', { className: 'mc-answer-text' }, result.answer),
          result.transcript_snapshot?.schema === 'transcript-snapshot-v1' && transcriptContext(result.transcript_snapshot),

          result.web_search?.used && h('p', { style: muted }, `${result.web_search.mode === 'automatic' ? 'Automatic web fallback' : 'Explicit web search'}: ${result.web_search.query}`),
          ...(result.warnings || []).map((w, i) => h('p', { key: `warning-${i}`, style: muted }, w)),
          result.answer && /partial|interrupt/i.test((result.warnings || []).join(' ')) && (result.request || lastAsked) && h('div', { style: { paddingTop: 8 } }, h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(result.request || { question: lastAsked.question, ...(lastAsked.scope || {}) }) }, 'Retry for the complete response')),
          h('details', { className: 'mc-evidence', style: { marginTop: 12 } }, h('summary', { style: { cursor: 'pointer', ...muted } }, `Evidence · ${(result.sources || []).length} sources`),
            h('p', { style: muted }, 'Excerpts are limited to 2,050 characters per source.'),
            ...(result.sources || []).map(evidenceItem))),
        !result && !liveEntry && !busy && h('section', { className: 'mc-empty', 'aria-label': 'Answer workspace' },
          h(PaneIcon, { name: 'sparkle' }), 'Answers & evidence appear here.'),
        h('details', { style: card, 'aria-label': 'Meeting review notes', open: noteItems.length > 0 },
          h('summary', { style: { cursor: 'pointer' } }, `Meeting notes · ${noteItems.length}/12`),
          h('p', { style: muted }, 'Keep an answer → edit it → check Reviewed → copy. Notes are not saved; copy before closing or starting a new meeting.'),
          h('details', null, h('summary', null, 'Privacy and review tips'), h('p', { style: muted }, 'Confirm owners and deadlines against evidence; leave unknowns unspecified. Notes stay only in this pane. Clear, New meeting, closing or reloading removes them. Copies you export remain outside Copilot.')),
          !noteItems.length && h('p', { style: muted }, 'After an answer, choose Keep as note. Pinning and editing make no model request.'),
          ...noteItems.map((note, i) => h('div', { key: note.key, style: { marginTop: 12 } },
            h('label', null, `Note ${i + 1}`, h('textarea', { 'aria-label': `Review note ${i + 1}`, value: note.text, maxLength: 12000, rows: 5,
              style: { width: '100%', boxSizing: 'border-box' },
              onChange: e => updateNotes(items => items.map(n => n.key === note.key ? { ...n, text: e.target.value.slice(0, 12000), reviewed: false } : n)) })),
            note.shortened && h('p', { style: muted }, 'Pinned answer shortened to 12,000 characters. Check the original before reviewing.'),
            h('label', null, h('input', { type: 'checkbox', checked: note.reviewed,
              onChange: e => updateNotes(items => items.map(n => n.key === note.key ? { ...n, reviewed: e.target.checked } : n)) }), ' Reviewed against evidence'),
            h(Button, { variant: 'secondary', onClick: () => updateNotes(items => items.filter(n => n.key !== note.key)) }, `Remove note ${i + 1}`),
            h('details', null, h('summary', null, 'Source evidence at pin time (up to 20,000 characters)'), h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, note.evidence)))),
          h(Button, { variant: 'secondary', disabled: !notesCurrent() || !noteItems.length || noteItems.some(n => !n.reviewed || !n.text.trim()), onClick: async () => {
            if (!notesCurrent() || !noteItems.length || noteItems.some(n => !n.reviewed || !n.text.trim())) return;
            if (typeof window === 'undefined' || typeof window.confirm !== 'function' || !window.confirm('Copy these reviewed notes? They may contain confidential meeting information. Clipboard history and sync may retain them. Source excerpts and transcript are not included. Nothing is sent automatically.')) return;
            if (!notesCurrent()) return;
            const output = ['MEETING COPILOT — REVIEWED NOTES', 'User-reviewed notes from selected AI suggestions; not a complete transcript or independent verification.',
              ...noteItems.map((n, i) => `\n${i + 1}. ${n.text}`)].join('\n');
            try { await writeClipboard(output); if (usable()) notify('info', 'Reviewed notes copied. Choose where to save or share them.'); }
            catch (_) { if (usable()) setError('Clipboard unavailable. Select and copy the notes manually.'); }
          } }, 'Copy reviewed notes')),
        h('details', { className: 'mc-transcript', open: showTranscript, onToggle: e => setShowTranscript(e.currentTarget.open) },
          h('summary', { style: { cursor: 'pointer', ...muted } }, `Live transcript · ${transcript.length} segments`),
          h('div', { style: { maxHeight: 280, overflowY: 'auto', marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 } },
            ...(transcript.length ? transcript.slice(-30).map((s, i) => {
              const order = [];
              for (const seg of transcript) { const n = String(seg.speaker || 'Speaker'); if (!order.includes(n)) order.push(n); }
              const color = speakerPalette[Math.max(0, order.indexOf(String(s.speaker || 'Speaker'))) % speakerPalette.length];
              return h('div', { className: 'mc-transcript-row', key: `${i}-${s.timestamp}` },
                h('i', { style: { background: color }, 'aria-hidden': true }),
                h('div', null, h('strong', { style: { color } }, s.speaker || 'Speaker'), h('p', null, s.text,
                  s.partial_tail && h('span', { title: 'Transcribed from trailing audio captured during shutdown' }, ' · trailing audio'))));
            })
              : [h('p', { key: 'empty', style: { ...muted, margin: 0 } }, 'No transcript yet.')]))),
        (Array.isArray(data.recent) && data.recent.length > 0) && h('details', { className: 'mc-history' }, h('summary', { style: { cursor: 'pointer', ...muted } }, `Recent answers · ${data.recent.length}`),
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
              entry.answer && h(Button, { variant: 'secondary', onClick: async () => { if (!usable()) return; try { await writeClipboard(entry.answer); } catch (e) { if (usable()) setError('Clipboard unavailable. Select and copy the answer manually.'); } } }, 'Copy this answer'),
              entry.request && h(Button, { variant: 'secondary', disabled: answerBlocked, onClick: () => replayRequest(entry.request) }, /partial|interrupt|unavailable|failed|error/i.test((entry.warnings || []).join(' ') + ' ' + (entry.status || '')) ? 'Retry for the complete response' : 'Ask again'))))),
        h('details', { 'data-answer-setup': true, open: setupOpen, onToggle: e => setSetupOpen(e.currentTarget.open), style: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 12, padding: '2px 14px 14px', background: 'var(--ui-bg-card)' } },
        h('summary', { style: { cursor: 'pointer', fontWeight: 700, color: 'var(--ui-text-primary)', padding: '12px 0', fontSize: 14 } }, settingsAvailable ? 'More actions' : 'Answer setup'),
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 14, paddingTop: 4 } },

        !settingsAvailable && h(AnswerPreferences, { prefs, status, projects, models: modelCatalog, qc, live: () => usable() && overlayActionAt.current.mounted }),
        h('section', { style: { ...card, display: 'grid', gap: 10 } },
          h('strong', null, 'Overlay scope'),
          scopeStale && overlayOn && h('p', { role: 'status', style: { ...muted, margin: 0 } }, 'Settings changed — syncing overlay scope…'),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
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
        h('details', null, h('summary', { style: { cursor: 'pointer', ...muted } }, 'Help'),
          h('p', { style: muted }, 'The read-only macOS overlay shows transcript and detected questions; ask in this pane. Press ⌃⌥⌘O (Control + Option + Command + O) to interact temporarily, drag the header, use position presets, or toggle Show in recordings. It returns to click-through automatically.'),
          h('p', { style: muted }, 'Closing the pane initiates pause or detach; check stop status before assuming audio is off. Pause may discard the last few seconds. Resume keeps this meeting; New meeting, different capture settings, or Clear starts fresh. No extra audio files are saved by Copilot.')));
    }
    // Keep the pane bound to the activation on which it mounted. An epoch may
    // advance before an asynchronous source swap finishes, so auto-remounting
    // immediately could bind the new epoch to the old socket. Require an
    // explicit close/reopen after the user has selected the destination.
    function RoutePane({ registration: entry }) {
      if (routeSupported) {
        sdk.useGatewayActivationEpoch();
        useValue(host.state.profile);
        // Activation may finish later, after the epoch notification. The
        // public connection atom also repaints a pane reopened mid-dial.
        useValue(host.state.connectionId);
      }
      const binding = currentBinding();
      if (!entry.binding && binding) {
        entry.binding = binding;
        if (!sameBinding(safetyBinding, binding)) { safety = newSafety(); safetyBinding = binding; }
        entry.safety = safety;
      }
      if (!entry.binding) return h('div', { role: 'alert', style: card },
        'This Desktop cannot safely identify the active gateway. Update Hermes Desktop before using Meeting Copilot.');
      if (!bindingCurrent(entry.binding)) return h('div', { role: 'alert', style: card },
        'Gateway changed. This pane is retired; close and reopen Meeting Copilot after the switch. Previous capture or overlay shutdown is not confirmed; verify the OS audio indicator and previous gateway.');
      return h(MeetingPane, { registration: entry });
    }
    const paneId = `${ID}:pane`;
    const visibility = typeof host.paneVisibility === 'function' ? host.paneVisibility(paneId) : null;
    const registerPane = () => {
      const entry = { active: true, life: null, a11yId: `${ID}-pane-${registration}-${++paneIds}` };
      paneRegistration = entry;
      const remove = ctx.register({ id: 'pane', area: 'panes', title: 'Meeting Copilot', data: { placement: 'right', width: '440px' },
        render: () => registered() && entry.active && paneRegistration === entry ? h(RoutePane, { registration: entry }) : null });
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
      const current = () => registered() && paneRegistration === entry && removePane === remove && entry.life === life && bindingCurrent(entry.binding);
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
