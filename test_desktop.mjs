import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

test('capture recovery confirms changed source before destructive restart', async () => {
  for (const accept of [false, true]) {
    const confirmations = [];
    const { tree, requests } = pane('', true, false, '',
      { 3: true, 4: 'macos', 11: 'system' }, {},
      { error: 'capture failed', result: { answer: 'Retained answer', sources: [] },
        last_session: { backend: 'macos', transcription_model: 'base.en', audio_source: 'microphone' } },
      { window: { confirm: message => { confirmations.push(message); return accept; } } });
    const recovery = nodes(tree).find(n => n.type === 'button' && n.children.includes('Restart capture'));
    assert.ok(recovery);
    await recovery.props.onClick();
    assert.equal(confirmations.length, 1, 'recovery shares Resume confirmation');
    assert.equal(requests.filter(r => r.path === '/start').length, accept ? 1 : 0);
  }
});

test('live capture error offers a status recheck, not a false restart label', async () => {
  const { tree, requests } = pane('', true, false, '', {}, {},
    { listening: true, backend: 'meetily', error: 'snapshot_unavailable' });
  const recheck = nodes(tree).find(n => n.type === 'button' && n.children.includes('Recheck status'));
  assert.ok(recheck);
  assert.ok(!nodes(tree).some(n => n.type === 'button' && n.children.includes('Restart capture')));
  await recheck.props.onClick();
  assert.ok(!requests.some(r => r.path === '/start' || r.path === '/assist'));
});

// Exercise the real uncompiled pane with lightweight React/SDK stand-ins.
// This checks rendering/request contracts, not visual acceptance.
function pane(scope, supported = true, fallback = true, web = '', settings = {}, respond = {}, statusExtra = {}, browserGlobals = {}, qcBehavior = {}) {
  let registered;
  let stateIndex = 0;
  const values = [];
  const requests = [];
  const contributions = [];
  const revealed = [];
  let visible = false;
  const visibility = { get: () => visible };
  const ctx = {
    rest: async (path, options = {}) => {
      requests.push({ path, ...options });
      const r = respond[path];
      return typeof r === 'function' ? r(path, options) : (r || {});
    },
    register: item => {
      contributions.push(item);
      if (item.area === 'panes') registered = item;
      return () => { contributions.splice(contributions.indexOf(item), 1); if (item.area === 'panes') visible = false; };
    },
  };
  const h = (type, props, ...children) => typeof type === 'function'
    ? type(props || {}) : { type, props: props || {}, children: children.flat(Infinity) };
  const source = readFileSync(new URL('./desktop/plugin.js', import.meta.url), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export default', 'const plugin =');
  vm.runInNewContext(source + '\nplugin.register(ctx);', {
    ctx, h,
    useState: initial => {
      const i = stateIndex++;
      if (!(i in values)) {
        const overrides = { 0: scope, 2: web, ...settings };
        values[i] = Object.hasOwn(overrides, i) ? overrides[i] : initial === true ? fallback : initial;
      }
      return [values[i], v => { values[i] = typeof v === 'function' ? v(values[i]) : v; }];
    },
    useRef: initial => {
      const i = stateIndex++;
      if (!(i in values)) values[i] = { current: initial };
      return values[i];
    },
    useEffect: () => {},
    useQueryClient: () => ({ invalidateQueries: qcBehavior.failInvalidate ? async () => { throw new Error('invalidate failed'); } : async () => {} }),
    useQuery: options => ({ data: options.queryKey[1] === 'status'
      ? { capabilities: supported ? { all_projects: true, automatic_web_fallback: true, independent_models: true, macos_transcription: true, overlay: true, overlay_stream: true, overlay_cancel: true } : undefined, ...statusExtra }
      : options.queryKey[1] === 'projects' ? [{ name: 'alpha', path: '/projects/alpha' }]
      : options.queryKey[1] === 'models' ? { providers: [{ id: 'openai-codex', label: 'Codex', models: [{ id: 'gpt-test', label: 'Test model' }] }] } : [] }),
    useValue: atom => atom.get(),
    host: { paneVisibility: () => visibility, revealPane: id => { revealed.push(id); visible = true; } }, Button: 'button', Input: 'input', Textarea: 'textarea', Badge: 'badge', PALETTE_AREA: 'palette',
    ...browserGlobals,
  });
  const render = () => { stateIndex = 0; return registered.render(); };
  return { tree: render(), rerender: render, requests, contributions, revealed };
}
function nodes(tree) {
  return tree && typeof tree === 'object' ? [tree, ...(tree.children || []).flatMap(nodes)] : [];
}

test('native opener restores the namespaced pane without capture requests', () => {
  const { contributions, revealed, requests } = pane('');
  const open = contributions.find(c => c.area === 'palette' && c.data.label === 'Open Meeting Copilot');
  assert.ok(open, 'A real open command must be discoverable in Cmd+K');
  open.data.run();
  assert.deepEqual(revealed, ['meeting-copilot-live:pane']);
  assert.equal(requests.length, 0);
});

test('status bar reopens the pane without starting capture', () => {
  const { contributions, revealed, requests } = pane('');
  const entry = contributions.find(c => c.area === 'statusBar.right');
  assert.ok(entry, 'An always-available status bar opener must exist');
  const button = nodes(entry.render()).find(n => n.type === 'button');
  assert.equal(button.props.title, 'Open Meeting Copilot');
  assert.equal(typeof button.props.onContextMenu, 'function', 'Right-click must offer the restart menu');
  button.props.onClick();
  assert.deepEqual(revealed, ['meeting-copilot-live:pane']);
  assert.equal(requests.length, 0);
});

test('status bar toggles open, hide, and reopen without capture changes', () => {
  const { contributions, requests } = pane('');
  const entry = contributions.find(c => c.area === 'statusBar.right');
  const first = nodes(entry.render()).find(n => n.type === 'button');
  first.props.onClick();
  const shown = nodes(entry.render()).find(n => n.type === 'button');
  assert.equal(shown.props.title, 'Hide Meeting Copilot');
  assert.equal(shown.props['aria-pressed'], true);
  shown.props.onClick();
  assert.equal(contributions.filter(c => c.area === 'panes').length, 0);
  assert.equal(nodes(entry.render()).find(n => n.type === 'button').props.title, 'Open Meeting Copilot');
  nodes(entry.render()).find(n => n.type === 'button').props.onClick();
  assert.equal(contributions.filter(c => c.area === 'panes').length, 1);
  assert.equal(nodes(entry.render()).find(n => n.type === 'button').props['aria-pressed'], true);
  assert.equal(requests.length, 0);
});

test('restart command pauses capture and reopens exactly one pane', async () => {
  const { contributions, revealed, requests } = pane('');
  const restart = contributions.find(c => c.area === 'palette' && c.data.label === 'Restart Meeting Copilot');
  assert.ok(restart, 'A restart command must be discoverable in Cmd+K');
  await restart.data.run();
  assert.ok(requests.some(r => r.path === '/pause'), 'Restart must pause capture first');
  assert.equal(contributions.filter(c => c.area === 'panes').length, 1, 'Restart must leave exactly one pane registration');
  assert.deepEqual(revealed, ['meeting-copilot-live:pane']);
});

test('independent transcription sends explicit local model/source and consent', async () => {
  const { tree, requests } = pane('', true, false, '', { 3: true, 4: 'macos', 10: 'tiny.en', 11: 'microphone' });
  const local = nodes(tree).find(n => n.type === 'option' && n.props.value === 'macos');
  assert.ok(local, 'Independent macOS capture must be selectable');
  const start = nodes(tree).find(n => n.type === 'button' && n.children.includes('Start listening'));
  await start.props.onClick();
  const body = requests.find(r => r.path === '/start').body;
  assert.equal(body.backend, 'macos');
  assert.equal(body.transcription_model, 'tiny.en');
  assert.equal(body.audio_source, 'microphone');
  assert.equal(body.consent, true);
});

test('answer model selection is explicit and separate from transcription', async () => {
  const { tree, requests } = pane('', true, false, '', { 12: 'openai-codex', 13: 'gpt-test' });
  assert.ok(nodes(tree).find(n => n.type === 'option' && n.props.value === 'gpt-test'));
  await nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer')).props.onClick();
  const body = requests.find(r => r.path === '/assist').body;
  assert.equal(body.answer_provider, 'openai-codex');
  assert.equal(body.answer_model, 'gpt-test');
});

test('all projects option sends an explicit scope without a selected project', async () => {
  const { tree, requests } = pane('__all_projects__');
  const all = nodes(tree).find(n => n.type === 'option' && n.props.value === '__all_projects__');
  assert.ok(all, 'All projects option must be rendered');
  assert.equal(all.props.disabled, false);
  assert.ok(all.children.includes('All projects + sessions'));
  const suggest = nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer'));
  await suggest.props.onClick();
  const request = requests.find(r => r.path === '/assist');
  assert.equal(request.body.all_projects, true);
  assert.equal(request.body.project, null);
  assert.equal(request.body.web_query, undefined);
  assert.equal(request.body.web_fallback, false);
});

test('older backend cannot silently accept an unsupported all-project selection', () => {
  const { tree } = pane('__all_projects__', false);
  const all = nodes(tree).find(n => n.type === 'option' && n.props.value === '__all_projects__');
  assert.ok(all, 'All projects option must be rendered');
  assert.equal(all.props.disabled, true);
  const suggest = nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer'));
  assert.equal(suggest.props.disabled, true);
});

test('specific project and sessions-only requests retain their narrow scopes', async () => {
  for (const scope of ['/projects/alpha', '']) {
    const { tree, requests } = pane(scope);
    await nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer')).props.onClick();
    const request = requests.find(r => r.path === '/assist');
    assert.equal(request.body.all_projects, false);
    assert.equal(request.body.project, scope || null);
  }
});

test('automatic web fallback can be disabled and explicit web query remains separate', async () => {
  const { tree, requests } = pane('', true, false, 'public product documentation');
  const buttons = nodes(tree).filter(n => n.type === 'button');
  await buttons.find(n => n.children.includes('Suggest answer')).props.onClick();
  assert.equal(requests.at(-1).body.web_fallback, false);
  assert.equal(requests.at(-1).body.web_query, undefined);
  await buttons.find(n => n.children.includes('Check sources + web')).props.onClick();
  assert.equal(requests.at(-1).body.web_query, 'public product documentation');
  assert.equal(requests.at(-1).body.web_fallback, false);
});

test('overlay section shows explicitly, syncs current scope, and warns about screen share', async () => {
  const { tree } = pane('alpha');
  const text = JSON.stringify(tree);
  assert.match(text, /Meeting overlay/);
  assert.match(text, /Show overlay/);
  assert.match(text, /may be visible in screen shares/);
  const buttons = nodes(tree).filter(n => n.type === 'button');
  const show = buttons.find(n => (Array.isArray(n.children) ? n.children.join('') : String(n.children || '')) === 'Show overlay');
  assert.ok(show, 'explicit Show overlay action must exist');
  assert.equal(show.props.disabled, false);
  await show.props.onClick();
});

test('overlay scope sync posts current pane settings and nothing else starts capture', async () => {
  const { tree, requests } = pane('alpha');
  const buttons = nodes(tree).filter(n => n.type === 'button');
  const show = buttons.find(n => (Array.isArray(n.children) ? n.children.join('') : String(n.children || '')) === 'Show overlay');
  const sync = buttons.find(n => (Array.isArray(n.children) ? n.children.join('') : String(n.children || '')) === 'Sync scope');
  assert.ok(show && sync, 'Show and Sync scope actions must exist');
  await show.props.onClick();
  await sync.props.onClick();
  const overlayCalls = requests.filter(r => r.path.startsWith('/overlay/'));
  assert.ok(overlayCalls.length >= 1);
  assert.ok(overlayCalls.every(r => r.path.startsWith('/overlay/')));
  assert.ok(requests.every(r => r.path !== '/start' && r.path !== '/assist'));
  const shown = overlayCalls.find(r => r.path === '/overlay/show');
  assert.ok(shown && JSON.stringify(shown.body || shown).includes('alpha'));
});

test('overlay button is disabled when the backend lacks overlay capability', () => {
  const { tree } = pane('alpha', false);
  const buttons = nodes(tree).filter(n => n.type === 'button');
  const show = buttons.find(n => (Array.isArray(n.children) ? n.children.join('') : String(n.children || '')) === 'Show overlay');
  assert.ok(show, 'Show overlay action must render even when unsupported');
  assert.equal(show.props.disabled, true);
});

test('pane uses only approved theme vars and no resurrected scope controls', () => {
  const source = readFileSync(new URL('./desktop/plugin.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /--ui-surface-/);
  assert.doesNotMatch(source, /\/overlay-bridge|--base-url|UrllibTransport/);
  assert.match(source, /\/overlay\/show/);
  assert.match(source, /\/overlay\/hide/);
  assert.match(source, /\/overlay\/scope/);
});

test('pane imports every React hook it calls (no undeclared globals)', () => {
  const src = readFileSync(new URL('./desktop/plugin.js', import.meta.url), 'utf8');
  const importLine = src.split('\n').find(l => l.startsWith('import {') && l.includes('react'));
  assert.ok(importLine, 'expected a react import line');
  const imported = new Set(importLine.replace(/^import \{/, '').replace(/\}.*$/, '').split(',').map(s => s.trim()));
  const called = new Set([...src.matchAll(/(?<![A-Za-z0-9_.])use[A-Z]\w*(?=\()/g)].map(m => m[0]));
  const provided = new Set(['useState', 'useRef', 'useEffect', 'useQueryClient', 'useQuery', 'useValue']);
  for (const hook of called) {
    assert.ok(imported.has(hook) || provided.has(hook), `${hook} must be imported or harness-provided`);
    if (hook.startsWith('use') && !['useState', 'useRef', 'useEffect', 'useQueryClient', 'useQuery', 'useValue'].includes(hook)) {
      assert.ok(imported.has(hook), `${hook} is not a known harness global and must be imported from react`);
    }
  }
  assert.ok(imported.has('useRef'), 'useRef must be imported from react');
});

test('rejected scope sync keeps the stale warning actionable', async () => {
  const { rerender } = pane('alpha', true, false, '', {}, { '/overlay/scope': { accepted: false } });
  const findButton = (tree, label) => nodes(tree).filter(n => n.type === 'button')
    .find(n => (Array.isArray(n.children) ? n.children.join('') : String(n.children || '')) === label);
  let tree = rerender();
  await findButton(tree, 'Show overlay').props.onClick();
  tree = rerender();
  await findButton(tree, 'Sync scope').props.onClick();
  tree = rerender();
  assert.ok(JSON.stringify(tree).includes('stale sync was ignored'), 'rejection must stay visible, not clear the stale state');
});

test('accepted scope sync clears the stale indicator', async () => {
  const { rerender } = pane('alpha', true, false, '', {}, { '/overlay/scope': { accepted: true } });
  const findButton = (tree, label) => nodes(tree).filter(n => n.type === 'button')
    .find(n => (Array.isArray(n.children) ? n.children.join('') : String(n.children || '')) === label);
  let tree = rerender();
  await findButton(tree, 'Show overlay').props.onClick();
  tree = rerender();
  await findButton(tree, 'Sync scope').props.onClick();
  tree = rerender();
  assert.ok(JSON.stringify(tree).includes('Overlay scope updated to current settings'));
});

test('suggesting an answer tracks the request and offers cancel', async () => {
  const { tree, rerender, requests } = pane('', true, false, '', {}, { '/assist': { accepted: true, request_id: 'r-9' } });
  await nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer')).props.onClick();
  assert.equal(requests.find(r => r.path === '/assist').body.question, '');
  const retree = rerender();
  const cancel = nodes(retree).find(n => n.type === 'button' && n.children.includes('Cancel answer'));
  assert.ok(cancel, 'Cancel answer must appear once a request is tracked');
  await cancel.props.onClick();
  const cancelReq = requests.find(r => r.path === '/assist/cancel');
  assert.equal(cancelReq.body.request_id, 'r-9');
});

test('live partials render while a request streams', async () => {
  const { tree } = pane('', true, false, '', { 21: 'r-1', 22: { status: 'streaming', answer: 'partial answer…' } }, {});
  const section = nodes(tree).find(n => n.type === 'strong' && n.children.includes('Streaming answer…'));
  assert.ok(section, 'streaming status must render while a request is live');
  assert.ok(nodes(tree).some(n => n.children && n.children.includes('partial answer…')));
});

test('completed-mode work is labeled preparing, not streaming', async () => {
  const { tree } = pane('', true, false, '', { 21: 'r-1', 22: { status: 'running', streaming: false, answer: '' } }, {}, {});
  assert.ok(nodes(tree).find(n => n.type === 'strong' && n.children.includes('Preparing answer…')));
});

test('reopened pane reattaches cancel to the live request from status', async () => {
  const { tree } = pane('', true, false, '', {}, {}, { requests: { 'r-7': { request_id: 'r-7', status: 'streaming' } } });
  assert.ok(nodes(tree).find(n => n.type === 'button' && n.children.includes('Cancel answer')));
});

test('errors surface at the top with a dismiss action', async () => {
  const { tree } = pane('', true, false, '', { 6: 'boom' }, {}, {});
  const alerts = nodes(tree).filter(n => n.props && n.props.role === 'alert');
  assert.ok(alerts.length > 0);
  const first = nodes(tree).findIndex(n => n.props && n.props.role === 'alert');
  const question = nodes(tree).findIndex(n => n.type === 'strong' && n.children.includes('What should I say?'));
  assert.ok(first < question, 'error must precede the question controls');
  assert.ok(nodes(tree).some(n => n.type === 'button' && n.children.includes('Dismiss')));
});

test('advanced setup is progressively disclosed below the answers', async () => {
  const { tree } = pane('', true, false, '', {}, {}, {});
  const setup = nodes(tree).findIndex(n => n.type === 'summary' && n.children.some(c => typeof c === 'string' && c.includes('Answer setup')));
  const question = nodes(tree).findIndex(n => n.type === 'strong' && n.children.includes('What should I say?'));
  assert.ok(setup > question, 'setup disclosure must follow the question and answers');
});

test('listening compresses the source card into a disclosure', async () => {
  const { tree } = pane('', true, false, '', {}, {}, { listening: true, backend: 'macos' });
  const summaries = nodes(tree).filter(n => n.type === 'summary');
  const compressed = summaries.some(n => n.children.some(c => typeof c === 'string' && c.includes('pause to change settings')));
  assert.ok(compressed, 'listening compresses the source card');
  const { tree: idle } = pane('', true, false, '', {}, {}, {});
  const idleOpen = nodes(idle).filter(n => n.type === 'summary').some(n => n.children.some(c => typeof c === 'string' && c.includes('pause to change settings')));
  assert.ok(!idleOpen, 'idle pane keeps the full source card');
});

test('unsupported setup names the reason beside the answer action', async () => {
  const { tree } = pane('__all_projects__', false, false, '', {}, {}, {});
  const alert = nodes(tree).find(n => n.props && n.props.role === 'alert' && n.children.some(c => typeof c === 'string' && c.includes('Suggest is unavailable')));
  assert.ok(alert);
  const buttons = nodes(tree).filter(n => n.type === 'button');
  assert.ok(buttons.some(n => n.children.includes('Suggest answer') && n.props.disabled));
});

test('backend errors offer retry, local errors offer dismiss', async () => {
  const { tree: local } = pane('', true, false, '', { 6: 'boom' }, {}, {});
  assert.ok(nodes(local).some(n => n.type === 'button' && n.children.includes('Dismiss')));
  const { tree: remote } = pane('', true, false, '', {}, {}, { error: 'capture failed' });
  assert.ok(nodes(remote).some(n => n.type === 'button' && n.children.includes('Restart capture')));
});

test('listening bar keeps pause and loss visible', async () => {
  const { tree } = pane('', true, false, '', {}, {}, { listening: true, backend: 'macos' });
  assert.ok(nodes(tree).some(n => n.type === 'button' && n.children.includes('Pause listening')));
});

test('listening bar surfaces drop counts', async () => {
  const { tree } = pane('', true, false, '', {}, {}, { listening: true, backend: 'macos', capture: { dropped_chunks: 3, native_dropped_chunks: 2, worker_dropped_chunks: 1, warning: 'drops happened' } });
  assert.ok(nodes(tree).some(n => n.children && n.children.some(c => typeof c === 'string' && c.includes('1 worker chunk(s) + 2 native frame(s)'))));
});

test('retry re-asks the failed question', async () => {
  const { tree, requests } = pane('edited', true, false, '', { 6: 'boom', 23: { question: 'original?', extra: {} }, 24: true }, {}, {});
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry'));
  assert.ok(retry, 'a failed answer offers Retry');
  await retry.props.onClick();
  const last = requests.filter(r => r.path === '/assist').at(-1);
  assert.equal(last.body.question, 'original?');
});

test('dismiss never sends anything', async () => {
  const { tree, requests } = pane('', true, false, '', { 6: 'boom' }, {}, {});
  const dismiss = nodes(tree).find(n => n.type === 'button' && n.children.includes('Dismiss'));
  assert.ok(dismiss, 'local errors offer Dismiss');
  await dismiss.props.onClick();
  assert.ok(!requests.some(r => r.path === '/assist'), 'dismiss must not send');
});

test('capture errors offer an honest capture restart instead of re-asking', async () => {
  const { tree, requests } = pane('edited', true, false, '', { 23: { question: 'original?', extra: {} }, 24: true }, {}, { error: 'capture failed' });
  const retry = nodes(tree).find(n => n.type === 'button' && (n.children.includes('Restart capture') || n.children.includes('Retry')));
  assert.ok(retry, 'capture errors offer recovery');
  await retry.props.onClick();
  assert.ok(!requests.some(r => r.path === '/assist'), 'capture recovery must not re-ask an answer');
  assert.ok(requests.some(r => r.path === '/start'), 'capture recovery restarts capture');
});

test('partial answers carry a retry for the complete response', async () => {
  const statusExtra = { result: { answer: 'half an answer', warnings: ['Stream interrupted; showing a partial answer. Retry for the complete response.'], sources: [], elapsed_seconds: 1.2, model_route: 'x', web_search: { used: false } } };
  const { tree, requests } = pane('', true, false, '', { 23: { question: 'q?', extra: {} } }, {}, statusExtra);
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry for the complete response'));
  assert.ok(retry);
  await retry.props.onClick();
  const last = requests.filter(r => r.path === '/assist').at(-1);
  assert.equal(last.body.question, 'q?');
});

test('async failure retry replays the stored original request', async () => {
  const stored = { question: 'orig q?', project: '/p', all_projects: false, web_fallback: false, transcript: 'you: hi' };
  const { tree, requests } = pane('', true, false, '', {}, {}, { error_request: stored, error: 'RuntimeError: down' });
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry'));
  assert.ok(retry);
  await retry.props.onClick();
  const last = requests.filter(r => r.path === '/assist').at(-1);
  assert.equal(last.body.question, 'orig q?');
  assert.equal(last.body.project, '/p');
  assert.equal(last.body.transcript, 'you: hi');
});

test('partial retry prefers the result request over pane state', async () => {
  const statusExtra = { result: { answer: 'half', warnings: ['Stream interrupted; retry.'], sources: [], elapsed_seconds: 1, model_route: 'x', web_search: { used: false },
    request: { question: 'stored q?', project: null, all_projects: false, web_fallback: false, transcript: 'remote: hey' } } };
  const { tree, requests } = pane('different?', true, false, '', {}, {}, statusExtra);
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry for the complete response'));
  assert.ok(retry);
  await retry.props.onClick();
  const last = requests.filter(r => r.path === '/assist').at(-1);
  assert.equal(last.body.question, 'stored q?');
  assert.equal(last.body.transcript, 'remote: hey');
});

test('empty stored transcripts replay verbatim, not current segments', async () => {
  const stored = { question: 'q?', project: null, all_projects: false, web_fallback: false, transcript: '' };
  const { tree, requests } = pane('changed?', true, false, '', {}, {}, { result: { answer: 'half', warnings: ['Stream interrupted; retry.'], sources: [], elapsed_seconds: 1, model_route: 'x', web_search: { used: false }, request: stored } });
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry for the complete response'));
  assert.ok(retry);
  await retry.props.onClick();
  const last = requests.filter(r => r.path === '/assist').at(-1);
  assert.equal(last.body.question, 'q?');
  assert.ok('transcript' in last.body, 'empty transcript must be sent explicitly');
  assert.equal(last.body.transcript, '');
});

test('recent entries retry their own stored request', async () => {
  const statusExtra = { recent: [{ request_id: 'r-old', question: 'old q?', answer: 'half old', status: 'done', warnings: ['Stream interrupted; retry.'], sources: [], request: { question: 'old q?', project: '/p', all_projects: false, web_fallback: false, transcript: 'you: old' } }] };
  const { tree, requests } = pane('', true, false, '', {}, {}, statusExtra);
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry for the complete response'));
  assert.ok(retry);
  await retry.props.onClick();
  const last = requests.filter(r => r.path === '/assist').at(-1);
  assert.equal(last.body.question, 'old q?');
  assert.equal(last.body.transcript, 'you: old');
});

test('stored backend errors pair text with retry', async () => {
  const stored = { question: 'orig q?', project: null, all_projects: false, web_fallback: false, transcript: 'you: hi' };
  const { tree } = pane('', true, false, '', { 6: 'local hiccup' }, {}, { error: 'RuntimeError: down', error_request: stored });
  assert.ok(nodes(tree).some(n => n.type === 'button' && n.children.includes('Retry')));
});

test('idle pane keeps a prominent start action above the fold', async () => {
  const { tree } = pane('', true, false, '', {}, {}, {});
  assert.ok(nodes(tree).some(n => n.type === 'button' && (n.children.includes('Attach to Meetily') || n.children.includes('Start listening'))));
});

test('separate loss units render without a misleading total', async () => {
  const { tree } = pane('', true, false, '', {}, {}, { listening: true, backend: 'macos', capture: { dropped_chunks: 3, native_dropped_chunks: 2, worker_dropped_chunks: 1 } });
  assert.ok(nodes(tree).some(n => n.children && n.children.some(c => typeof c === 'string' && c.includes('1 worker chunk(s) + 2 native frame(s)'))));
});

test('failed replay stays retryable as itself', async () => {
  let calls = 0;
  const failOnce = () => { calls++; if (calls === 1) throw new Error('down'); return { accepted: true, request_id: 'r-2' }; };
  const req = { question: 'old q?', project: null, all_projects: false, web_fallback: false };
  const statusExtra = { recent: [{ request_id: 'r-1', question: 'old q?', answer: 'half', status: 'done', warnings: ['Stream interrupted; retry.'], sources: [], request: req }] };
  const { tree, rerender, requests } = pane('', true, false, '', {}, { '/assist': failOnce }, statusExtra);
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry for the complete response'));
  assert.ok(retry);
  await retry.props.onClick();
  assert.equal(calls, 1);
  const tree2 = rerender();
  const retry2 = nodes(tree2).find(n => n.type === 'button' && n.children.includes('Retry'));
  assert.ok(retry2, 'failed replay offers Retry');
  await retry2.props.onClick();
  const bodies = requests.filter(r => r.path === '/assist').map(r => r.body);
  assert.equal(bodies.length, 2);
  assert.ok(bodies.every(b => b.question === 'old q?'));
});

test('answer-less history entries keep their replay action', async () => {
  const statusExtra = { recent: [{ request_id: 'r-e', question: 'failed q?', answer: '', status: 'error', error: 'RuntimeError: down', warnings: [], sources: [], request: { question: 'failed q?', project: null, all_projects: false, web_fallback: false } }] };
  const { tree, requests } = pane('', true, false, '', {}, {}, statusExtra);
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Retry for the complete response'));
  assert.ok(retry, 'error entries keep a retry action');
  await retry.props.onClick();
  const last = requests.filter(r => r.path === '/assist').at(-1);
  assert.equal(last.body.question, 'failed q?');
});

test('idle bar explains why start is unavailable', async () => {
  const { tree } = pane('', true, false, '', {}, {}, {});
  assert.ok(nodes(tree).some(n => n.type === 'p' && n.children.some(c => typeof c === 'string' && c.includes('Confirm permission'))));
});

test('trailing-audio transcript segments carry a visible marker', () => {
  const { tree } = pane('', true, true, '', {}, {}, { segments: [
    { speaker: 'you', text: 'hello', timestamp: 1 },
    { speaker: 'you', text: 'tail words', timestamp: 2, partial_tail: true },
  ] });
  const text = n => (n.children || []).map(c => typeof c === 'string' ? c : text(c)).join('');
  const marked = nodes(tree).filter(n => n.type === 'span' && text(n).includes('trailing audio'));
  assert.equal(marked.length, 1);
});

test('idle setup exposes exactly one primary start action and one consent control', () => {
  const { tree } = pane('', true);
  const all = nodes(tree);
  const starts = all.filter(n => n.type === 'button' && (n.children.includes('Start listening') || n.children.includes('Attach to Meetily')));
  assert.equal(starts.length, 1);
  // Consent, web fallback: no duplicated permission/start controls on the idle path.
  const boxes = all.filter(n => n.type === 'input' && n.props.type === 'checkbox');
  assert.ok(boxes.length <= 2, `expected at most consent + web-fallback checkboxes, found ${boxes.length}`);
});

test('paused meeting offers resume and explicit new meeting', () => {
  const { tree } = pane('', true, true, '', {}, {}, { segments: [{ speaker: 'you', text: 'hi', timestamp: 1 }] });
  const labels = nodes(tree).filter(n => n.type === 'button').flatMap(n => n.children.filter(c => typeof c === 'string'));
  assert.ok(labels.includes('Resume listening') || labels.includes('Resume'), 'paused meeting offers resume');
  assert.ok(labels.includes('New meeting'), 'paused meeting offers an explicit fresh start');
});

test('fresh idle offers start without resume or new-meeting actions', () => {
  const { tree } = pane('', true, true, '', {}, {}, {});
  const labels = nodes(tree).filter(n => n.type === 'button').flatMap(n => n.children.filter(c => typeof c === 'string'));
  assert.ok(labels.includes('Start listening') || labels.includes('Attach to Meetily'));
  assert.ok(!labels.includes('Resume listening') && !labels.includes('Resume') && !labels.includes('New meeting'));
});

test('capture failure surfaces a needs-attention badge, not paused', () => {
  const { tree } = pane('', true, true, '', {}, {}, { error: 'Native audio capture failed.' });
  const text = n => (n.children || []).map(c => typeof c === 'string' ? c : text(c)).join('');
  const badge = nodes(tree).find(n => n.type === 'badge');
  assert.ok(badge, 'status badge exists');
  assert.ok(text(badge).includes('Needs attention'));
});

test('new meeting sends new_meeting flag', async () => {
  const { tree, requests } = pane('', true, true, '', {}, {}, { segments: [{ speaker: 'you', text: 'hi', timestamp: 1 }] });
  const btn = nodes(tree).filter(n => n.type === 'button').find(n => n.children.includes('New meeting'));
  assert.ok(btn, 'new meeting button exists when prior content');
  await btn.props.onClick();
  const start = requests.find(r => r.path === '/start');
  assert.ok(start, '/start called');
  assert.equal(start.body.new_meeting, true);
});


test('overlay show forwards the explicit web query', async () => {
  const { tree, requests } = pane('', true, true, 'explicit q');
  const btn = nodes(tree).filter(n => n.type === 'button').find(n => n.children.includes('Show overlay'));
  assert.ok(btn, 'show overlay offered');
  await btn.props.onClick();
  const show = requests.find(r => r.path === '/overlay/show');
  assert.ok(show, '/overlay/show called');
  assert.equal(show.body.scope.web_query, 'explicit q');
});

test('clear resets local state when the request succeeds even if refresh fails', async () => {
  const { tree, rerender, requests } = pane('edited', true, false, '', { 1: 'typed question', 23: { question: 'q?', extra: {} } }, {}, { segments: [{ speaker: 'you', text: 'hi', timestamp: 1 }] }, {}, { failInvalidate: true });
  assert.ok(nodes(tree).some(n => n.type === 'textarea' && n.props.value === 'typed question'), 'question starts typed');
  const btn = nodes(tree).filter(n => n.type === 'button').find(n => n.children.includes('Clear meeting'));
  assert.ok(btn, 'clear offered with prior content');
  await btn.props.onClick();
  assert.ok(requests.some(r => r.path === '/clear'), 'clear was sent');
  const inputs = nodes(rerender()).filter(n => n.type === 'textarea');
  assert.ok(inputs.every(n => n.props.value === ''), 'question input reset despite failed refresh');
});

test('failed forget boundary offers Clear again, not silent dismiss', async () => {
  const { tree, requests } = pane('', true, false, '', {}, {},
    { meetily_clear_error: 'Could not install the Meetily forget boundary: snapshot locked.' });
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Clear again'));
  assert.ok(retry, 'retry offered for failed boundary');
  await retry.props.onClick();
  assert.ok(requests.some(r => r.path === '/clear'), 'clear retried');
});

test('accepted assist with failed refresh is not reported as failed', async () => {
  const { tree, rerender, requests } = pane('', true, false, '', {}, { '/assist': { accepted: true, request_id: 'r-9' } }, {}, {}, { failInvalidate: true });
  await nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer')).props.onClick();
  assert.equal(requests.filter(r => r.path === '/assist').length, 1);
  const after = rerender();
  assert.ok(!nodes(after).some(n => n.type === 'button' && n.children.includes('Retry')), 'no Retry for an accepted request');
  assert.ok(nodes(after).some(n => n.type === 'button' && n.children.includes('Cancel answer')), 'accepted request stays tracked');
});
