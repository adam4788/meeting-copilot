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
test('Meta key form saves explicitly, clears input, reads back, and never caches secrets', async () => {
  let saved = false;
  const info = () => ({ saved, request_configured: false, verified: false, activation: 'host-scoped', revision: 'r'.repeat(64) });
  const ui = pane('', true, false, '', {}, {
    '/credentials/meta': (_path, opts) => {
      if (opts.method === 'POST') {
        assert.equal(opts.body.action, 'save');
        assert.equal(opts.body.key, 'fake-ui-key-only');
        saved = true;
      }
      return info();
    },
  });
  const button = (tree, text) => nodes(tree).find(n => n.type === 'button' && n.children.includes(text));
  let tree = ui.tree;
  const field = nodes(tree).find(n => n.props['aria-label'] === 'Meta API key');
  assert.ok(field, 'masked key field exists inside the pane');
  assert.equal(field.props.type, 'password');
  assert.equal(field.props.autoComplete, 'new-password');
  assert.equal(button(tree, 'Save key').props.disabled, true, 'unknown status cannot overwrite');
  await button(tree, 'Recheck key status').props.onClick();
  tree = ui.rerender();
  const input = nodes(tree).find(n => n.props['aria-label'] === 'Meta API key');
  input.props.ref.current = { value: 'fake-ui-key-only' };
  input.props.onChange({ target: input.props.ref.current });
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 0, 'typing never saves');
  tree = ui.rerender();
  await button(tree, 'Save key').props.onClick();
  assert.equal(input.props.ref.current.value, '', 'erase draft immediately after submission');
  tree = ui.rerender();
  assert.ok(saved, 'save changed the backend fixture');
  assert.equal(ui.requests.at(-1).method, undefined, 'separate exact-target readback');
  assert.match(JSON.stringify(tree), /Saved — new scoped requests can use the key immediately/i);
  assert.match(JSON.stringify(tree), /Not verified/i);
  assert.ok(!JSON.stringify(tree).includes('fake-ui-key-only'));
});

test('Meta key never claims a status source before or after a failed check', async () => {
  const ui = pane('', true, false, '', {}, {
    '/credentials/meta': () => { throw new Error('unavailable'); },
  });
  const verifyUnknown = tree => {
    const text = JSON.stringify(tree);
    assert.match(text, /Key status not checked/);
    assert.doesNotMatch(text, /This status used/);
  };
  verifyUnknown(ui.tree);
  await nodes(ui.tree).find(n => n.type === 'button' && n.children.includes('Recheck key status')).props.onClick();
  const after = ui.rerender();
  verifyUnknown(after);
  assert.match(JSON.stringify(after), /Key settings unavailable/);
});

test('Meta key explains scoped activation, process fallback and snapshot limits', async () => {
  for (const activation of ['host-scoped', 'process']) {
    const ui = pane('', true, false, '', {}, { '/credentials/meta': () => ({
      saved: true, request_configured: true, process_configured: true,
      process_differs_from_saved: true, activation, verified: false, revision: 'r'.repeat(64),
    }) });
    await nodes(ui.tree).find(n => n.type === 'button' && n.children.includes('Recheck key status')).props.onClick();
    const text = JSON.stringify(ui.rerender());
    assert.match(text, /Configured for this status request/);
    assert.match(text, activation === 'host-scoped' ? /This status used a host-scoped credential snapshot/ : /This status used the process environment/);
    assert.match(text, /new scoped requests.*immediately/i);
    assert.match(text, /saved file.*precedence.*inherited/i);
    assert.match(text, /existing requests.*workers.*retain/i);
    assert.match(text, /process.environment.*fallback/i);
    assert.match(text, /Not verified with Meta/);
    assert.doesNotMatch(text, /does not override it|before using the new key|Configured in running backend/);
  }
});

test('Meta key save errors do not echo transport secrets or claim readiness', async () => {
  const ui = pane('', true, false, '', {}, {
    '/credentials/meta': (_path, opts) => {
      if (opts.method === 'POST') throw new Error('fake-ui-key-only');
      return { saved: false, request_configured: false, verified: false, revision: 'r'.repeat(64) };
    },
  });
  const button = (tree, text) => nodes(tree).find(n => n.type === 'button' && n.children.includes(text));
  await button(ui.tree, 'Recheck key status').props.onClick();
  const input = nodes(ui.rerender()).find(n => n.props['aria-label'] === 'Meta API key');
  input.props.ref.current = { value: 'fake-ui-key-only' };
  input.props.onChange({ target: input.props.ref.current });
  await button(ui.rerender(), 'Save key').props.onClick();
  const tree = ui.rerender();
  assert.equal(input.props.ref.current.value, '');
  assert.ok(!JSON.stringify(tree).includes('fake-ui-key-only'));
  assert.match(JSON.stringify(tree), /could not be confirmed/i);
});

test('Meta key label matches accessible name and invalid lengths explain bounds', async () => {
  const ui = pane('', true, false, '', {}, { '/credentials/meta': () => ({ saved: false, revision: 'r'.repeat(64) }) });
  const button = (tree, text) => nodes(tree).find(n => n.type === 'button' && n.children.includes(text));
  await button(ui.tree, 'Recheck key status').props.onClick();
  const field = nodes(ui.rerender()).find(n => n.props['aria-label'] === 'Meta API key');

  // Rerenders create new nodes; locate the nested input by its accessible name.
  const visibleLabel = nodes(ui.rerender()).find(n => n.type === 'label' && nodes(n).some(x => x.props['aria-label'] === 'Meta API key'));
  assert.ok(visibleLabel.children.includes(field.props['aria-label']));
  for (const value of ['short', 'x'.repeat(4097)]) {
    field.props.ref.current = { value };
    field.props.onChange({ target: field.props.ref.current });
    await button(ui.rerender(), 'Save key').props.onClick();
    assert.match(JSON.stringify(ui.rerender()), /8–4096/);
    assert.equal(ui.requests.filter(r => r.method === 'POST').length, 0);
  }
});

for (const realHost of [false, true]) for (const race of [false, true]) test(`Meta key pane effects reach isolated backend (host=${realHost}, interleaving=${race})`, async t => {
  const { spawn, spawnSync } = await import('node:child_process');
  if (realHost && spawnSync(process.env.PYTHON || 'python', ['-c', "import agent.secret_scope, hermes_cli.web_server_profiles"]).status !== 0) {
    t.skip('Installed Hermes host unavailable'); return;
  }
  const { createInterface } = await import('node:readline');
  const child = spawn(process.env.PYTHON || 'python', ['-u', realHost ? 'test_credential_host_scope.py' : 'test_credentials.py', '--ui-bridge'], { stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: child.stdout });
  const pending = [];
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk.toString(); });
  lines.on('line', line => {
    const slot = pending.shift();
    const response = JSON.parse(line);
    if (response.status >= 400) slot.reject(new Error('Backend refused request'));
    else slot.resolve(response.body);
  });
  child.on('exit', () => { for (const slot of pending.splice(0)) slot.reject(new Error('Backend exited')); });
  const call = (path, opts = {}) => new Promise((resolve, reject) => {
    pending.push({ resolve, reject });
    child.stdin.write(JSON.stringify({ path, opts }) + '\n');
  });
  t.after(async () => {
    child.stdin.end();
    await new Promise(resolve => { if (child.exitCode !== null) resolve(); else child.once('exit', resolve); });
    assert.ok(!stderr.includes('fake-ui-key-only'), 'stderr contains no synthetic key');
  });
  const routedCall = async (path, opts = {}) => {
    const result = await call(path, opts);
    if (race && opts.method === 'POST' && opts.body.action === 'save') {
      // Real second writer commits between this pane's POST and GET.
      const second = await call(path);
      await call(path, { method: 'POST', body: { action: 'save', key: 'fake-other-writer-key', revision: second.revision } });
    }
    return result;
  };
  const ui = pane('', true, false, '', {}, { '/credentials/meta': routedCall }, {}, { window: { confirm: () => true } });
  const button = (tree, text) => nodes(tree).find(n => n.type === 'button' && n.children.includes(text));
  await button(ui.tree, 'Recheck key status').props.onClick();
  const field = nodes(ui.rerender()).find(n => n.props['aria-label'] === 'Meta API key');
  field.props.ref.current = { value: 'fake-ui-key-only' };
  field.props.onChange({ target: field.props.ref.current });
  assert.equal((await call('/test/saved-effect')).saved, false);
  await button(ui.rerender(), 'Save key').props.onClick();
  if (race) {
    assert.deepEqual(await call('/test/saved-effect'), { saved: false, mode_private: true, runtime_unchanged: true });
    const rendered = JSON.stringify(ui.rerender());
    assert.match(rendered, /changed.*after|another.*edit/i);
    assert.doesNotMatch(rendered, /Saved — new scoped requests can use the key immediately/);
    assert.doesNotMatch(rendered, /fake-other-writer-key|fake-ui-key-only/);
    return;
  }
  assert.deepEqual(await call('/test/saved-effect'), { saved: true, mode_private: true, runtime_unchanged: true });
  assert.equal(field.props.ref.current.value, '');
  assert.match(JSON.stringify(ui.rerender()), /Not verified/);
  await button(ui.rerender(), 'Remove saved key').props.onClick();
  assert.deepEqual(await call('/test/saved-effect'), { saved: false, mode_private: true, runtime_unchanged: true });
  assert.match(JSON.stringify(ui.rerender()), /No saved key/);
});

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

test('meta cloud transcription is opt-in, warns, and sends provider/mode', async () => {
  const caps = { capabilities: { all_projects: true, automatic_web_fallback: true, independent_models: true, macos_transcription: true, meta_asr: true, overlay: true, overlay_stream: true, overlay_cancel: true } };
  const { tree, requests } = pane('', true, false, '',
    { 3: true, 4: 'macos', 25: 'meta', 26: 'DIARIZATION' }, {}, caps);
  const provider = nodes(tree).find(n => n.type === 'select' && n.props['aria-label'] === 'Transcription provider');
  assert.ok(provider, 'transcription provider select renders');
  assert.equal(provider.props.value, 'meta');
  const mode = nodes(tree).find(n => n.type === 'select' && n.props['aria-label'] === 'Meta ASR mode');
  assert.ok(mode, 'ASR mode select renders for the meta provider');
  assert.equal(mode.props.value, 'DIARIZATION');
  assert.ok(!nodes(tree).some(n => n.type === 'select' && n.props['aria-label'] === 'Local transcription model'),
    'local model select hides when cloud provider is selected');
  assert.ok(JSON.stringify(tree).includes('meeting audio is sent to Meta'), 'cloud warning is explicit');
  const start = nodes(tree).find(n => n.type === 'button' && n.children.includes('Start listening'));
  await start.props.onClick();
  const body = requests.find(r => r.path === '/start').body;
  assert.equal(body.transcription_provider, 'meta');
  assert.equal(body.asr_mode, 'DIARIZATION');
});

test('capture card discloses cloud audio transmission for selected Meta transcription', () => {
  for (const [backend, provider, expected] of [['macos', 'meta', true], ['macos', 'local', false], ['meetily', 'meta', false]]) {
    const { tree } = pane('', true, false, '', { 4: backend, 25: provider });
    const capture = nodes(tree).find(n => n.type === 'section' && n.props['aria-label'] === 'Capture session');
    const disclosure = nodes(capture).find(n => n.props['data-cloud-capture-disclosure'] === true);
    assert.equal(!!disclosure, expected, `${backend}/${provider}`);
    if (disclosure) {
      assert.match(JSON.stringify(disclosure), /audio.*sent to Meta/i);
      assert.equal(disclosure.props.role, 'status');
      assert.ok(capture.children.indexOf(disclosure) < capture.children.findIndex(n => n?.type === 'div' && nodes(n).some(x => x.type === 'button' && x.children.includes('Start listening'))));
      const start = nodes(capture).find(n => n.type === 'button' && n.children.includes('Start listening'));
      assert.equal(start.props.disabled, true, 'permission gate remains in effect');
    }
  }
  const listening = pane('', true, false, '', { 3: true, 4: 'macos', 25: 'meta' }, {}, { listening: true });
  const notice = nodes(listening.tree).find(n => n.props['data-cloud-capture-disclosure'] === true);
  assert.match(JSON.stringify(notice), /Pause to change/);
});

test('meta provider option is disabled without a configured key', async () => {
  const { tree } = pane('', true, false, '', { 3: true, 4: 'macos' });
  const option = nodes(tree).find(n => n.type === 'option' && n.props.value === 'meta');
  assert.ok(option, 'meta option renders');
  assert.equal(option.props.disabled, true);
  const provider = nodes(tree).find(n => n.type === 'select' && n.props['aria-label'] === 'Transcription provider');
  assert.equal(provider.props.value, 'local', 'local remains the default');
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

function reactImports(src) {
  // Checkouts may use CRLF: `/\}.*$/` cannot cross a trailing `\r`
  // (`.` excludes it and `$` will not match mid-string), so normalize
  // line endings before parsing or `useRef } from 'react';` counts as a name.
  const text = src.replace(/\r\n?/g, '\n');
  const importLine = text.split('\n').find(l => l.startsWith('import {') && l.includes('react'));
  assert.ok(importLine, 'expected a react import line');
  return new Set(importLine.replace(/^import \{/, '').replace(/\}.*$/, '').split(',').map(s => s.trim()));
}

test('pane imports every React hook it calls (no undeclared globals)', () => {
  const src = readFileSync(new URL('./desktop/plugin.js', import.meta.url), 'utf8');
  const imported = reactImports(src);
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

test('react import parser tolerates CRLF checkouts', () => {
  const src = readFileSync(new URL('./desktop/plugin.js', import.meta.url), 'utf8');
  const crlf = src.replace(/\n/g, '\r\n');
  const imported = reactImports(crlf);
  assert.ok(imported.has('useRef'), 'CRLF must not fuse the import tail into a name');
  assert.ok(![...imported].some(s => s.includes('react')), 'no `} from \'react\'` fragments as names');
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

test('answer region carries a scroll ref so new answers find the user', async () => {
  const { tree } = pane('', true, false, '', {}, {},
    { result: { answer: 'Ship it Monday.', sources: [], elapsed_seconds: 1 } });
  const section = nodes(tree).find(n => n.type === 'section'
    && JSON.stringify(n.children).includes('Suggested answer'));
  assert.ok(section, 'result section renders');
  assert.ok(section.props.ref && typeof section.props.ref === 'object',
    'result section is attached to the answer scroll ref');
});

test('clear question empties the composer', async () => {
  const { tree, rerender } = pane('', true, false, '', { 1: 'Why is the sky blue?' }, {}, {});
  const clear = nodes(tree).find(n => n.type === 'button' && n.children.includes('Clear question'));
  assert.ok(clear, 'clear button shows when a question is typed');
  await clear.props.onClick();
  assert.ok(!nodes(rerender()).some(n => n.type === 'button' && n.children.includes('Clear question')),
    'clear button hides once the question is empty');
});

test('clearing a question returns focus to the composer without sending', () => {
  const { tree, rerender, requests } = pane('', true, false, '', { 1: 'Draft question' });
  const label = nodes(tree).find(n => n.type === 'label' && nodes(n).some(c => c.type === 'textarea'));
  assert.ok(label.props.ref, 'composer wrapper exposes its DOM ref');
  let focused = 0;
  label.props.ref.current = { querySelector: selector => selector === 'textarea' ? { focus: () => { focused++; } } : null };
  nodes(tree).find(n => n.type === 'button' && n.children.includes('Clear question')).props.onClick();
  assert.equal(focused, 1, 'focus returns to the question field');
  assert.equal(nodes(rerender()).find(n => n.type === 'textarea').props.value, '');
  assert.equal(requests.length, 0, 'clearing does not send a request');
});

test('setup tiles are separated by real gaps, not touching edges', async () => {
  const { tree } = pane('', true, false, '', {}, {}, {});
  const setup = nodes(tree).find(n => n.type === 'details'
    && JSON.stringify(n.children).includes('Answer setup'));
  assert.ok(setup, 'answer setup renders');
  const stack = (setup.children || []).find(c => c && typeof c === 'object' && c.type === 'div'
    && c.props.style && c.props.style.display === 'flex');
  assert.ok(stack, 'setup content uses an inner flex stack');
  assert.ok(stack.props.style.gap >= 12, `expected gap >= 12, got ${stack.props.style.gap}`);
});

test('transcript segments render as bounded tiles with accent edges', async () => {
  const { tree } = pane('', true, true, '', {}, {}, { segments: [
    { speaker: 'Alice', text: 'hello there', timestamp: 1 },
    { speaker: 'Bob', text: 'general kenobi', timestamp: 2 },
  ] });
  const tiles = nodes(tree).filter(n => n.type === 'p'
    && n.props.style && String(n.props.style.borderLeft || '').includes('3px solid'));
  assert.equal(tiles.length, 2);
  for (const tile of tiles) {
    assert.ok(String(tile.props.style.border || '').includes('var(--ui-stroke-secondary)'));
    assert.ok(String(tile.props.style.background || '').includes('color-mix'));
    assert.ok(tile.props.style.borderRadius >= 8);
  }
});

test('setup rhythm: cards use grids and the web toggle is a tile', async () => {
  const { tree } = pane('', true, false, '', {}, {}, {});
  const setup = nodes(tree).find(n => n.type === 'details'
    && JSON.stringify(n.children).includes('Answer setup'));
  const stack = (setup.children || []).find(c => c && typeof c === 'object' && c.type === 'div'
    && c.props.style && c.props.style.display === 'flex');
  assert.ok(stack && stack.props.style.gap >= 14, `expected setup gap >= 14, got ${stack && stack.props.style.gap}`);
  const modelCard = nodes(tree).find(n => n.type === 'section'
    && JSON.stringify(n.children).includes('Answer model'));
  assert.ok(modelCard, 'answer model card renders');
  assert.equal(modelCard.props.style.display, 'grid');
  assert.ok(modelCard.props.style.gap >= 10);
  const overlayCard = nodes(tree).find(n => n.type === 'section'
    && JSON.stringify(n.children).includes('Meeting overlay'));
  assert.ok(overlayCard, 'overlay card renders');
  assert.equal(overlayCard.props.style.display, 'grid');
  const toggle = nodes(tree).find(n => n.type === 'label'
    && n.props.style && String(n.props.style.border || '').includes('var(--ui-stroke-secondary)')
    && JSON.stringify(n.children).includes('Automatic web fallback'));
  assert.ok(toggle, 'web fallback toggle renders as a bordered tile');
  assert.ok(toggle.props.style.borderRadius >= 10);
  assert.ok(toggle.props.style.padding.includes('12px'));
});

test('each speaker keeps one distinct color across tiles, with a legend', async () => {
  const { tree } = pane('', true, true, '', {}, {}, { segments: [
    { speaker: 'Alice', text: 'first', timestamp: 1 },
    { speaker: 'Bob', text: 'second', timestamp: 2 },
    { speaker: 'Alice', text: 'third', timestamp: 3 },
  ] });
  const tiles = nodes(tree).filter(n => n.type === 'p'
    && n.props.style && String(n.props.style.borderLeft || '').includes('3px solid'));
  assert.equal(tiles.length, 3);
  const colors = tiles.map(t => t.props.style.borderLeft);
  assert.equal(colors[0], colors[2], 'same speaker, same color');
  assert.notEqual(colors[0], colors[1], 'different speakers, different colors');
  const speakerNames = tiles.map(t => t.children[0]);
  assert.ok(speakerNames.every((s, i) => s && s.props.style
    && s.props.style.color === colors[i].replace('3px solid ', '')),
    'speaker name uses the tile color');
  const legend = nodes(tree).filter(n => n.type === 'span'
    && (n.children || []).some(c => typeof c === 'string' && (c === 'Alice' || c === 'Bob')));
  assert.equal(legend.length, 2, 'legend chips name each speaker');
});

test('composer shortcut sends once without inserting a newline or interrupting composition', () => {
  for (const modifier of ['metaKey', 'ctrlKey']) {
    const { tree, requests } = pane('', true, false, '', { 1: 'My question' });
    const composer = nodes(tree).find(n => n.type === 'textarea');
    let prevented = 0;
    const event = { key: 'Enter', [modifier]: true, preventDefault: () => { prevented++; } };
    composer.props.onKeyDown({ ...event, nativeEvent: { isComposing: true } });
    composer.props.onKeyDown({ ...event, repeat: true });
    composer.props.onKeyDown({ ...event, key: 'a' });
    composer.props.onKeyDown({ key: 'Enter', preventDefault: event.preventDefault });
    assert.equal(requests.length, 0);
    assert.equal(prevented, 0, 'ordinary typing and IME composition remain untouched');
    composer.props.onKeyDown(event);
    assert.equal(prevented, 1);
    assert.equal(requests.filter(r => r.path === '/assist').length, 1);
    assert.equal(requests[0].body.question, 'My question');
  }
  for (const [supported, status] of [[true, { busy: true }], [false, {}]]) {
    const { tree, requests } = pane(supported ? '' : '__all_projects__', supported, false, '', {}, {}, status);
    let prevented = false;
    nodes(tree).find(n => n.type === 'textarea').props.onKeyDown({ key: 'Enter', metaKey: true, preventDefault: () => { prevented = true; } });
    assert.equal(requests.length, 0, 'disabled shortcut must not send');
    assert.equal(prevented, false);
  }
});

test('answer setup is a bordered panel with a padded inner stack', async () => {
  const { tree } = pane('', true, false, '', {}, {}, {});
  const setup = nodes(tree).find(n => n.type === 'details'
    && JSON.stringify(n.children).includes('Answer setup'));
  assert.ok(setup, 'answer setup renders');
  assert.ok(String(setup.props.style.border || '').includes('var(--ui-stroke-secondary)'),
    'setup expandable has a visible panel boundary');
  assert.ok(setup.props.style.borderRadius >= 12);
  const wrapper = (setup.children || []).find(c => c && typeof c === 'object' && c.type === 'div'
    && c.props.style && c.props.style.display === 'flex');
  assert.ok(wrapper, 'setup content sits in an inner flex wrapper, not on the details element');
  assert.ok(wrapper.props.style.gap >= 14);
  const summary = (setup.children || []).find(c => c && c.type === 'summary');
  assert.ok(JSON.stringify(summary.children).includes('Answer setup'), 'summary labels the panel');
});

test('capture session offers an overlay quick toggle', async () => {
  const { tree, requests } = pane('', true, false, '', {}, {}, {});
  const session = nodes(tree).find(n => n.type === 'section' && n.props['aria-label'] === 'Capture session');
  const toggle = nodes(session).find(n => n.type === 'button' && n.children.includes('Show overlay'));
  assert.ok(toggle, 'overlay can be shown from the top card without opening setup');
  await toggle.props.onClick();
  assert.ok(requests.some(r => r.path === '/overlay/show'), 'quick toggle shows the overlay');
});

test('layout order: capture session with permission comes first, transcript above history', async () => {
  const { tree } = pane('', true, true, '', {}, {}, { segments: [
    { speaker: 'Alice', text: 'hi', timestamp: 1 },
  ], recent: [{ request_id: 'r1', question: 'q?', answer: 'a', status: 'done', sources: [] }] });
  const flat = nodes(tree);
  const sessionAt = flat.findIndex(n => n.type === 'section' && n.props['aria-label'] === 'Capture session');
  const sourceAt = flat.findIndex(n => (n.type === 'section' || n.type === 'details')
    && JSON.stringify(n.children).includes('Transcript source'));
  assert.ok(sessionAt > -1 && sourceAt > sessionAt, 'session card precedes source card');
  const session = flat[sessionAt];
  const sessionNodes = nodes(session);
  assert.ok(sessionNodes.some(n => n.type === 'input' && n.props.type === 'checkbox'
    && n.props['aria-label'] === 'Capture permission'), 'permission check lives in the top card');
  assert.ok(sessionNodes.some(n => n.type === 'button'
    && (n.children.includes('Start listening') || n.children.includes('Attach to Meetily')
      || n.children.includes('Resume') || n.children.includes('Resume listening'))),
    'primary start lives in the top card');
  const source = flat[sourceAt];
  assert.ok(!nodes(source).some(n => n.type === 'input' && n.props['aria-label'] === 'Capture permission'),
    'no duplicate permission check in the source card');
  const transcriptAt = flat.findIndex(n => n.type === 'details'
    && JSON.stringify(n.children).includes('Live transcript'));
  const recentAt = flat.findIndex(n => n.type === 'details'
    && JSON.stringify(n.children).includes('Recent answers'));
  assert.ok(transcriptAt > -1 && recentAt > transcriptAt, 'live transcript above recent answers');
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
