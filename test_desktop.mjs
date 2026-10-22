import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Exercise the real uncompiled pane with lightweight React/SDK stand-ins.
// This checks rendering/request contracts, not visual acceptance.
function pane(scope, supported = true, fallback = true, web = '') {
  let registered;
  let stateIndex = 0;
  const requests = [];
  const ctx = {
    rest: async (path, options = {}) => { requests.push({ path, ...options }); return {}; },
    register: item => { if (item.area === 'panes') registered = item; },
  };
  const h = (type, props, ...children) => typeof type === 'function'
    ? type(props || {}) : { type, props: props || {}, children: children.flat(Infinity) };
  const source = readFileSync(new URL('./desktop/plugin.js', import.meta.url), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export default', 'const plugin =');
  vm.runInNewContext(source + '\nplugin.register(ctx);', {
    ctx, h,
    useState: initial => { const i = stateIndex++; return [i === 0 ? scope : i === 2 ? web : initial === true ? fallback : initial, () => {}]; },
    useEffect: () => {},
    useQueryClient: () => ({ invalidateQueries: async () => {} }),
    useQuery: options => ({ data: options.queryKey[1] === 'status'
      ? { capabilities: supported ? { all_projects: true, automatic_web_fallback: true } : undefined }
      : options.queryKey[1] === 'projects' ? [{ name: 'alpha', path: '/projects/alpha' }] : [] }),
    host: {}, Button: 'button', Input: 'input', Textarea: 'textarea', Badge: 'badge', PALETTE_AREA: 'palette',
  });
  return { tree: registered.render(), requests };
}
function nodes(tree) {
  return tree && typeof tree === 'object' ? [tree, ...(tree.children || []).flatMap(nodes)] : [];
}

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
