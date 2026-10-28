import { createElement as h, useState, useEffect } from 'react';
import { host, useValue, useQuery, useQueryClient, Button, Input, Textarea, Badge, PALETTE_AREA } from '@hermes/plugin-sdk';

const ID = 'meeting-copilot-live';
const muted = { color: 'var(--ui-text-secondary)', fontSize: 12, lineHeight: 1.5 };
const card = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 14, minWidth: 0, boxSizing: 'border-box', background: 'var(--ui-bg-card)' };
const selectStyle = { padding: 8, borderRadius: 6, width: '100%', maxWidth: '100%', minWidth: 0, color: 'var(--ui-text-primary)', background: 'var(--ui-surface-primary)', border: '1px solid var(--ui-stroke-secondary)' };

export default {
  id: ID,
  name: 'Meeting Copilot',
  description: 'Live answers grounded in your projects, prior sessions, and optional web research.',
  version: '0.2.0',
  register(ctx) {
    const api = (path, body) => ctx.rest(path, body === undefined ? {} : { method: 'POST', body });
    function MeetingPane() {
      const qc = useQueryClient();
      const [project, setProject] = useState('');
      const [question, setQuestion] = useState('');
      const [web, setWeb] = useState('');
      const [consent, setConsent] = useState(false);
      const [backend, setBackend] = useState('meetily');
      const [recording, setRecording] = useState('');
      const [error, setError] = useState('');
      const [pending, setPending] = useState(false);
      const [showTranscript, setShowTranscript] = useState(false);
      const [webFallback, setWebFallback] = useState(true);
      const [transcriptionModel, setTranscriptionModel] = useState('base.en');
      const [audioSource, setAudioSource] = useState('microphone');
      const [answerProvider, setAnswerProvider] = useState('');
      const [answerModel, setAnswerModel] = useState('');
      const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => api('/status'), refetchInterval: 2500, retry: 1 });
      const projects = useQuery({ queryKey: [ID, 'projects'], queryFn: () => api('/projects'), staleTime: 60000, refetchInterval: query => query.state.error ? 5000 : false, retry: 3 });
      const recordings = useQuery({ queryKey: [ID, 'recordings'], queryFn: () => api('/recordings'), refetchInterval: backend === 'meetily' ? 5000 : false, enabled: backend === 'meetily', retry: 1 });
      const data = status.data || {};
      const modelCatalog = useQuery({ queryKey: [ID, 'models'], queryFn: () => api('/models'), enabled: !!data.capabilities?.independent_models, staleTime: 60000, retry: 1 });
      const providers = modelCatalog.data?.providers || [];
      const answerModels = providers.find(p => p.id === answerProvider)?.models || [];
      const busy = pending || data.busy;
      const allProjects = project === '__all_projects__';
      const scope = { project: allProjects ? null : project || null, all_projects: allProjects, web_fallback: webFallback, ...(answerProvider && answerModel ? { answer_provider: answerProvider, answer_model: answerModel } : {}) };
      const unsupported = (allProjects && !data.capabilities?.all_projects) || (webFallback && !data.capabilities?.automatic_web_fallback) || (answerProvider && (!answerModel || !data.capabilities?.independent_models));
      useEffect(() => () => { api('/pause', {}).catch(() => {}); }, []);
      async function act(path, body = {}) {
        setPending(true); setError('');
        try { await api(path, body); await qc.invalidateQueries({ queryKey: [ID, 'status'] }); }
        catch (e) { setError(String(e.message || e)); }
        finally { setPending(false); }
      }
      const entries = Array.isArray(projects.data) ? projects.data : (projects.data?.projects || []);
      const result = data.result;
      const transcript = data.segments || [];
      return h('div', { style: { padding: 16, display: 'flex', flexDirection: 'column', gap: 14, height: '100%', minWidth: 0, width: '100%', boxSizing: 'border-box', overflowY: 'auto', overflowX: 'hidden', overflowWrap: 'anywhere', color: 'var(--ui-text-primary)' } },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } },
          h('strong', { style: { fontSize: 17 } }, 'Meeting Copilot'),
          h(Badge, null, data.listening ? 'Listening' : 'Paused')),
        h('p', { style: { ...muted, margin: 0 } }, 'Your projects and past sessions first. Automatic public web fallback is on by default when local evidence is insufficient.'),
        h('label', { style: { display: 'grid', gap: 6 } }, h('strong', null, 'Search scope'),
          h('select', { value: project, onChange: e => setProject(e.target.value), style: selectStyle },
            h('option', { value: '' }, 'Past sessions only'),
            h('option', { value: '__all_projects__', disabled: !data.capabilities?.all_projects }, 'All projects + sessions'),
            ...entries.map(p => { const value = typeof p === 'string' ? p : (p.path || p.name); return h('option', { value, key: value }, typeof p === 'string' ? p.split(/[\\/]/).pop() : (p.name || value)); }))),
        projects.error && h('div', { role: 'alert', style: muted }, `Project lookup unavailable: ${projects.error.message}`),
        allProjects && h('p', { style: { ...muted, margin: 0 } }, 'Searches supported text files across up to 100 eligible project folders plus past sessions, not your whole computer. Evidence can include unrelated projects; review it before sharing.'),
        unsupported && h('p', { role: 'alert', style: muted }, 'These options need the updated backend. Restart the gateway when other sessions are idle, then reopen this pane.'),
        h('section', { style: card },
          h('label', { style: { display: 'grid', gap: 6, paddingBottom: 10 } }, h('strong', null, 'Transcript source'),
            h('select', { value: backend, disabled: data.listening, onChange: e => setBackend(e.target.value), style: selectStyle },
              h('option', { value: 'meetily' }, 'Meetily — use your existing recording'),
              h('option', { value: 'macos', disabled: !data.capabilities?.macos_transcription }, 'Independent macOS transcription — no Meetily'),
              h('option', { value: 'native' }, 'Native Windows capture — experimental'))),
          backend === 'macos' && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('label', null, 'Audio source', h('select', { style: selectStyle, value: audioSource, disabled: data.listening, onChange: e => setAudioSource(e.target.value) },
              h('option', { value: 'microphone' }, 'Microphone'), h('option', { value: 'system' }, 'System audio'), h('option', { value: 'both' }, 'Microphone + system audio'))),
            h('label', null, 'Local transcription model', h('select', { style: selectStyle, value: transcriptionModel, disabled: data.listening, onChange: e => setTranscriptionModel(e.target.value) },
              h('option', { value: 'tiny.en' }, 'Whisper tiny.en — fastest'), h('option', { value: 'base.en' }, 'Whisper base.en — balanced'), h('option', { value: 'small.en' }, 'Whisper small.en — higher accuracy'))),
            h('span', { style: muted }, 'Runs locally in a separate audio worker. First use may download the selected model. macOS microphone / screen-and-system-audio permissions are required. No Meetily process or recording is needed.')),
          backend === 'meetily' && h('div', { style: { display: 'grid', gap: 8, paddingBottom: 12 } },
            h('span', { style: muted }, 'Start recording in Meetily, then choose the active meeting here. Only the explicitly selected transcript is read.'),
            h('select', { value: recording, disabled: data.listening, onChange: e => setRecording(e.target.value), style: selectStyle },
              h('option', { value: '' }, 'Choose an active Meetily recording…'),
              ...(Array.isArray(recordings.data) ? recordings.data : []).filter(r => r.status === 'recording').map(r => h('option', { value: r.id, key: r.id }, r.title || r.id))),
            recordings.error && h('span', { role: 'alert', style: muted }, `Meetily lookup unavailable: ${recordings.error.message}`),
            h('span', { style: muted }, 'Meetily manages its own saved recordings and transcripts. Pausing here detaches the copilot; it does not stop Meetily recording.')),
          h('label', { style: { ...muted, display: 'flex', gap: 8 } },
            h('input', { type: 'checkbox', checked: consent, onChange: e => setConsent(e.target.checked) }),
            'I have permission to capture/read this meeting and use its context with my configured model.'),
          h('div', { style: { display: 'flex', gap: 8, marginTop: 12 } },
            h(Button, { disabled: pending || (!data.listening && (!consent || (backend === 'meetily' && !recording) || (backend === 'macos' && !data.capabilities?.macos_transcription))), onClick: () => act(data.listening ? '/pause' : '/start', { consent, backend, recording_id: recording || null, transcription_model: transcriptionModel, audio_source: audioSource }) }, data.listening ? (data.backend === 'meetily' ? 'Detach' : 'Pause listening') : (backend === 'meetily' ? 'Attach to Meetily' : 'Start listening')),
            h(Button, { variant: 'secondary', disabled: pending, onClick: () => act('/clear') }, 'Clear meeting')),
          h('p', { style: { ...muted, marginBottom: 0 } }, 'No additional audio files · copilot detaches/pauses if this pane disconnects.'),
          data.capture && h('div', { style: muted }, data.capture.message || data.capture.state || '')),
        h('section', { style: card },
          h('strong', null, 'Answer model'),
          h('select', { style: selectStyle, value: answerProvider, disabled: busy || !data.capabilities?.independent_models, onChange: e => { setAnswerProvider(e.target.value); setAnswerModel(''); } },
            h('option', { value: '' }, providers.some(p => p.id === 'meta') ? 'Use Meta Muse Spark (default)' : 'Use Hermes configured model'), ...providers.map(p => h('option', { value: p.id, key: p.id }, p.label || p.id))),
          answerProvider && h('select', { style: selectStyle, value: answerModel, disabled: busy, onChange: e => setAnswerModel(e.target.value) },
            h('option', { value: '' }, 'Choose answer model…'), ...answerModels.map(m => h('option', { value: m.id, key: m.id }, m.label || m.id))),
          modelCatalog.error && h('p', { role: 'alert', style: muted }, 'Model catalog unavailable. Use the configured model or check provider setup.'),
          h('p', { style: muted }, 'Answer selection applies only to this copilot request; it does not change your chat model. Transcription remains local.')),
        h('label', { style: { display: 'grid', gap: 6 } }, h('strong', null, 'What should I say?'),
          h(Textarea, { value: question, onChange: e => setQuestion(e.target.value), placeholder: 'Leave blank to answer the latest question, or type a question…', rows: 3 })),
        h('div', { style: { display: 'flex', gap: 8 } },
          h(Button, { disabled: busy || unsupported, onClick: () => act('/assist', { question, ...scope }) }, busy ? 'Checking your sources…' : 'Suggest answer'),
          h(Button, { variant: 'secondary', disabled: !result?.answer, onClick: async () => { try { await navigator.clipboard.writeText(result.answer); } catch (e) { setError('Clipboard unavailable. Select and copy the answer manually.'); } } }, 'Copy')),
        h('label', { style: { ...muted, display: 'flex', gap: 8 } },
          h('input', { type: 'checkbox', checked: webFallback, onChange: e => setWebFallback(e.target.checked) }), 'Automatic web fallback'),
        h('p', { style: { ...muted, margin: 0 } }, 'If needed, the model drafts a generic public query. Filtering is best-effort, not a privacy guarantee; turn this off for sensitive calls. Selected context still goes to your configured answer model.'),
        h('details', null, h('summary', { style: { cursor: 'pointer', ...muted } }, 'Search online with a specific query'),
          h('div', { style: { display: 'grid', gap: 8, paddingTop: 10 } },
            h(Input, { value: web, onChange: e => setWeb(e.target.value), placeholder: 'Public query — no client names or meeting details' }),
            h('span', { style: muted }, 'This explicit query overrides automatic fallback for this request. Do not include private details.'),
            h(Button, { variant: 'secondary', disabled: busy || unsupported || !web.trim(), onClick: () => act('/assist', { question, ...scope, web_query: web.trim() }) }, 'Check sources + web'))),
        (error || data.error || status.error) && h('div', { role: 'alert', style: card }, error || data.error || `Backend unavailable: ${status.error?.message}. Check installation and plugin enablement.`),
        result && h('section', { style: card, 'aria-live': 'polite' },
          h('div', { style: { display: 'flex', justifyContent: 'space-between' } }, h('strong', null, 'Suggested answer'), h('span', { style: muted }, `${Number.isFinite(result.elapsed_seconds) ? `${result.elapsed_seconds.toFixed(1)}s` : ''}${result.model_route ? ` · ${result.model_route}` : ''}`)),
          h('div', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.65, paddingTop: 10 } }, result.answer),
          result.web_search?.used && h('p', { style: muted }, `${result.web_search.mode === 'automatic' ? 'Automatic web fallback' : 'Explicit web search'}: ${result.web_search.query}`),
          ...(result.warnings || []).map((w, i) => h('p', { key: `warning-${i}`, style: muted }, w)),
          h('details', { style: { marginTop: 12 } }, h('summary', { style: { cursor: 'pointer', ...muted } }, `Evidence · ${(result.sources || []).length} sources`),
            ...(result.sources || []).map((s, i) => h('div', { key: s.id || i, style: { ...muted, paddingTop: 8, overflowWrap: 'anywhere' } },
              h('strong', null, `[${i + 1}] ${s.title || s.source || 'Source'}`),
              /^https?:\/\//.test(s.source || '') ? h('a', { href: s.source, target: '_blank', rel: 'noopener noreferrer', style: { display: 'block', color: 'var(--ui-accent)' } }, s.source) : h('div', null, s.source),
              s.updated_at && h('div', null, `Updated: ${typeof s.updated_at === 'number' ? new Date(s.updated_at * 1000).toLocaleString() : s.updated_at}`),
              h('div', { style: { whiteSpace: 'pre-wrap' } }, (s.text || '').slice(0, 900)))))),
        h('details', { open: showTranscript, onToggle: e => setShowTranscript(e.currentTarget.open) },
          h('summary', { style: { cursor: 'pointer', ...muted } }, `Live transcript · ${transcript.length} segments`),
          h('div', { style: { maxHeight: 260, overflowY: 'auto', paddingTop: 8 } }, ...transcript.slice(-30).map((s, i) => h('p', { key: `${i}-${s.timestamp}`, style: muted }, h('strong', null, `${s.speaker}: `), s.text)))),
        h('p', { style: { ...muted, marginTop: 'auto' } }, 'Read-only assistance. Nothing is spoken, posted, or changed for you. Check suggestions before using them.'));
    }
    const paneId = `${ID}:pane`;
    const visibility = typeof host.paneVisibility === 'function' ? host.paneVisibility(paneId) : null;
    const registerPane = () => ctx.register({ id: 'pane', area: 'panes', title: 'Meeting Copilot', data: { placement: 'right', width: '440px' }, render: () => h(MeetingPane) });
    let removePane = registerPane();
    const openPane = () => {
      if (!removePane) removePane = registerPane();
      if (typeof host.revealPane === 'function') host.revealPane(paneId);
      else host.notify({ kind: 'error', message: 'Update Hermes Desktop to reopen contributed panes.' });
    };
    const togglePane = () => {
      // Read current host state on click, not a potentially stale render closure.
      if (visibility?.get() && typeof removePane === 'function') {
        removePane();
        removePane = null;
      } else openPane();
    };
    async function restartPlugin() {
      try { await api('/pause', {}); } catch (e) {}
      try { if (typeof removePane === 'function') removePane(); } catch (e) {}
      removePane = null;
      openPane();
      try { host.notify({ kind: 'info', message: 'Meeting Copilot restarted. Capture paused and pane reopened.' }); } catch (e) {}
    };
    function StatusButton() {
      const visible = visibility ? useValue(visibility) : false;
      const [menu, setMenu] = useState(null);
      const title = `${visible ? 'Hide' : 'Open'} Meeting Copilot`;
      const closeMenu = () => setMenu(null);
      return h('div', { style: { position: 'relative' } },
        h('button', { type: 'button', title, 'aria-label': title, 'aria-pressed': visible, className: 'px-1.5 text-[0.6875rem] text-(--ui-text-secondary) hover:text-(--ui-text-primary)', onClick: togglePane, onContextMenu: e => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }); } }, 'Meeting Copilot'),
        menu && h('div', { onClick: closeMenu, onContextMenu: e => { e.preventDefault(); closeMenu(); }, style: { position: 'fixed', inset: 0, zIndex: 9998 } },
          h('div', { role: 'menu', 'aria-label': 'Meeting Copilot options', onClick: e => e.stopPropagation(), style: { position: 'fixed', left: menu.x, top: menu.y, transform: 'translate(-100%, -100%)', zIndex: 9999, minWidth: 210, background: 'var(--ui-bg-elevated)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: 4, boxShadow: '0 8px 24px rgba(0,0,0,0.25)' } },
            h('button', { role: 'menuitem', type: 'button', onClick: () => { closeMenu(); restartPlugin(); }, style: { display: 'block', width: '100%', textAlign: 'left', padding: '8px 10px', borderRadius: 6, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--ui-text-primary)', fontSize: 12 } }, 'Restart plugin'),
            h('div', { style: { ...muted, padding: '6px 10px 4px', fontSize: 11 } }, 'Tip: ⌘K → Reload desktop plugins refreshes every plugin.'))));
    }
    ctx.register({ id: 'opener', area: 'statusBar.right', order: 130, render: () => h(StatusButton) });
    ctx.register({ id: 'open', area: PALETTE_AREA, data: { id: `${ID}.open`, label: 'Open Meeting Copilot', keywords: ['meeting', 'copilot', 'show', 'restore'], run: openPane } });
    ctx.register({ id: 'restart', area: PALETTE_AREA, data: { id: `${ID}.restart`, label: 'Restart Meeting Copilot', keywords: ['meeting', 'copilot', 'restart', 'reload'], run: restartPlugin } });
    ctx.register({ id: 'meeting-help', area: PALETTE_AREA, data: { label: 'Meeting Copilot: usage', run: () => host.notify({ kind: 'info', message: 'Open Meeting Copilot. Choose one project, All projects + sessions, or sessions only. Web fallback is on by default and can be disabled. Confirm consent and attach your recording, then Suggest answer.' }) } });
    return () => { api('/pause', {}).catch(() => {}); };
  },
};
