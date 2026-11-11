#!/usr/bin/env node
/** Static, fictional pane previews. No backend, devices, providers or user storage.
 * Usage: node scripts/render_pane_preview.mjs [output-directory]
 * Uses the regression harness rather than a second copy of the production UI.
 * SDK controls have neutral HTML stand-ins: these are source previews, not a
 * claim of pixel-identical rendering inside a particular Hermes release.
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const output = resolve(process.argv[2] || '/tmp/meeting-minimal-pane-preview');
const tests = readFileSync(resolve(root, 'tests/test_desktop.mjs'), 'utf8');
const section = (from, to) => {
  const start = tests.indexOf(from), end = tests.indexOf(to, start + from.length);
  if (start < 0 || end < 0) throw new Error('Pane harness boundaries changed; update the preview extractor.');
  return tests.slice(start, end);
};
const harness = section('function readyPreflight(', '\nasync function realBackend(')
  + section('function publicRoute(', '\n// Exercise the real uncompiled pane')
  + section('function pane(', "\ntest('route activation fences")
    .replace("new URL('../desktop/plugin.js', import.meta.url)", 'pluginPath');
const pane = vm.runInNewContext(harness + '\npane;', {
  readFileSync, vm, webcrypto, pluginPath: resolve(root, 'desktop/plugin.js'),
});

const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const unitless = new Set(['lineHeight', 'fontWeight', 'flexShrink', 'flexGrow', 'opacity', 'zIndex', 'order']);
const names = { className: 'class', htmlFor: 'for', tabIndex: 'tabindex', strokeWidth: 'stroke-width',
  strokeLinecap: 'stroke-linecap', strokeLinejoin: 'stroke-linejoin', fillRule: 'fill-rule', clipRule: 'clip-rule' };
const voidTags = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);
function html(node, selected) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node !== 'object') return escape(node);
  if (Array.isArray(node)) return node.map(child => html(child, selected)).join('');
  const props = node.props || {}, tag = node.type;
  if (typeof tag !== 'string') throw new Error('Unresolved component in preview');
  const attrs = Object.entries(props).flatMap(([key, value]) => {
    if (key.startsWith('on') || ['key', 'ref', 'variant', 'children'].includes(key) || value == null || value === false) return [];
    if (key === 'value' && ['textarea', 'select'].includes(tag)) return [];
    if (key === 'style') value = Object.entries(value).map(([name, item]) =>
      `${name.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase())}:${typeof item === 'number' && item !== 0 && !unitless.has(name) ? item + 'px' : item}`).join(';');
    return [`${names[key] || key}="${escape(value)}"`];
  });
  if (tag === 'option' && props.value === selected) attrs.push('selected');
  const content = tag === 'style' ? node.children.join('') : tag === 'textarea' ? escape(props.value || '')
    : (node.children || []).map(child => html(child, tag === 'select' ? props.value : selected)).join('');
  return `<${tag} ${attrs.join(' ')}>${voidTags.has(tag) ? '' : content + `</${tag}>`}`;
}
// Structural visibility only: CSS geometry and real browser rendering are not
// established by these checks. Closed details expose their summary, not body.
function disclosed(node) {
  if (node == null || typeof node === 'boolean') return [];
  if (typeof node !== 'object') return [node];
  if (Array.isArray(node)) return node.flatMap(disclosed);
  if (['style', 'svg'].includes(node.type) || node.props?.hidden || node.props?.['aria-hidden'] === true || String(node.props?.className || '').split(/\s+/).includes('mc-sr-only')) return [];
  const children = node.type === 'details' && !node.props?.open
    ? node.children.filter(child => child?.type === 'summary') : node.children;
  return [node, ...children.flatMap(disclosed)];
}
function allNodes(node) {
  if (!node || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(allNodes);
  return [node, ...(node.children || []).flatMap(allNodes)];
}
const review = [];
const caps = { all_projects: true, automatic_web_fallback: true, independent_models: true, history_opt_out: true,
  capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, overlay: true,
  overlay_stream: true, overlay_cancel: true };
const checks = [
  { id: 'consent', label: 'Participant permission', status: 'ready', detail: 'Permission confirmed for these settings.' },
  { id: 'microphone', label: 'Microphone permission', status: 'ready', detail: 'Microphone access is authorized.' },
  { id: 'runtime', label: 'Audio runtime', status: 'ready', detail: 'Required audio libraries load successfully.' },
];
const segments = [
  { speaker: 'you · Speaker A', text: 'The sample launch review is Monday.', timestamp: 1 },
  { speaker: 'remote · Speaker A', text: 'I will prepare the fictional checklist.', timestamp: 2 },
];
const answer = { answer: 'Confirm the sample checklist before Monday’s review. [1]', elapsed_seconds: 1.8,
  model_route: 'Hermes default', sources: [{ title: 'Fictional launch plan', source: 'sample-docs/launch.md',
    text: 'Prepare the sample checklist before the Monday review.' }] };
const scenarios = {
  ready: { status: {}, prefs: {}, preflight: { checks } },
  cloud: { status: { listening: true, backend: 'macos', segments, last_session: { backend: 'macos',
    transcription_provider: 'meta', audio_source: 'both', asr_mode: 'DIARIZATION' } },
    prefs: { transcriptionProvider: 'meta', asrMode: 'DIARIZATION' }, preflight: { checks } },
  blocked: { status: {}, prefs: {}, preflight: { ready: false, checks: [...checks.slice(0, 1),
    { id: 'microphone_permission', label: 'Microphone permission', status: 'blocked',
      detail: 'Allow Microphone access in System Settings → Privacy & Security, then recheck.' }] } },
  answer: { status: { segments, result: answer }, prefs: {}, preflight: { checks } },
};
mkdirSync(output, { recursive: true });
const files = [];
for (const width of [320, 440]) for (const theme of ['light', 'dark']) for (const [name, scenario] of Object.entries(scenarios)) {
  const prefs = { project: '/projects/alpha', backend: 'macos', transcriptionProvider: 'local',
    transcriptionModel: 'tiny.en', audioSource: 'both', asrMode: 'ENDPOINTING', transcriptionChoiceVersion: 1,
    answerProvider: 'hermes', answerModel: 'default', excludeHistory: true, webFallback: false, ...scenario.prefs };
  const ui = pane('/projects/alpha', true, false, '', { 3: true }, {}, { capabilities: caps, ...scenario.status }, {}, {
    settingsHost: true, noSeedOverrides: true, freshDefaults: true,
    storage: { get: () => prefs, set: () => { throw new Error('Preview must not save preferences'); } },
    providers: [{ id: 'hermes', configured: true, available: true, status: 'Host default available', models: [] }],
    preflight: scenario.preflight,
  });
  if (ui.requests.length) throw new Error('Static preview unexpectedly requested a backend');
  const visible = disclosed(ui.tree), elements = visible.filter(item => typeof item === 'object');
  const text = visible.filter(item => typeof item !== 'object').join(' ').replace(/\s+/g, ' ').trim();
  const nodes = allNodes(ui.tree);
  const start = elements.find(node => node.type === 'button' && node.children.includes('Start listening'));
  const passed = nodes.find(node => node.props?.['data-passed-checks']);
  const cloudDisclosure = elements.find(node => node.props?.['data-cloud-capture-disclosure']);
  if (name === 'ready') {
    assert.ok(passed && !passed.props.open, 'Passed checks must be collapsed');
    assert.ok(!text.includes('Microphone access is authorized.'), 'Ready check body leaked into visible prose');
    assert.ok(start && !start.props.disabled, 'Ready consented fixture should permit Start');
  }
  if (name === 'blocked') {
    assert.ok(text.includes('Needs setup: Microphone permission'));
    assert.ok(text.includes('Allow Microphone access in System Settings'));
    assert.ok(start?.props.disabled, 'Blocked fixture must disable Start');
  }
  if (name === 'cloud') {
    assert.ok(cloudDisclosure && text.includes('meeting audio is sent to Meta while listening'));
    assert.ok(text.includes('Meta (DIARIZATION)') && text.includes('Microphone + system audio'), 'Saved cloud capture choices must remain');
    const rail = elements.find(node => node.props?.['data-capture-rail']);
    const pause = allNodes(rail).find(node => node.type === 'button' && node.children.includes('Pause listening'));
    assert.ok(pause && !pause.props.disabled, 'Active Pause must remain in the status rail');
    const stylesheet = nodes.find(node => node.type === 'style').children.join('');
    assert.match(stylesheet, /\.mc-rail\s*\{[^}]*position:\s*sticky/);
    assert.ok(!elements.some(node => node.type === 'button' && node.children.includes('Start listening')));
  }
  assert.ok(text.includes('I have permission to capture/read this meeting'), 'Consent must remain disclosed');
  review.push({ state: name, theme, width, visibleWords: text.split(/\s+/).length, visibleCharacters: text.length,
    noBackendRequests: true, structuralChecks: 'passed', geometryVerified: false });

  const vars = theme === 'light'
    ? '--ui-text-primary:#202735;--ui-text-secondary:#5d687b;--ui-bg-card:#fff;--ui-stroke-secondary:#dfe4ec;--ui-accent:#5559ca;background:#f7f8fb'
    : '--ui-text-primary:#edf0f8;--ui-text-secondary:#aab5c7;--ui-bg-card:#1c2230;--ui-stroke-secondary:#394457;--ui-accent:#b1afff;background:#121824';
  const filename = `${name}-${theme}-${width}.html`;
  writeFileSync(resolve(output, filename), `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synthetic Copilot · ${name} · ${width}px</title><style>html{${vars};color:var(--ui-text-primary);font-family:system-ui}body{margin:0 auto;width:${width}px;max-width:100%;height:900px}button,input,textarea,select{font:inherit;color:inherit;background:var(--ui-bg-card);border:1px solid var(--ui-stroke-secondary);border-radius:7px;padding:8px;font-size:12px}button:disabled{opacity:.45}textarea{box-sizing:border-box;width:100%}input[type=checkbox]{width:15px;height:15px}p{margin:0 0 10px}button{cursor:pointer}badge{font-size:12px}*{box-sizing:border-box}</style>${html(ui.tree)}</html>`);
  files.push(filename);
}
writeFileSync(resolve(output, 'index.html'), '<!doctype html><html lang="en"><meta charset="utf-8"><title>Synthetic pane previews</title><h1>Synthetic pane previews</h1><p>Fictional data only. Static source preview; SDK controls are HTML stand-ins.</p><ul>' + files.map(file => `<li><a href="${file}">${file}</a></li>`).join('') + '</ul></html>');
writeFileSync(resolve(output, 'review.json'), JSON.stringify(review, null, 2));
writeFileSync(resolve(output, 'README.md'), `# Synthetic Meeting Copilot pane previews

Generated from the actual desktop/plugin.js render output and the repository's
mock pane harness. All transcript, project and answer text is fictional.
No backend, provider, device, live account or persisted preference is accessed.
Events are stripped; these HTML files cannot start capture or submit requests.

Regenerate: node scripts/render_pane_preview.mjs [output-directory]

Ready, cloud-active, blocked and answer states are included at 320/440 px in
light/dark. SDK controls are neutral HTML stand-ins, not pixel-identical Hermes
controls. review.json reports structural visibility, excluding style/SVG and
closed-details bodies. It does not verify CSS layout, overflow or accessibility
in a real browser. No screenshot or real-browser geometry validation is claimed.
Inspect an authorized Hermes pane separately for those checks.
`);
console.log(JSON.stringify({ output, files, review, synthetic: true, network: false, liveCapture: false }, null, 2));
