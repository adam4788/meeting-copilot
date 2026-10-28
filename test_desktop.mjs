import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Exercise the real uncompiled pane with lightweight React/SDK stand-ins.
// This checks rendering/request contracts, not visual acceptance.
function pane(scope, supported = true, fallback = true, web = '', settings = {}) {
  let registered;
  let stateIndex = 0;
  const requests = [];
  const contributions = [];
  const revealed = [];
  let visible = false;
  const visibility = { get: () => visible };
  const ctx = {
    rest: async (path, options = {}) => { requests.push({ path, ...options }); return {}; },
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
    useState: initial => { const i = stateIndex++; const overrides = { 0: scope, 2: web, ...settings }; return [Object.hasOwn(overrides, i) ? overrides[i] : initial === true ? fallback : initial, () => {}]; },
    useEffect: () => {},
    useQueryClient: () => ({ invalidateQueries: async () => {} }),
    useQuery: options => ({ data: options.queryKey[1] === 'status'
      ? { capabilities: supported ? { all_projects: true, automatic_web_fallback: true, independent_models: true, macos_transcription: true } : undefined }
      : options.queryKey[1] === 'projects' ? [{ name: 'alpha', path: '/projects/alpha' }]
      : options.queryKey[1] === 'models' ? { providers: [{ id: 'openai-codex', label: 'Codex', models: [{ id: 'gpt-test', label: 'Test model' }] }] } : [] }),
    useValue: atom => atom.get(),
    host: { paneVisibility: () => visibility, revealPane: id => { revealed.push(id); visible = true; } }, Button: 'button', Input: 'input', Textarea: 'textarea', Badge: 'badge', PALETTE_AREA: 'palette',
  });
  return { tree: registered.render(), requests, contributions, revealed };
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
  assert.equal(request.body.web_fallback, true);
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
