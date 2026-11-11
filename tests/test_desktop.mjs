import './test_audio_diagnostics.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

// Explicit synthetic readiness. No device, permissions or model download.
function readyPreflight(signature, overrides = {}) {
  return { version: 1, ready: true, checks: [{ id: 'fixture', label: 'Synthetic readiness', status: 'ready', detail: 'No devices.' }],
    receivedAt: Date.now(), signature, ...overrides };
}

async function realBackend(t) {
  const child = spawn(process.env.PYTHON || 'python', ['-B', '-u', '-m', 'tests.lifecycle_test_backend'], { cwd: new URL('../', import.meta.url), stdio: ['pipe', 'pipe', 'pipe'] });
  let id = 0, stderr = '';
  const waiting = new Map(), errors = [], traffic = [];
  child.stderr.on('data', data => { stderr += data; });
  const lines = createInterface({ input: child.stdout });
  lines.on('line', line => {
    try {
      const data = JSON.parse(line), pending = waiting.get(data.id);
      assert.ok(pending, 'unexpected backend reply');
      waiting.delete(data.id);
      if (data.error) { errors.push(data.error); pending.reject(new Error(data.error)); }
      else pending.resolve(data.result);
    } catch (error) { errors.push(error); }
  });
  const exited = new Promise(resolve => child.on('exit', (code, signal) => {
    for (const pending of waiting.values()) pending.reject(new Error(`backend exited: ${code}/${signal}: ${stderr}`));
    waiting.clear(); resolve(code);
  }));
  t.after(async () => {
    child.stdin.end();
    assert.equal(await exited, 0, stderr);
    assert.deepEqual(errors, []);
    assert.equal(stderr, '', 'no swallowed backend/thread errors');
    lines.close();
  });
  const rpc = message => new Promise((resolve, reject) => {
    const key = ++id; waiting.set(key, { resolve, reject });
    child.stdin.write(JSON.stringify({ ...message, id: key }) + '\n');
  });
  const rest = async (path, options = {}) => {
    const reply = await rpc({ path, ...options });
    traffic.push({ path, ...options, requestBody: options.body, ...reply });
    if (reply.code >= 400) throw new Error(`${reply.code}: ${JSON.stringify(reply.body)}`);
    return reply.body;
  };
  const mount = (globals = {}, transport = rest, overrides = {}, initialStatus = {}) => {
    const timers = new Map(); let next = 0;
    const status = initialStatus;
    const p = pane('', true, false, '', { 3: true, 4: 'macos' }, transport, status, {
      setInterval: fn => { timers.set(++next, fn); return next; },
      clearInterval: key => timers.delete(key), ...globals,
    }, { effects: true, ...overrides });
    return { ...p,
      async refresh() { Object.assign(status, await transport('/status')); return p.rerender(); },
      async heartbeat() { for (const fn of timers.values()) await fn(); await rpc({ command: 'inspect' }); } };
  };
  return { rpc, rest, mount, traffic };
}

test('quick overlay toggle uses real host effect and discloses failed Show outside details', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  await backend.rpc({ command: 'install-show-fault' });
  const ui = await backend.mount();
  try {
    const before = disclosedNodes(ui.tree);
    assert.match(JSON.stringify(before), /Screen-share privacy/);
    await button(ui.tree, 'Show overlay').props.onClick();
    const visible = JSON.stringify(disclosedNodes(ui.rerender()));
    assert.match(visible, /Overlay action not confirmed/);
    assert.match(visible, /Retry Show \/ Hide/);
    assert.doesNotMatch(JSON.stringify(ui.rerender()), /synthetic-private-helper-path/);
    assert.equal(backend.traffic.find(r => r.path === '/overlay/show').code, 503);
    const state = await backend.rest('/status');
    assert.equal(state.overlay_shown, false, 'failed helper must not become shown');
    await backend.rpc({ command: 'allow-show' });
    await button(ui.rerender(), 'Show overlay').props.onClick();
    assert.equal((await backend.rest('/status')).overlay_shown, true);
    await button(ui.rerender(), 'Hide overlay').props.onClick();
    assert.equal((await backend.rest('/status')).overlay_shown, false);
  } finally { ui.unmount(); await backend.rpc({ command: 'inspect' }); }
});

test('real renderer heartbeat cannot keep abandoned capture alive after reload', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  const owner = backend.mount();
  await button(owner.tree, 'Start listening').props.onClick();
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  // No cleanup: the first renderer vanished. A fresh JS global has a new ID.
  const reloaded = backend.mount();
  await button(reloaded.tree, 'Start listening').props.onClick();
  assert.equal(backend.traffic.filter(r => r.path === '/overlay/claim').at(-1).body.reason, 'different_renderer');
  for (let i = 0; i < 4; i++) {
    await reloaded.heartbeat();
    await reloaded.pollStatus();
    await backend.rpc({ command: 'tick', seconds: 10 });
  }
  const stopped = await backend.rpc({ command: 'inspect' });
  assert.equal(stopped.running, false, 'rejected renderer must not renew abandoned capture');
  assert.equal(stopped.stops, 1);
  reloaded.unmount();
});

test('real current owner renews capture; superseded heartbeat and status do not', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  const globals = { globalThis: { crypto: webcrypto } };
  const old = backend.mount(globals);
  await button(old.tree, 'Start listening').props.onClick();
  const stalePane = old.requests.find(r => r.path === '/start').body.pane;
  const current = backend.mount(globals);
  await button(current.tree, 'Start listening').props.onClick();
  for (let i = 0; i < 5; i++) {
    await backend.rpc({ command: 'tick', seconds: 10 });
    await current.heartbeat();
  }
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  const before = (await backend.rpc({ command: 'inspect' })).seen;
  for (let i = 0; i < 4; i++) {
    await old.heartbeat(); await old.pollStatus();
    // A heartbeat already queued in transport before re-registration.
    await backend.rest('/heartbeat', { method: 'POST', body: { pane: stalePane } });
    await backend.rpc({ command: 'tick', seconds: 10 });
  }
  const after = await backend.rpc({ command: 'inspect' });
  assert.equal(after.seen, before, 'stale owner cannot extend lease');
  assert.equal(after.running, false);
  assert.equal(backend.traffic.filter(r => r.path === '/heartbeat').at(-1).body.accepted, false);
  old.unmount(); current.unmount();
  await backend.rpc({ command: 'inspect' });
});

function gate() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
function command(p, label) { return p.contributions.find(c => c.area === 'palette' && c.data.label === label).data.run; }

test('real Restart waits for fenced release before reopening and never leaves capture active', { timeout: 10000 }, async t => {
  const backend = await realBackend(t), entered = gate(), resume = gate();
  t.after(() => resume.resolve());
  const p = backend.mount({}, async (path, options) => {
    if (path === '/overlay/release') { entered.resolve(); await resume.promise; }
    return backend.rest(path, options);
  });
  await button(p.tree, 'Start listening').props.onClick();
  const original = p.contributions.find(c => c.area === 'panes');
  const restarting = command(p, 'Restart Meeting Copilot')();
  // This gate also exposes the old unscoped Pause path without hanging RED.
  await Promise.race([entered.promise, restarting]);
  const whilePending = p.contributions.find(c => c.area === 'panes');
  resume.resolve(); await restarting;
  await backend.rpc({ command: 'inspect' });
  assert.equal(whilePending, original, 'must await release before replacing pane');
  assert.ok(!p.requests.some(r => r.path === '/pause' && !r.body.pane));
  assert.equal((await backend.rpc({ command: 'inspect' })).running, false);
  assert.notEqual(p.contributions.find(c => c.area === 'panes'), original);
  await button(p.rerender(), 'Start listening').props.onClick();
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true, 'replacement remains functional');
  p.unmount(); await backend.rpc({ command: 'inspect' });
});

test('real overlapping Restart cannot remove or pause a newly opened pane', { timeout: 10000 }, async t => {
  const backend = await realBackend(t), entered = gate(), resume = gate();
  t.after(() => resume.resolve());
  const p = backend.mount({}, async (path, options) => {
    if (path === '/overlay/release' || path === '/pause') { entered.resolve(); await resume.promise; }
    return backend.rest(path, options);
  });
  await button(p.tree, 'Start listening').props.onClick();
  command(p, 'Open Meeting Copilot')();
  const restarting = command(p, 'Restart Meeting Copilot')();
  await entered.promise;
  // The old release is held before the backend. First stop the old capture
  // with its owner and a newer action; the replacement must observe idle
  // before its own Start, while the stale release remains in flight.
  const oldStart = p.requests.find(r => r.path === '/start').body;
  await backend.rest('/pause', { method: 'POST', body: { pane: oldStart.pane, action: oldStart.action + 2 } });
  const opener = p.contributions.find(c => c.area === 'statusBar.right');
  nodes(opener.render()).find(n => n.type === 'button').props.onClick();
  command(p, 'Open Meeting Copilot')();
  const replacement = p.contributions.find(c => c.area === 'panes');
  p.rerender(); await p.pollStatus();
  await button(p.rerender(), 'Start listening').props.onClick();
  resume.resolve(); await restarting;
  assert.equal(p.contributions.find(c => c.area === 'panes'), replacement);
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  p.unmount(); await backend.rpc({ command: 'inspect' });
});

test('real Restart cannot retire a newer effect lifecycle in the same registration', { timeout: 10000 }, async t => {
  const backend = await realBackend(t), entered = gate(), resume = gate();
  t.after(() => resume.resolve());
  const p = backend.mount({}, async (path, options) => {
    const result = await backend.rest(path, options);
    if (path === '/overlay/release') { entered.resolve(); await resume.promise; }
    return result;
  });
  await button(p.tree, 'Start listening').props.onClick();
  const original = p.contributions.find(c => c.area === 'panes');
  const restarting = command(p, 'Restart Meeting Copilot')();
  await entered.promise;
  p.replayEffects(); // effects clear retirement and schedule React's next render
  await p.pollStatus(); // release ran at the backend; only its response is delayed
  await button(p.rerender(), 'Start listening').props.onClick();
  resume.resolve(); await restarting;
  await backend.rpc({ command: 'inspect' });
  assert.equal(p.contributions.find(c => c.area === 'panes'), original);
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  p.unmount(); await backend.rpc({ command: 'inspect' });
});

test('real lookup retry restores Start Pause and Clear without replaying a claim', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  let lookups = 0;
  const p = backend.mount({}, (path, options) => {
    if (path === '/overlay/lifecycle' && ++lookups === 1) throw new Error('transient GET');
    return backend.rest(path, options);
  });
  await settle();
  await button(p.tree, 'Start listening').props.onClick();
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  await backend.rpc({ command: 'segment', text: 'Synthetic integration meeting' });
  await button(await p.refresh(), 'Clear meeting').props.onClick();
  assert.deepEqual((await backend.rest('/status')).segments, []);
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true, 'Clear restarts the authorized capture');
  await button(await p.refresh(), 'Pause listening').props.onClick();
  assert.equal((await backend.rpc({ command: 'inspect' })).running, false);
  assert.equal(lookups, 2);
  assert.equal(backend.traffic.filter(r => r.path === '/overlay/claim').length, 1);
  const controls = backend.traffic.filter(r => ['/start', '/clear', '/pause'].includes(r.path));
  assert.deepEqual(controls.map(r => r.code), [200, 200, 200]);
  assert.ok(controls.every(r => r.requestBody.pane && r.requestBody.action > 0));
  assert.ok(controls[0].requestBody.action < controls[1].requestBody.action);
  assert.ok(controls[1].requestBody.action < controls[2].requestBody.action);
  p.unmount(); await backend.rpc({ command: 'inspect' });
});

test('real committed claim with lost reply never replays; explicit new mount recovers', { timeout: 10000 }, async t => {
  const backend = await realBackend(t), committed = gate();
  const globals = { globalThis: { crypto: webcrypto } };
  const old = backend.mount(globals, async (path, options) => {
    const result = await backend.rest(path, options);
    if (path === '/overlay/claim') { committed.resolve(); throw new Error('claim committed, response lost'); }
    return result;
  });
  await committed.promise;
  for (let i = 0; i < 2; i++) await button(old.tree, 'Start listening').props.onClick();
  await old.heartbeat();
  assert.equal(backend.traffic.filter(r => r.path === '/overlay/claim').length, 1);
  assert.ok(!backend.traffic.some(r => ['/start', '/heartbeat'].includes(r.path)));
  old.unmount();
  const newer = backend.mount(globals);
  await button(newer.tree, 'Start listening').props.onClick();
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  newer.unmount(); await backend.rpc({ command: 'inspect' });
});

test('actual macOS parser cleanup failure renders unconfirmed and scoped Retry stop recovers', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  await backend.rpc({ command: 'install-macos-fault' });
  const p = backend.mount();
  await button(p.tree, 'Start listening').props.onClick();
  const failure = await backend.rpc({ command: 'wait-macos-error' });
  assert.equal(failure.running, true, 'failed signals leave the synthetic worker alive');
  assert.equal(failure.status.listening, false);
  assert.equal(failure.status.stop_confirmed, false);
  const tree = await p.refresh();
  assert.match(JSON.stringify(tree), /Stopping — unconfirmed/);
  assert.ok(!button(tree, 'Start listening') && !button(tree, 'Resume listening') && !button(tree, 'New meeting'));
  await backend.rpc({ command: 'allow-stop' });
  await button(tree, 'Retry stop').props.onClick();
  const stopped = await backend.rest('/status');
  assert.equal(stopped.stopping, false);
  assert.equal(stopped.stop_confirmed, true);
  assert.equal((await backend.rpc({ command: 'inspect' })).running, false);
  const pauses = backend.traffic.filter(r => r.path === '/pause');
  assert.equal(pauses.length, 1);
  assert.ok(pauses[0].requestBody.pane && pauses[0].requestBody.action > 0);
  assert.equal(backend.traffic.filter(r => r.path === '/start').length, 1);
  assert.ok(!button(await p.refresh(), 'Retry stop'));
  p.unmount(); await backend.rpc({ command: 'inspect' });
});

test('real denied overlay cleanup stays visible across Clear and failed Hide; retry confirms shutdown', {
  timeout: 10000,
  skip: !['darwin', 'linux'].includes(process.platform), // real POSIX pipe/select helper
}, async t => {
  const backend = await realBackend(t);
  const p = backend.mount();
  await button(p.tree, 'Start listening').props.onClick();
  await backend.rpc({ command: 'install-overlay-fault' });
  await backend.rpc({ command: 'segment', text: 'Question before Clear?' });
  await button(await p.refresh(), 'Clear meeting').props.onClick();
  let tree = await p.refresh();
  assert.match(JSON.stringify(tree), /Overlay.*shutdown is not confirmed/);
  assert.ok(button(tree, 'Retry hide overlay'), 'Clear must retain an explicit cleanup retry');
  assert.equal(button(tree, 'Start listening').props.disabled, true);
  assert.equal((await backend.rpc({ command: 'inspect' })).running, false);
  await button(tree, 'Retry hide overlay').props.onClick();
  tree = await p.refresh();
  assert.ok(button(tree, 'Retry hide overlay'), 'denied retry is not hidden');
  await button(tree, 'Clear meeting').props.onClick();
  tree = await p.refresh();
  assert.match(JSON.stringify(tree), /Overlay.*shutdown is not confirmed/);
  assert.equal((await backend.rpc({ command: 'inspect-overlay' })).alive, true);
  await backend.rpc({ command: 'allow-overlay-stop' });
  await button(tree, 'Retry hide overlay').props.onClick();
  tree = await p.refresh();
  const final = await backend.rpc({ command: 'inspect-overlay' });
  assert.equal(final.alive, false);
  assert.equal(final.pending, false);
  assert.equal(final.status.overlay_stop_confirmed, true);
  assert.equal(final.status.overlay_cleanup_error, null);
  assert.equal(final.status.listening, false);
  assert.doesNotMatch(JSON.stringify(tree), /Overlay.*shutdown is not confirmed/);
  assert.ok(button(tree, 'Show overlay'));
});

test('live helper EOF immediately replaces shown success with actionable cleanup warning', {
  timeout: 10000, skip: !['darwin', 'linux'].includes(process.platform),
}, async t => {
  const backend = await realBackend(t);
  // This lifecycle fixture uses a fake macOS capture object on both POSIX hosts.
  // Advertise that fake readiness rather than selecting Windows capture on Linux.
  const p = backend.mount({}, async (path, options) => {
    const reply = await backend.rest(path, options);
    if (path === '/status') reply.capabilities.macos_transcription = true;
    return reply;
  });
  await button(p.tree, 'Start listening').props.onClick(); // claim using fake audio only
  await button(await p.refresh(), 'Pause listening').props.onClick();
  await backend.rpc({ command: 'install-overlay-channel-fault' });
  await button(await p.refresh(), 'Show overlay').props.onClick();
  assert.ok(JSON.stringify(p.rerender()).includes('Read-only overlay shown'), JSON.stringify(backend.traffic.slice(-3)));
  await backend.rpc({ command: 'close-overlay-stdout' });
  let tree = await p.refresh();
  const status = (await backend.rpc({ command: 'inspect-overlay' })).status;
  assert.equal(status.overlay_shown, null);
  assert.equal(status.overlay_stop_confirmed, false);
  assert.match(JSON.stringify(tree), /click channel failed/);
  assert.match(JSON.stringify(tree), /shutdown is not confirmed/);
  assert.doesNotMatch(JSON.stringify(tree), /Read-only overlay shown/);
  assert.ok(!button(tree, 'Restart capture'), 'overlay fault must not suggest audio restart');
  assert.equal(button(tree, 'Start listening').props.disabled, true);
  await button(tree, 'Retry hide overlay').props.onClick();
  tree = await p.refresh();
  assert.ok(button(tree, 'Retry hide overlay'));
  assert.equal((await backend.rpc({ command: 'inspect-overlay' })).alive, true);
  await backend.rpc({ command: 'allow-overlay-stop' });
  await button(tree, 'Retry hide overlay').props.onClick();
  tree = await p.refresh();
  assert.equal((await backend.rpc({ command: 'inspect-overlay' })).status.overlay_stop_confirmed, true);
  assert.ok(!button(tree, 'Retry hide overlay'));
  assert.equal(button(tree, 'Start listening').props.disabled, false);
});

test('accepted release with unconfirmed physical stop does not reopen or claim success', async () => {
  const p = lifecyclePane({ '/overlay/release': { accepted: true, stopping: true, stop_confirmed: false } }, { 3: true, 4: 'macos' });
  await button(p.tree, 'Start listening').props.onClick();
  const original = p.contributions.find(c => c.area === 'panes');
  await command(p, 'Restart Meeting Copilot')();
  assert.equal(p.contributions.find(c => c.area === 'panes'), original);
  assert.ok(!nodes(p.rerender()).some(n => typeof n.props?.onClick === 'function'));
  assert.ok(p.notices.some(n => n.kind === 'warning' && /not confirmed/.test(n.message)));
  assert.ok(!p.notices.some(n => n.kind === 'success'));
  p.unmount();
  const closed = lifecyclePane({ '/overlay/release': { accepted: true, stopping: true, stop_confirmed: false } });
  await button(closed.tree, 'Show overlay').props.onClick();
  closed.unmount(); await new Promise(resolve => setImmediate(resolve));
  assert.ok(closed.requests.some(r => r.path === '/overlay/release'));
  assert.ok(closed.notices.some(n => n.kind === 'warning' && /not confirmed/.test(n.message)));
});

test('overlay release failure blocks Restart and warns on unmount independently of audio stop', async () => {
  const reply = { accepted: true, stop_confirmed: true, stopping: false, overlay_stop_confirmed: false,
    overlay_cleanup_error: 'Overlay shutdown is not confirmed. Retry Hide / Clear.' };
  const p = lifecyclePane({ '/overlay/release': reply });
  await button(p.tree, 'Show overlay').props.onClick();
  const original = p.contributions.find(c => c.area === 'panes');
  await command(p, 'Restart Meeting Copilot')();
  assert.equal(p.contributions.find(c => c.area === 'panes'), original);
  assert.ok(p.notices.some(n => n.kind === 'warning' && /Overlay shutdown is not confirmed/.test(n.message)));
  assert.ok(!p.notices.some(n => /pane reopened/.test(n.message)));
  const closed = lifecyclePane({ '/overlay/release': reply });
  await button(closed.tree, 'Show overlay').props.onClick();
  closed.unmount(); await new Promise(resolve => setImmediate(resolve));
  assert.ok(closed.notices.some(n => n.kind === 'warning' && /Overlay shutdown is not confirmed/.test(n.message)));
});

test('unconfirmed stop is visible and only offers stop retry, not restart capture', async () => {
  const p = lifecyclePane({}, { 3: true, 4: 'macos' }, {
    listening: false, stopping: true, error: 'Capture stop is not confirmed; audio may still be active.',
    capture: { state: 'stopping' }, segments: [{ text: 'Earlier context' }],
  });
  const tree = p.rerender();
  assert.match(JSON.stringify(tree), /Stopping — unconfirmed/);
  assert.ok(!button(tree, 'Start listening') && !button(tree, 'Resume listening') && !button(tree, 'New meeting'));
  await button(tree, 'Retry stop').props.onClick();
  assert.ok(p.requests.some(r => r.path === '/pause' && r.body.pane));
  assert.ok(!p.requests.some(r => r.path === '/start'));
  p.unmount();
});

test('real failed Restart release stays closed to callbacks and expires instead of legacy fallback', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  const p = backend.mount({}, (path, options) => {
    if (path === '/overlay/release') throw new Error('release transport lost');
    return backend.rest(path, options);
  });
  await button(p.tree, 'Start listening').props.onClick();
  const original = p.contributions.find(c => c.area === 'panes');
  const staleSuggest = button(p.tree, 'Suggest answer').props.onClick;
  await command(p, 'Restart Meeting Copilot')();
  assert.equal(p.contributions.find(c => c.area === 'panes'), original);
  const retired = p.rerender();
  assert.ok(!nodes(retired).some(n => typeof n.props?.onClick === 'function'), 'retired pane has no interactive controls');
  assert.match(JSON.stringify(retired), /Capture pause is not confirmed/);
  assert.match(JSON.stringify(retired), /close.*reopen/i);
  const before = p.requests.length;
  await staleSuggest();
  assert.equal(p.requests.length, before, 'captured old Assist cannot send private meeting context');
  assert.ok(!p.requests.some(r => r.path === '/pause'));
  assert.ok(p.notices.some(n => n.kind === 'warning' && /not completed/.test(n.message)), 'unconfirmed pause is visible');
  await p.heartbeat();
  assert.equal((await backend.rpc({ command: 'tick', seconds: 32 })).running, false);
  p.unmount();
  assert.equal(p.requests.filter(r => r.path === '/overlay/release').length, 1, 'uncertain release is not replayed');
});

test('superseded registration callbacks cannot restart open or heartbeat a newer pane', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  const globals = { globalThis: { crypto: webcrypto } };
  const old = backend.mount(globals);
  await button(old.tree, 'Start listening').props.onClick();
  const restart = command(old, 'Restart Meeting Copilot'), open = command(old, 'Open Meeting Copilot');
  const current = backend.mount(globals);
  await button(current.tree, 'Start listening').props.onClick();
  const requests = old.requests.length;
  await restart(); open(); await old.heartbeat();
  assert.equal(old.requests.length, requests);
  assert.equal(old.revealed.length, 0);
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  old.unregister();
  await backend.rpc({ command: 'inspect' });
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  current.unmount(); await backend.rpc({ command: 'inspect' });
});

test('transient lifecycle GET retries but uncertain claim never replays', async () => {
  let lookups = 0;
  const p = lifecyclePane({ '/overlay/lifecycle': () => {
    if (++lookups === 1) throw new Error('temporary lookup failure');
    return { revision: 0, epoch: 'backend-epoch' };
  } }, { 3: true, 4: 'macos' });
  await settle();
  await button(p.tree, 'Start listening').props.onClick();
  assert.equal(lookups, 2);
  assert.equal(p.requests.filter(r => r.path === '/overlay/claim').length, 1);
  assert.equal(p.requests.filter(r => r.path === '/start').length, 1);
  p.unmount();
  let claims = 0;
  const uncertain = lifecyclePane({ '/overlay/claim': () => { claims++; throw new Error('response lost after commit'); } }, { 3: true, 4: 'macos' });
  await settle();
  for (let i = 0; i < 2; i++) await button(uncertain.tree, 'Start listening').props.onClick();
  assert.equal(claims, 1);
  assert.ok(!uncertain.requests.some(r => r.path === '/start'));
  uncertain.unmount();
});

test('capture recovery confirms changed source before destructive restart', async () => {
  for (const accept of [false, true]) {
    const confirmations = [];
    const { tree, requests } = pane('', true, false, '',
      { 3: true, 4: 'macos', 11: 'system' }, {},
      { error: 'capture failed', error_source: 'capture', result: { answer: 'Retained answer', sources: [] },
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
    { listening: true, backend: 'meetily', error: 'snapshot_unavailable', error_source: 'capture' });
  const recheck = nodes(tree).find(n => n.type === 'button' && n.children.includes('Recheck status'));
  assert.ok(recheck);
  assert.ok(!nodes(tree).some(n => n.type === 'button' && n.children.includes('Restart capture')));
  await recheck.props.onClick();
  assert.ok(!requests.some(r => r.path === '/start' || r.path === '/assist'));
});

// Public host atoms and epoch change synchronously before the next React render.
// `connectionId` identifies the actual source even for equal profile names.
function publicRoute(profile = 'alpha', connectionId = 'source-a') {
  const listeners = new Set();
  const route = { profile, connectionId, epoch: 1 };
  route.profileAtom = {
    get: () => route.profile,
    listen: listener => { listeners.add(listener); return () => listeners.delete(listener); },
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  };
  route.connectionAtom = {
    get: () => route.connectionId,
    listen: listener => { listeners.add(listener); return () => listeners.delete(listener); },
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  };
  route.beginActivation = () => { route.epoch += 1; };
  route.finishActivation = (nextProfile, nextConnection) => {
    route.profile = nextProfile; route.connectionId = nextConnection;
    for (const listener of [...listeners]) listener(route.profile);
  };
  route.activate = (nextProfile, nextConnection) => {
    route.beginActivation(); route.finishActivation(nextProfile, nextConnection);
  };
  return route;
}

// Exercise the real uncompiled pane with lightweight React/SDK stand-ins.
// This checks rendering/request contracts, not visual acceptance.
function pane(scope, supported = true, fallback = true, web = '', settings = {}, respond = {}, statusExtra = {}, browserGlobals = {}, qcBehavior = {}) {
  let registered;
  let paneGeneration = 0;
  let stateIndex = 0;
  let values = [];
  let paneValues = values;
  const settingsValues = [];
  const requests = [];
  const queryOptions = [];
  const contributions = [];
  const revealed = [], navigations = [], hostDisposers = [];
  const notices = [];
  const cacheListeners = new Set();
  const keyedCache = new Map();
  let effects = [];
  let paneEffects = effects;
  const settingsEffects = [];
  const delayedPaneEffects = [];
  let effectIndex = 0;
  let pendingEffects = [];
  const scheduleEffect = (fn, deps) => {
    if (!qcBehavior.effects) return;
    const i = effectIndex++;
    const old = effects[i];
    if (!old || !deps || deps.some((value, j) => !Object.is(value, old.deps?.[j]))) {
      pendingEffects.push(() => { old?.cleanup?.(); effects[i] = { deps, cleanup: fn() }; });
    }
  };
  let statusQuery;
  let visible = false;
  const visibility = { get: () => visible };
  let preferenceAtom;
  // Public SDK shape: route changes update the active source/profile and bump
  // the same activation epoch consumed by gatewayActivationEpoch().
  const route = qcBehavior.route || publicRoute();
  const sdk = qcBehavior.legacySdk ? {} : {
    gatewayActivationEpoch: () => route.epoch,
    useGatewayActivationEpoch: () => route.epoch,
  };
  const activeStatus = () => qcBehavior.statusForRoute?.(route) || statusExtra;
  const ctx = {
    ...(qcBehavior.storage ? { storage: qcBehavior.storage } : {}),
    ...(qcBehavior.os ? { os: qcBehavior.os } : {}),
    onDispose: dispose => hostDisposers.push(dispose),
    rest: async (path, options = {}) => {
      requests.push({ path, ...options });
      const defaults = { '/overlay/lifecycle': { revision: 0, epoch: 'backend-epoch' }, '/overlay/claim': { pane: 'test-pane' }, '/overlay/show': { shown: true, owner: 'test-owner' }, '/overlay/release': { accepted: true } };
      if (typeof respond === 'function') return respond(path, options);
      const r = respond[path] || defaults[path];
      return typeof r === 'function' ? r(path, options) : (r || {});
    },
    register: item => {
      contributions.push(item);
      if (item.area === 'panes') registered = item;
      return () => {
        const index = contributions.indexOf(item);
        if (index < 0) return;
        contributions.splice(index, 1);
        if (item.area === 'panes') {
          visible = false;
          // A remounted React component has new hook slots. Retained setters
          // from the old tree must never mutate the replacement's state.
          const oldValues = paneValues, oldEffects = paneEffects;
          for (const effect of oldEffects) effect?.cleanup?.();
          paneValues = []; paneEffects = []; paneGeneration += 1;
          if (values === oldValues) values = paneValues;
          if (effects === oldEffects) effects = paneEffects;
        }
      };
    },
  };
  if (qcBehavior.settingsHost) ctx.registerSettingsPage = page => {
    if (qcBehavior.settingsHostThrows) throw new Error('unsupported settings host');
    contributions.push({ ...page, area: 'settings.plugins', render: () => {
      const previous = { values, effects, stateIndex, effectIndex, pendingEffects };
      values = settingsValues; effects = settingsEffects; stateIndex = 0; effectIndex = 0; pendingEffects = [];
      try { const tree = page.render(); for (const effect of pendingEffects) effect(); return tree; }
      finally { ({ values, effects, stateIndex, effectIndex, pendingEffects } = previous); }
    } });
    return () => {};
  };
  const h = (type, props, ...children) => typeof type === 'function'
    ? type(props || {}) : { type, props: props || {}, children: children.flat(Infinity) };
  const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export default', 'const plugin =');
  const unregister = vm.runInNewContext(source + '\nplugin.register(ctx);', {
    ctx, h, crypto: webcrypto, sdk,
    atom: initial => {
      if (typeof initial !== 'object') { let value = initial; return { get: () => value, set: next => { value = next; } }; }
      const overrides = qcBehavior.noSeedOverrides ? {} : { 0: scope, ...(qcBehavior.freshDefaults ? {} : { 12: 'openai', 13: 'gpt-test', 18: false }), ...settings };
      let value = { ...initial, ...(Object.hasOwn(overrides, 0) ? { project: overrides[0] } : {}),
        ...(Object.hasOwn(overrides, 9) ? { webFallback: overrides[9] } : {}),
        ...(Object.hasOwn(overrides, 12) ? { answerProvider: overrides[12] } : {}),
        ...(Object.hasOwn(overrides, 13) ? { answerModel: overrides[13] } : {}),
        ...(Object.hasOwn(overrides, 18) ? { excludeHistory: overrides[18] } : {}) };
      preferenceAtom = { get: () => value, set: next => { value = next; } };
      return preferenceAtom;
    },
    useState: initial => {
      const i = stateIndex++;
      if (!(i in values)) {
        const overrides = { 0: scope, 2: web, ...(qcBehavior.freshDefaults ? {} : {12:'openai',13:'gpt-test'}),
          ...(!qcBehavior.seedFirstPaneOnly || paneGeneration === 0 ? settings : {}) };
        // Answer preferences now live in a shared atom, not pane hook slots.
        const indexes = [1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 14, 15, 16, 17];
        const legacyIndex = indexes[i] ?? i + 4;
        values[i] = values === settingsValues ? initial : Object.hasOwn(overrides, legacyIndex) ? overrides[legacyIndex] : initial === true ? fallback : initial;
      }
      const slots = values;
      return [slots[i], v => { slots[i] = typeof v === 'function' ? v(slots[i]) : v; }];
    },
    useRef: initial => {
      const i = stateIndex++;
      if (!(i in values)) values[i] = { current: initial };
      return values[i];
    },
    useEffect: scheduleEffect,
    useLayoutEffect: scheduleEffect,
    useSyncExternalStore: (_subscribe, snapshot) => snapshot(),
    useQueryClient: () => ({ invalidateQueries: qcBehavior.failInvalidate ? async () => { throw new Error('invalidate failed'); } : async () => {},
      getQueryCache: () => ({ subscribe: listener => { cacheListeners.add(listener); return () => cacheListeners.delete(listener); } }),
      getQueryState: key => qcBehavior.keyedCache ? keyedCache.get(JSON.stringify(key)) : key[1] === 'status'
        ? ({ error: qcBehavior.statusError, isPending: qcBehavior.statusPending, fetchStatus: qcBehavior.statusFetchStatus || 'idle', data: { listening: false, stopping: false, overlay_shown: false, overlay_stop_confirmed: true, busy: false, requests: {}, capabilities: supported ? { all_projects: true, automatic_web_fallback: true, independent_models: true, history_opt_out: true, capture_preflight: 1, model_discovery: true, macos_transcription: true, overlay: true } : undefined, ...activeStatus() } })
        : key[1] === 'preflight' ? { data: readyPreflight(key.at(-1), qcBehavior.preflight), error: qcBehavior.preflightError, fetchStatus: qcBehavior.preflightFetchStatus || 'idle', status: 'success' }
        : key[1] === 'projects' ? { data: [{ name: 'alpha', path: '/projects/alpha' }], status: 'success' }
        : key[1] === 'models' ? { data: { providers: qcBehavior.providers ?? [{ id: 'openai', label: 'OpenAI API', models: [], custom_model: true, configured: true, status: 'Key available; connection and model unverified' }] }, status: 'success' }
        : undefined }),
    useQuery: options => {
      queryOptions.push(options);
      if (options.queryKey[1] === 'status' && options.enabled !== false) statusQuery = options;
      const result = { ...(qcBehavior.keyedCache ? { status: 'success', fetchStatus: 'idle' } : {}), error: options.queryKey[1] === 'status' ? qcBehavior.statusError : options.queryKey[1] === 'preflight' ? qcBehavior.preflightError : null, data: options.queryKey[1] === 'status'
        ? { listening: false, stopping: false, overlay_shown: false, overlay_stop_confirmed: true, busy: false, requests: {}, capabilities: supported ? { all_projects: true, automatic_web_fallback: true, independent_models: true, history_opt_out: true, capture_preflight: 1, model_discovery: true, macos_transcription: true, overlay: true, overlay_stream: true, overlay_cancel: true } : undefined, ...activeStatus() }
        : options.queryKey[1] === 'preflight' ? readyPreflight(options.queryKey.at(-1), qcBehavior.preflight)
        : options.queryKey[1] === 'projects' ? (qcBehavior.projectsForRoute?.(route) || [{ name: 'alpha', path: '/projects/alpha' }])
        : options.queryKey[1] === 'models' ? { providers: qcBehavior.providers ?? [{ id: 'openai', label: 'OpenAI API', models: [], custom_model: true, configured: true, status: 'Key available; connection and model unverified' }] } : [] };
      if (!qcBehavior.keyedCache) return result;
      const key = JSON.stringify(options.queryKey);
      if (!keyedCache.has(key)) keyedCache.set(key, result);
      return keyedCache.get(key);
    },
    useValue: atom => { qcBehavior.observedAtoms?.push(atom); return atom.get(); },
    host: { notify: notice => notices.push(notice), paneVisibility: () => visibility, revealPane: id => { revealed.push(id); visible = true; }, navigate: path => navigations.push(path),
      state: { profile: route.profileAtom, connectionId: route.connectionAtom }, activeConnectionId: () => route.connectionId }, Button: 'button', Input: 'input', Textarea: 'textarea', Badge: 'badge', PALETTE_AREA: 'palette',
    ...browserGlobals,
  });
  const render = () => {
    values = paneValues; effects = paneEffects;
    stateIndex = 0; effectIndex = 0; pendingEffects = [];
    const tree = registered.render();
    if (qcBehavior.delayPaneEffects) delayedPaneEffects.push(...pendingEffects);
    else for (const effect of pendingEffects) effect();
    return tree;
  };
  return { tree: qcBehavior.skipInitialPane ? null : render(), rerender: render, requests, queryOptions, contributions, revealed, navigations, notices, unregister,
    forcePreferences: patch => preferenceAtom.set({ ...preferenceAtom.get(), ...patch }),
    notifyCache: () => { for (const listener of [...cacheListeners]) listener(); },
    flushPaneEffects: () => { qcBehavior.delayPaneEffects = false; for (const effect of delayedPaneEffects.splice(0)) effect(); },
    pollStatus: () => statusQuery.queryFn(),
    hostUnload: () => { for (const dispose of hostDisposers) dispose(); },
    unmountSettings: () => { for (const effect of settingsEffects) effect?.cleanup?.(); },
    unmount: () => { for (const effect of paneEffects) effect?.cleanup?.(); },
    replayEffects: () => { for (const effect of effects) effect?.cleanup?.(); effects.length = 0; return render(); } };
}
test('route activation fences a retained retry before rerender and B can ask after reopening', async () => {
  const route = publicRoute('default', 'source-a');
  const traffic = [];
  const prior = { question: 'Synthetic A question', transcript: 'PRIVATE_A', replay_request_id: 'answer-a',
    project: '/projects/alpha', answer_provider: 'openai', answer_model: 'gpt-test' };
  const a = { answer_error: 'Synthetic failure', error_source: 'answer', error_request: prior,
    result: { answer: 'A result', sources: [] }, segments: [{ speaker: 'A', text: 'A transcript' }] };
  const b = { result: { answer: 'B result', sources: [] } };
  const ui = pane('/projects/alpha', true, false, '', {}, (path, options) => {
    traffic.push({ profile: route.profile, source: route.connectionId, epoch: route.epoch, path, body: options.body });
    if (path === '/overlay/lifecycle') return { revision: 0, epoch: 'backend-epoch' };
    if (path === '/overlay/claim') return { accepted: true, pane: 'pane-' + route.connectionId };
    if (path === '/overlay/release') return { accepted: true, stop_confirmed: true, overlay_stop_confirmed: true };
    if (path === '/assist') return { accepted: true, request_id: 'new-b' };
    return {};
  }, {}, { setInterval: () => 0, clearInterval: () => {} },
  { effects: true, route, statusForRoute: r => r.connectionId === 'source-a' ? a : b });
  await settle();
  const retry = button(alertFor(ui.tree, 'Answer failed'), 'Retry').props.onClick;
  const aKeys = new Map(ui.queryOptions.map(q => [q.queryKey[1], q.queryKey]));
  assert.match(JSON.stringify(ui.tree), /A result|A transcript/);
  route.activate('default', 'source-b'); // same profile, different source
  await retry();
  assert.equal(traffic.filter(t => t.path === '/assist').length, 0, 'old A retry must not POST to B');
  assert.doesNotMatch(JSON.stringify(ui.rerender()), /A result|A transcript|PRIVATE_A/);
  // Reopen after the host's active route settles; the old release must not hit B.
  command(ui, 'Open Meeting Copilot')();
  nodes(ui.contributions.find(c => c.area === 'statusBar.right').render()).find(n => n.type === 'button').props.onClick();
  command(ui, 'Open Meeting Copilot')();
  let fresh = ui.rerender(); await settle();
  assert.doesNotMatch(JSON.stringify(fresh), /A result|A transcript|PRIVATE_A/);
  assert.match(JSON.stringify(fresh), /B result/);
  for (const kind of ['status', 'projects', 'models', 'recordings']) {
    const bKey = ui.queryOptions.filter(q => q.queryKey[1] === kind).at(-1).queryKey;
    assert.notDeepEqual(aKeys.get(kind), bKey, `${kind} cache is activation-scoped`);
  }
  assert.ok(!traffic.some(t => t.path === '/overlay/release' && t.source === 'source-b'));
  await button(fresh, 'Suggest answer').props.onClick();
  assert.equal(traffic.filter(t => t.path === '/assist').length, 1, 'fresh B pane still works');
  assert.equal(traffic.find(t => t.path === '/assist').source, 'source-b');
  assert.equal(traffic.find(t => t.path === '/assist').body.transcript, undefined);
  ui.unmount();
});

test('activation and profile changes retire old retry even after switching away and back', async () => {
  for (const switches of [
    [['beta', 'source-a']],
    [['default', 'source-b'], ['default', 'source-a']],
    [['default', 'source-a']], // same source reactivation increments the epoch too
  ]) {
    const route = publicRoute('default', 'source-a');
    const prior = { question: 'Old', transcript: 'PRIVATE_A', replay_request_id: 'a' };
    const ui = pane('', true, false, '', {}, {}, { answer_error: 'Failed', error_source: 'answer', error_request: prior }, {}, { route });
    const retry = button(alertFor(ui.tree, 'Answer failed'), 'Retry').props.onClick;
    for (const [profile, source] of switches) route.activate(profile, source);
    await retry();
    assert.equal(ui.requests.filter(r => r.path === '/assist').length, 0);
    await assert.rejects(ui.pollStatus(), /retired/i);
    assert.match(JSON.stringify(ui.rerender()), /Gateway changed/);
    ui.unmount();
  }
});

test('settings observe activation-scoped query caches and old settings callbacks are fenced', async () => {
  const route = publicRoute('default', 'source-a');
  const ui = pane('', true, false, '', {}, {}, {}, { setInterval: () => 0, clearInterval: () => {} }, {
    effects: true, settingsHost: true, route, keyedCache: true,
    projectsForRoute: r => r.connectionId === 'source-a' ? [{ name: 'SECRET_PROJECT_A', path: '/synthetic/a' }] : [{ name: 'PROJECT_B', path: '/synthetic/b' }],
  });
  await settle();
  const initialSettings = settingsPage(ui).render();
  assert.match(JSON.stringify(initialSettings), /SECRET_PROJECT_A/);
  const oldScope = nodes(initialSettings).find(n => n.type === 'select' && n.props['aria-labelledby'] === 'mc-scope-label');
  route.activate('default', 'source-b');
  oldScope.props.onChange({ target: { value: '/synthetic/a' } });
  const retiredSettings = settingsPage(ui).render();
  assert.doesNotMatch(JSON.stringify(retiredSettings), /SECRET_PROJECT_A|\/synthetic\/a/);
  command(ui, 'Open Meeting Copilot')();
  nodes(ui.contributions.find(c => c.area === 'statusBar.right').render()).find(n => n.type === 'button').props.onClick();
  command(ui, 'Open Meeting Copilot')(); ui.rerender(); await settle();
  const bSettings = settingsPage(ui).render();
  assert.match(JSON.stringify(bSettings), /PROJECT_B/);
  assert.doesNotMatch(JSON.stringify(bSettings), /SECRET_PROJECT_A|\/synthetic\/a/);
  ui.unmount(); ui.unmountSettings();
});

test('pane and settings subscribe to the public active-connection atom during same-profile switches', () => {
  const route = publicRoute('default', 'source-a'), observedAtoms = [];
  const ui = pane('', true, false, '', {}, {}, {}, {}, { route, observedAtoms, settingsHost: true });
  assert.ok(observedAtoms.includes(route.connectionAtom), 'pane must subscribe to connection changes after an epoch bump');
  observedAtoms.length = 0; settingsPage(ui).render();
  assert.ok(observedAtoms.includes(route.connectionAtom), 'settings must subscribe too');
  ui.unmount(); ui.unmountSettings();
});

test('epoch advances before an asynchronous source switch; pane stays retired until reopened', async () => {
  const route = publicRoute('default', 'source-a');
  const prior = { question: 'Old', transcript: 'PRIVATE_A', replay_request_id: 'a' };
  const ui = pane('', true, false, '', {}, {}, { answer_error: 'Failed', error_source: 'answer', error_request: prior }, {}, { route });
  const retry = button(alertFor(ui.tree, 'Answer failed'), 'Retry').props.onClick;
  route.beginActivation(); // source/profile are still A while a connection dials
  await retry(); assert.equal(ui.requests.length, 0);
  assert.match(JSON.stringify(ui.rerender()), /Gateway changed/);
  // If the user reopens too early, it is bound to A at the new epoch. When
  // the connection atom finally publishes B, this pane must retire as well.
  command(ui, 'Open Meeting Copilot')();
  nodes(ui.contributions.find(c => c.area === 'statusBar.right').render()).find(n => n.type === 'button').props.onClick();
  command(ui, 'Open Meeting Copilot')();
  const early = ui.rerender();
  const earlyRetry = button(alertFor(early, 'Answer failed'), 'Retry').props.onClick;
  route.finishActivation('default', 'source-b');
  await earlyRetry();
  assert.match(JSON.stringify(ui.rerender()), /Gateway changed/);
  await retry(); assert.equal(ui.requests.length, 0);
  ui.unmount();
});

test('pending A status and admission cannot publish or lock newly opened B', async () => {
  const route = publicRoute('default', 'source-a'), status = gate(), admission = gate();
  const traffic = [];
  const ui = pane('/projects/alpha', true, false, '', {}, (path, options) => {
    const source = route.connectionId; traffic.push({ source, path, body: options.body });
    if (source === 'source-a' && path === '/status') return status.promise;
    if (source === 'source-a' && path === '/assist') return admission.promise;
    if (path === '/overlay/lifecycle') return { revision: 0, epoch: 'backend-epoch' };
    if (path === '/overlay/claim') return { pane: `pane-${source}` };
    if (path === '/assist') return { accepted: true, request_id: 'b' };
    return {};
  }, {}, { setInterval: () => 0, clearInterval: () => {} },
  { effects: true, route, statusForRoute: r => r.connectionId === 'source-a' ? { segments: [{ text: 'PRIVATE_A' }] } : { result: { answer: 'B answer' } } });
  await settle();
  const pendingStatus = ui.pollStatus();
  const pendingAsk = button(ui.tree, 'Suggest answer').props.onClick();
  await settle();
  route.activate('default', 'source-b');
  command(ui, 'Open Meeting Copilot')();
  nodes(ui.contributions.find(c => c.area === 'statusBar.right').render()).find(n => n.type === 'button').props.onClick();
  command(ui, 'Open Meeting Copilot')();
  ui.rerender(); await settle();
  status.resolve({ result: { answer: 'LATE_PRIVATE_A' }, listening: true });
  admission.resolve({ accepted: true, request_id: 'late-a' });
  await assert.rejects(pendingStatus, /retired/i); await pendingAsk;
  const b = ui.rerender();
  assert.match(JSON.stringify(b), /B answer/);
  assert.doesNotMatch(JSON.stringify(b), /PRIVATE_A|late-a|not confirmed/i);
  await button(b, 'Suggest answer').props.onClick();
  assert.equal(traffic.filter(t => t.path === '/assist' && t.source === 'source-b').length, 1);
  assert.ok(!traffic.some(t => t.path === '/overlay/release' && t.source === 'source-b'));
  ui.unmount();
});

test('pending and retained A progress polling never targets or publishes into B', async () => {
  const route = publicRoute('default', 'source-a'), progress = gate(), timers = [], traffic = [];
  const ui = pane('', true, false, '', { 21: 'request-a', 22: { status: 'streaming', answer: 'A partial' } },
    (path, options) => {
      traffic.push({ source: route.connectionId, path, body: options.body });
      if (path.startsWith('/assist/request-a')) return progress.promise;
      if (path === '/overlay/lifecycle') return { revision: 0, epoch: 'backend-epoch' };
      if (path === '/overlay/claim') return { pane: 'pane-' + route.connectionId };
      return {};
    }, {}, { setInterval: fn => { timers.push(fn); return timers.length; }, clearInterval: () => {} },
    { effects: true, route, seedFirstPaneOnly: true, statusForRoute: r => r.connectionId === 'source-b' ? { result: { answer: 'B answer' } } : {} });
  await settle();
  assert.equal(traffic.filter(t => t.path.startsWith('/assist/request-a')).length, 1);
  const oldTimers = [...timers];
  route.activate('default', 'source-b');
  command(ui, 'Open Meeting Copilot')();
  nodes(ui.contributions.find(c => c.area === 'statusBar.right').render()).find(n => n.type === 'button').props.onClick();
  command(ui, 'Open Meeting Copilot')(); ui.rerender(); await settle();
  progress.resolve({ status: 'done', answer: 'LATE_PRIVATE_A' });
  await settle(); for (const timer of oldTimers) await timer(); await settle();
  const text = JSON.stringify(ui.rerender());
  assert.match(text, /B answer/); assert.doesNotMatch(text, /LATE_PRIVATE_A|A partial|request-a/);
  assert.ok(!traffic.some(t => t.source === 'source-b' && t.path.startsWith('/assist/request-a')));
  assert.ok(!traffic.some(t => t.source === 'source-b' && t.path === '/heartbeat' && t.body?.pane === 'pane-source-a'));
  ui.unmount();
});

test('pending restart from A cannot reopen or announce success in B', async () => {
  const route = publicRoute('default', 'source-a'), release = gate(), entered = gate(), traffic = [];
  const ui = pane('', true, false, '', {}, (path, options) => {
    traffic.push({ source: route.connectionId, path, body: options.body });
    if (path === '/overlay/lifecycle') return { revision: 0, epoch: 'backend-epoch' };
    if (path === '/overlay/claim') return { pane: 'pane-a' };
    if (path === '/overlay/release') { entered.resolve(); return release.promise; }
    return {};
  }, {}, { setInterval: () => 0, clearInterval: () => {} }, { effects: true, route });
  await settle();
  const original = ui.contributions.find(c => c.area === 'panes');
  const pending = command(ui, 'Restart Meeting Copilot')(); await entered.promise;
  route.activate('default', 'source-b');
  release.resolve({ accepted: true, stop_confirmed: true, overlay_stop_confirmed: true });
  await pending;
  assert.equal(ui.contributions.find(c => c.area === 'panes'), original, 'old restart cannot reopen on B');
  assert.ok(!ui.notices.some(n => /pane reopened/i.test(n.message)));
  assert.ok(!traffic.some(t => t.source === 'source-b'));
  assert.match(JSON.stringify(ui.rerender()), /Gateway changed/);
  ui.unmount();
});

test('late A release diagnostics are not published into B', async () => {
  const route = publicRoute('default', 'source-a'), release = gate(), entered = gate();
  const ui = pane('', true, false, '', {}, (path) => {
    if (path === '/overlay/lifecycle') return { revision: 0, epoch: 'backend-epoch' };
    if (path === '/overlay/claim') return { pane: 'pane-a' };
    if (path === '/overlay/release') { entered.resolve(); return release.promise; }
    return {};
  }, {}, { setInterval: () => 0, clearInterval: () => {} }, { effects: true, route });
  await settle();
  const pending = command(ui, 'Restart Meeting Copilot')(); await entered.promise;
  route.activate('default', 'source-b'); ui.rerender();
  release.resolve({ accepted: true, stop_confirmed: false, overlay_stop_confirmed: false, overlay_cleanup_error: 'PRIVATE_A_BACKEND_ERROR' });
  await pending; await settle();
  assert.ok(!ui.notices.some(n => /PRIVATE_A_BACKEND_ERROR/.test(n.message)));
  ui.unmount();
});

test('pending old claim on route change never releases or heartbeats new backend', async () => {
  const route = publicRoute('default', 'source-a'), claim = gate(), traffic = [];
  const ui = pane('', true, false, '', {}, (path, options) => {
    traffic.push({ source: route.connectionId, path, body: options.body });
    if (path === '/overlay/lifecycle') return { revision: 0, epoch: 'backend-epoch' };
    if (path === '/overlay/claim') return claim.promise;
    return {};
  }, {}, { setInterval: () => 0, clearInterval: () => {} }, { effects: true, route });
  await settle(); assert.equal(traffic.filter(t => t.path === '/overlay/claim').length, 1);
  route.activate('default', 'source-b');
  ui.unmount(); claim.resolve({ accepted: true, pane: 'A-owner' }); await settle();
  assert.ok(!traffic.some(t => t.source === 'source-b'));
  assert.ok(ui.notices.some(n => /previous capture and overlay may still be active/i.test(n.message)));
});

test('legacy SDK without activation identity shows upgrade error and makes no backend requests', async () => {
  const ui = pane('', true, false, '', {}, {}, {}, {}, { legacySdk: true, settingsHost: true, effects: true });
  assert.match(JSON.stringify(ui.tree), /cannot safely identify the active gateway/i);
  settingsPage(ui).render(); await settle();
  assert.equal(ui.requests.length, 0);
  ui.unmount(); ui.unmountSettings();
});

test('host unload fences retained commands without relying on register return value', async () => {
  const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export default', 'const plugin =');
  const contributions = [], disposers = [], reveals = [];
  const ctx = {
    register(item) {
      contributions.push(item);
      const dispose = () => { const index = contributions.indexOf(item); if (index >= 0) contributions.splice(index, 1); };
      disposers.push(dispose);
      return dispose;
    },
    onDispose: dispose => { disposers.push(dispose); },
    rest: async () => ({}),
  };
  const atom = initial => { let value = initial; return { get: () => value, set: next => { value = next; } }; };
  const host = { paneVisibility: () => atom(false), revealPane: id => reveals.push(id), notify: () => {} };
  // The real host tracks ctx registrations and intentionally ignores register's return.
  vm.runInNewContext(source + '\nplugin.register(ctx);', { ctx, sdk: {}, h: () => null, atom, host, PALETTE_AREA: 'palette' });
  const restart = contributions.find(item => item.id === 'restart').data.run;
  const open = contributions.find(item => item.id === 'open').data.run;
  for (const dispose of [...disposers]) dispose();
  assert.equal(contributions.length, 0, 'host removed the contributed UI');
  await restart();
  open();
  assert.deepEqual(contributions, [], 'retained commands must not recreate a pane after unload');
  assert.deepEqual(reveals, [], 'retained commands must not reveal a pane after unload');
});

test('host unload releases its claimed pane once even before React unmount', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {},
    { setInterval: () => 0, clearInterval: () => {} }, { effects: true, settingsHost: true });
  await new Promise(resolve => setImmediate(resolve)); // allow the synthetic pane claim to resolve
  assert.equal(ui.requests.filter(r => r.path === '/overlay/claim').length, 1);
  const staleSuggest = button(ui.tree, 'Suggest answer');
  const stalePoll = ui.pollStatus;
  ui.hostUnload();
  ui.hostUnload(); // host and fallback cleanup may both run
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests.filter(r => r.path === '/overlay/release').length, 1);
  await staleSuggest.props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 0);
  await assert.rejects(stalePoll(), /retired/i, 'unloaded plugin cannot make a stale status request');
  assert.equal(ui.requests.filter(r => r.path === '/status').length, 0);
  ui.unmount(); ui.unmountSettings();
  assert.equal(ui.requests.filter(r => r.path === '/overlay/release').length, 1);
});

test('host unload releases a claim that completes after disposal', async () => {
  let finishClaim;
  const claim = new Promise(resolve => { finishClaim = resolve; });
  const ui = pane('/projects/alpha', true, false, '', {}, { '/overlay/claim': () => claim }, {},
    { setInterval: () => 0, clearInterval: () => {} }, { effects: true, settingsHost: true });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests.filter(r => r.path === '/overlay/claim').length, 1);
  ui.hostUnload();
  finishClaim({ accepted: true, pane: 'late-test-pane' });
  await new Promise(resolve => setImmediate(resolve));
  const released = ui.requests.filter(r => r.path === '/overlay/release');
  assert.equal(released.length, 1);
  assert.equal(released[0].body.pane, 'late-test-pane');
  ui.unmount(); ui.unmountSettings();
});

test('host unload fences an in-flight Restart before its release completes', async () => {
  let finishRelease;
  const release = new Promise(resolve => { finishRelease = resolve; });
  const ui = pane('/projects/alpha', true, false, '', {}, { '/overlay/release': () => release }, {},
    { setInterval: () => 0, clearInterval: () => {} }, { effects: true, settingsHost: true });
  await new Promise(resolve => setImmediate(resolve));
  const original = ui.contributions.find(item => item.area === 'panes');
  const restart = ui.contributions.find(item => item.id === 'restart').data.run;
  const pending = restart();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests.filter(r => r.path === '/overlay/release').length, 1);
  ui.hostUnload();
  finishRelease({ accepted: true, stop_confirmed: true, overlay_stop_confirmed: true });
  await pending;
  assert.equal(ui.contributions.find(item => item.area === 'panes'), original, 'old Restart must not register a replacement');
  assert.equal(ui.revealed.length, 0, 'old Restart must not reveal a replacement');
  assert.equal(ui.requests.filter(r => r.path === '/overlay/release').length, 1);
  ui.unmount(); ui.unmountSettings();
});

test('host unload stops synthetic backend capture and rejects stale Start', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  const ui = backend.mount();
  try {
    const start = button(ui.tree, 'Start listening');
    await start.props.onClick();
    assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
    ui.hostUnload();
    const stopped = await backend.rpc({ command: 'inspect' });
    assert.equal(stopped.running, false, 'host disposal must release owned capture');
    assert.equal(stopped.stops, 1);
    await start.props.onClick();
    assert.equal((await backend.rpc({ command: 'inspect' })).running, false);
    assert.equal(backend.traffic.filter(r => r.path === '/start').length, 1);
  } finally { ui.unmount(); await backend.rpc({ command: 'inspect' }); }
});

test('settings host registers Meeting Copilot in Settings → Plugins', () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {}, {}, { settingsHost: true });
  const page = ui.contributions.find(c => c.area === 'settings.plugins');
  assert.ok(page, 'registerSettingsPage must contribute a native settings page');
  assert.equal(page.title, 'Meeting Copilot');
  assert.equal(typeof page.render, 'function');
});

test('settings fields contain padding and border within their available width', () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {}, {}, { settingsHost: true });
  let page = ui.contributions.find(c => c.area === 'settings.plugins').render();
  const provider = nodes(page).find(n => n.props['aria-labelledby'] === 'mc-provider-label');
  provider.props.onChange({ target: { value: 'openai' } });
  page = ui.contributions.find(c => c.area === 'settings.plugins').render();
  const model = nodes(page).find(n => n.props['aria-label'] === 'Answer model');
  assert.ok(model);
  assert.equal(model.props.style.boxSizing, 'border-box');
});

test('native settings move answer preferences and propagate changes to an open pane', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {}, {}, { settingsHost: true });
  const page = ui.contributions.find(c => c.area === 'settings.plugins');
  assert.ok(page, 'native settings page is available');
  let settings = page.render();
  assert.ok(nodes(settings).some(n => n.props['aria-labelledby'] === 'mc-scope-label'));
  assert.ok(nodes(settings).some(n => n.props['aria-label'] === 'Exclude past sessions'));
  assert.ok(nodes(settings).some(n => n.props['aria-labelledby'] === 'mc-provider-label'));
  assert.ok(fallbackControl(settings));
  assert.doesNotMatch(JSON.stringify(ui.tree), /Optional API key overrides/);
  assert.equal(nodes(ui.tree).some(n => n.props['aria-labelledby'] === 'mc-scope-label'), false);
  assert.ok(nodes(ui.tree).some(n => n.props['aria-label'] === 'Capture session'));
  assert.ok(button(ui.tree, 'Show overlay'));
  assert.ok(button(ui.tree, 'Suggest answer'));
  assert.ok(nodes(ui.tree).some(n => n.props['aria-label'] === 'Specific public web query'));
  assert.equal(nodes(settings).some(n => n.props['aria-label'] === 'Specific public web query'), false);
  fallbackControl(settings).props.onChange({ target: { checked: true } });
  const paneAfter = ui.rerender();
  assert.ok(disclosedNodes(paneAfter).some(n => n.props['data-web-fallback-disclosure']));
  assert.equal(ui.requests.some(r => ['/assist', '/start', '/overlay/show'].includes(r.path)), false);
  await button(paneAfter, 'Suggest answer').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/assist').body.web_fallback, true);
});

test('explicit web search is gated by history-exclusion privacy with visible guidance', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, { '/assist': { request_id: 'web-privacy' } }, {},
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true });
  ui.forcePreferences({ excludeHistory: true });
  let tree = ui.rerender();
  nodes(tree).find(n => n.props['aria-label'] === 'Specific public web query').props.onChange({ target: { value: 'public documentation' } });
  tree = ui.rerender();
  const action = button(tree, 'Check sources + web');
  assert.equal(action.props.disabled, true, 'explicit web cannot bypass excluded-history privacy');
  assert.match(JSON.stringify(tree), /exclude past sessions.*turn off web search/i);
  await action.props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/assist'), false);
  ui.forcePreferences({ excludeHistory: false });
  tree = ui.rerender();
  assert.equal(button(tree, 'Check sources + web').props.disabled, false);
  await button(tree, 'Check sources + web').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/assist').body.web_query, 'public documentation');
  ui.unmount(); ui.unmountSettings();
});

test('pane web action uses its current question', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, { '/assist': { request_id: 'fresh-question' } }, {},
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true });
  const composer = nodes(ui.tree).find(n => n.props['aria-labelledby'] === 'mc-q-label');
  composer.props.onChange({ target: { value: 'Current synthetic question?' } });
  let tree = ui.rerender();
  nodes(tree).find(n => n.props['aria-label'] === 'Specific public web query').props.onChange({ target: { value: 'public documentation' } });
  tree = ui.rerender();
  await button(tree, 'Check sources + web').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/assist').body.question, 'Current synthetic question?');
  ui.unmount(); ui.unmountSettings();
});

test('pane public query is explicit and cannot outlive its pane', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, { '/assist': { request_id: 'web-1' } }, {},
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true });
  let tree = ui.tree;
  const input = nodes(tree).find(n => n.props['aria-label'] === 'Specific public web query');
  assert.ok(input, 'public query is in the pane');
  input.props.onChange({ target: { value: 'public documentation' } });
  assert.equal(ui.requests.some(r => r.path === '/assist'), false, 'typing never sends');
  tree = ui.rerender();
  const submit = button(tree, 'Check sources + web');
  assert.equal(submit.props.disabled, false);
  await submit.props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/assist').body.web_query, 'public documentation');
  ui.unmount();
  await submit.props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 1, 'retired pane cannot be used');
  ui.unmountSettings();
});

function settingsStore() {
  const map = new Map();
  return { map, get: (key, fallback) => map.has(key) ? map.get(key) : fallback,
    set: (key, value) => map.set(key, JSON.parse(JSON.stringify(value))) };
}
const settingsPage = ui => ui.contributions.find(c => c.area === 'settings.plugins');
const settingNode = (tree, label) => nodes(tree).find(n => n.props['aria-label'] === label || n.props['aria-labelledby'] === label);

for (const modern of [true, false]) for (const [platform, caps, expected] of [
  ['macOS', { capture_preflight: 1, model_discovery: true, macos_transcription: true, windows_transcription: false, meta_asr: true }, 'macos'],
  ['Windows', { capture_preflight: 1, model_discovery: true, macos_transcription: false, windows_transcription: true, meta_asr: true }, 'native'],
]) test(`fresh ${platform} defaults send both inputs locally (modern=${modern})`, async () => {
  const storage = settingsStore();
  const status = { listening: false, stopping: false, capabilities: caps };
  const ui = pane(undefined, true, false, '', {}, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: modern, effects: true, noSeedOverrides: true, storage });
  let tree = ui.rerender();
  const text = JSON.stringify(disclosedNodes(tree));
  assert.match(text, /Microphone \+ system audio/);
  assert.match(text, /Local transcription/);
  assert.doesNotMatch(text, /meeting audio is sent to Meta/i);
  assert.equal(permissionControl(tree).props.checked, false);
  permissionControl(tree).props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  const start = button(tree, 'Start listening');
  assert.equal(start.props.disabled, false);
  await start.props.onClick();
  const body = ui.requests.find(r => r.path === '/start')?.body;
  assert.ok(body, 'actual outgoing request is made only after consent');
  assert.equal(body.backend, expected);
  assert.equal(body.audio_source, 'both');
  assert.equal(body.transcription_provider, 'local');
  assert.equal(body.recording_id, null);
  assert.equal(body.consent, true);
  ui.unmount(); ui.unmountSettings();
});

for (const modern of [true, false]) test(`delayed platform readiness never freezes or stores an inferred fallback (modern=${modern})`, async () => {
  const storage = settingsStore(), status = { listening: false, stopping: false, capabilities: undefined };
  const ui = pane(undefined, true, false, '', {}, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: modern, effects: true, noSeedOverrides: true, storage });
  let tree = ui.rerender();
  assert.match(JSON.stringify(disclosedNodes(tree)), /capture source.*(not identified|choose)/i);
  assert.equal(button(tree, 'Start listening').props.disabled, true);
  // An unrelated save before discovery must not write a fallback backend.
  const setup = modern ? settingsPage(ui).render() : tree;
  settingNode(setup, 'mc-scope-label').props.onChange({ target: { value: '/projects/alpha' } });
  assert.equal(storage.map.get('preferences-v1')?.backend, undefined);
  status.capabilities = { windows_transcription: true, capture_preflight: 1, model_discovery: true, macos_transcription: false, meta_asr: true };
  tree = ui.rerender();
  assert.match(JSON.stringify(disclosedNodes(tree)), /Windows.*Microphone \+ system audio.*Local transcription/);
  assert.equal(storage.map.get('preferences-v1')?.backend, undefined);
  permissionControl(tree).props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  await button(tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/start')?.body.backend, 'native');
  assert.equal(ui.requests.find(r => r.path === '/start')?.body.audio_source, 'both');
  ui.unmount(); ui.unmountSettings();
});

for (const modern of [true, false]) test(`inferred-source changes revoke consent and a pending claim (modern=${modern})`, async () => {
  const claim = gate(), storage = settingsStore();
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, windows_transcription: false } };
  const ui = pane(undefined, true, false, '', {}, { '/overlay/claim': () => claim.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: modern, effects: true, noSeedOverrides: true, storage });
  permissionControl(ui.tree).props.onChange({ target: { checked: true } });
  const pending = button(ui.rerender(), 'Start listening').props.onClick();
  status.capabilities = { capture_preflight: 1, model_discovery: true, macos_transcription: false, windows_transcription: true };
  claim.resolve({ pane: 'inferred-source-test' });
  await pending;
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 0);
  const tree = ui.rerender();
  assert.equal(permissionControl(tree).props.checked, false);
  assert.match(JSON.stringify(disclosedNodes(tree)), /confirm permission again/i);
  permissionControl(tree).props.onChange({ target: { checked: true } });
  await button(ui.rerender(), 'Start listening').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/start')?.body.backend, 'native');
  ui.unmount(); ui.unmountSettings();
});

for (const modern of [true, false]) for (const pending of [false, true]) test(`inferred source away and back never restores old consent (modern=${modern}, pending=${pending})`, async () => {
  const claim = gate(), storage = settingsStore();
  const mac = { capture_preflight: 1, model_discovery: true, macos_transcription: true, windows_transcription: false };
  const windows = { capture_preflight: 1, model_discovery: true, macos_transcription: false, windows_transcription: true };
  const status = { listening: false, stopping: false, capabilities: mac };
  const ui = pane(undefined, true, false, '', {}, { '/overlay/claim': () => claim.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: modern, effects: true, noSeedOverrides: true, storage });
  permissionControl(ui.tree).props.onChange({ target: { checked: true } });
  const start = button(ui.rerender(), 'Start listening');
  const inflight = pending ? start.props.onClick() : null;
  status.capabilities = windows;
  assert.equal(permissionControl(ui.rerender()).props.checked, false);
  status.capabilities = mac;
  let tree = ui.rerender();
  assert.equal(permissionControl(tree).props.checked, false, 'returning to the original source must not revive consent');
  if (pending) { claim.resolve({ pane: 'source-return-test' }); await inflight; }
  else await start.props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 0);
  tree = ui.rerender();
  permissionControl(tree).props.onChange({ target: { checked: true } });
  if (!pending) claim.resolve({ pane: 'source-return-test' });
  await button(ui.rerender(), 'Start listening').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/start')?.body.backend, 'macos');
  ui.unmount(); ui.unmountSettings();
});

for (const modern of [true, false]) for (const pending of [false, true]) test(`batched inferred-source changes revoke consent before rendering (modern=${modern}, pending=${pending})`, async () => {
  const claim = gate(), storage = settingsStore();
  const mac = { capture_preflight: 1, model_discovery: true, macos_transcription: true, windows_transcription: false };
  const status = { listening: false, stopping: false, capabilities: mac };
  const delayed = pane(undefined, true, false, '', {}, { '/overlay/claim': () => claim.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: modern, effects: true, noSeedOverrides: true, storage });
  permissionControl(delayed.tree).props.onChange({ target: { checked: true } });
  const start = button(delayed.rerender(), 'Start listening');
  const inflight = pending ? start.props.onClick() : null;
  status.capabilities = { capture_preflight: 1, model_discovery: true, macos_transcription: false, windows_transcription: true };
  delayed.notifyCache();
  status.capabilities = mac;
  delayed.notifyCache(); // Deliberately no render between authoritative updates.
  claim.resolve({ pane: 'batched-source-test' });
  if (pending) await inflight;
  else await start.props.onClick();
  assert.equal(delayed.requests.filter(r => r.path === '/start').length, 0);
  assert.equal(permissionControl(delayed.rerender()).props.checked, false);
  delayed.unmount(); delayed.unmountSettings();
});

test('unavailable native readiness identifies platform without pretending capture is ready', () => {
  for (const [caps, platform] of [
    [{ capture_preflight: 1, model_discovery: true, macos_transcription: false, windows_transcription: false, macos_detail: 'On macOS: run prepare_macos.py, allow Microphone / Screen Recording, then restart capture.', windows_detail: 'Windows capture requires Windows.' }, 'macOS'],
    [{ capture_preflight: 1, model_discovery: true, macos_transcription: false, windows_transcription: false, macos_detail: 'macOS transcription requires macOS.', windows_detail: 'Windows capture needs optional audio dependencies in the gateway interpreter: SoundCard. See the Windows setup guide. Capture remains unavailable.' }, 'Windows'],
  ]) {
    const ui = pane(undefined, true, false, '', {}, {}, { listening: false, stopping: false, capabilities: caps }, {}, { settingsHost: true, noSeedOverrides: true });
    const text = JSON.stringify(disclosedNodes(ui.tree));
    assert.match(text, new RegExp(`Capture source: ${platform}`));
    assert.match(text, platform === 'macOS' ? /On macOS: run prepare_macos\.py/ : /Windows capture needs optional audio dependencies/);
    assert.equal(button(ui.tree, 'Start listening').props.disabled, true);
  }
});

test('unknown and contradictory platform capability evidence stays unselected', () => {
  for (const caps of [{ capture_preflight: 1, model_discovery: true, macos_transcription: false, windows_transcription: false, macos_detail: 'macOS transcription requires macOS.', windows_detail: 'Windows capture requires Windows.' }, { capture_preflight: 1, model_discovery: true, macos_transcription: true, windows_transcription: true }]) {
    const storage = settingsStore();
    const ui = pane(undefined, true, false, '', {}, {}, { listening: false, stopping: false, capabilities: caps }, {}, { settingsHost: true, noSeedOverrides: true, storage });
    assert.match(JSON.stringify(disclosedNodes(ui.tree)), /capture source.*(not identified|choose)/i);
    assert.equal(button(ui.tree, 'Start listening').props.disabled, true);
    assert.equal(storage.map.get('preferences-v1')?.backend, undefined);
  }
});

for (const modern of [true, false]) test(`saved Windows source waits for reported readiness (modern=${modern})`, async () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'native', audioSource: 'both' });
  const status = { listening: false, stopping: false, capabilities: {} };
  const ui = pane(undefined, true, false, '', {}, {}, status, {}, { settingsHost: modern, noSeedOverrides: true, storage });
  permissionControl(ui.tree).props.onChange({ target: { checked: true } });
  const tree = ui.rerender();
  assert.equal(button(tree, 'Start listening').props.disabled, true);
  assert.match(JSON.stringify(disclosedNodes(tree)), /Windows.*(not reported|unknown|unavailable)/i);
  await button(tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/start'), false);
});

for (const modern of [true, false]) test(`explicit saved capture choices survive capability inference and unrelated preference edits (modern=${modern})`, () => {
  for (const backend of ['meetily', 'native', 'macos']) {
    const storage = settingsStore();
    storage.set('preferences-v1', { backend, audioSource: 'microphone', transcriptionProvider: 'local' });
    const ui = pane(undefined, true, false, '', {}, {}, { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, windows_transcription: false, independent_models: true, history_opt_out: true } }, {}, { settingsHost: modern, noSeedOverrides: true, storage });
    const page = modern ? settingsPage(ui).render() : ui.tree;
    assert.equal(settingNode(page, 'mc-backend-label').props.value, backend);
    settingNode(page, 'mc-scope-label').props.onChange({ target: { value: '/projects/alpha' } });
    assert.equal(storage.map.get('preferences-v1').backend, backend);
    assert.equal(storage.map.get('preferences-v1').audioSource, 'microphone');
  }
});

test('pane has one overlay toggle across collapsed sections and keeps key disclosures visible', async () => {
  for (const status of [
    { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, overlay: true, independent_models: true, history_opt_out: true } },
    { listening: false, stopping: false, overlay_cleanup_error: 'Overlay shutdown is not confirmed. Verify the window is gone.', capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, overlay: false, independent_models: true, history_opt_out: true } },
  ]) {
    const ui = pane('/projects/alpha', true, false, '', {}, {}, status, {}, { settingsHost: true });
    const tree = ui.rerender();
    const toggles = nodes(tree).filter(n => n.type === 'button' && n.children.some(c => /^(Show overlay|Hide overlay|Retry hide overlay)$/.test(c)));
    assert.equal(toggles.length, 1, 'exactly one overlay action in entire DOM, including closed details');
    assert.equal(nodes(tree).filter(n => n.type === 'button' && n.children.includes('Open settings')).length, 1);
    assert.equal(nodes(tree).some(n => n.props['aria-label'] === 'Getting started'), false);
    const visible = JSON.stringify(disclosedNodes(tree));
    assert.match(visible, /Screen-share privacy/);
    assert.match(visible, /capture source/i);
    assert.match(visible, /permission/i);
    assert.match(visible, /question.*transcript.*model/i);
    if (status.overlay_cleanup_error) assert.match(visible, /shutdown is not confirmed/i);
    assert.ok(nodes(tree).some(n => n.type === 'summary' && /help/i.test(JSON.stringify(n.children))));
  }
});

test('empty capture action row renders no numeric zero artifacts', () => {
  const ui = pane(undefined, true, false, '', {}, {}, { listening: false, stopping: false, segments: [], recent: [], capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true } }, {}, { settingsHost: true, noSeedOverrides: true });
  const capture = nodes(ui.tree).find(n => n.props['aria-label'] === 'Capture session');
  assert.equal(nodes(capture).flatMap(n => n.children || []).filter(child => child === 0).length, 0);
});

test('modern settings own capture configuration; the pane links there and keeps meeting actions', () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'meetily' });
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {}, {}, { settingsHost: true, storage });
  let page = settingsPage(ui).render();
  assert.ok(settingNode(page, 'mc-backend-label'));
  assert.equal(settingNode(ui.tree, 'mc-backend-label'), undefined);
  assert.ok(button(ui.tree, 'Open settings'));
  button(ui.tree, 'Open settings').props.onClick();
  assert.deepEqual(ui.navigations, ['/settings?tab=plugins&plugin=meeting-copilot-live']);
  assert.ok(settingNode(ui.tree, 'mc-rec-label'), 'live recording selection remains in the pane');
  assert.ok(settingNode(ui.tree, 'Specific public web query'), 'one-shot query remains in the pane');
  assert.ok(button(ui.tree, 'Check sources + web'));
  assert.equal(settingNode(page, 'Specific public web query'), undefined);
  assert.equal(button(page, 'Check sources + web'), undefined);
  assert.equal(ui.requests.length, 0, 'opening settings and editing nothing must not start actions');
});

test('modern pane identifies the chosen source and live privacy boundary before consent', () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'both', transcriptionProvider: 'meta', transcriptionChoiceVersion: 1, asrMode: 'DIARIZATION' });
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, independent_models: true, history_opt_out: true, all_projects: true, overlay: true } };
  const ui = pane(undefined, true, false, '', {}, {}, status, {}, { settingsHost: true, noSeedOverrides: true, storage });
  const card = nodes(ui.tree).find(n => n.props['aria-label'] === 'Capture session');
  const text = JSON.stringify(disclosedNodes(card));
  assert.match(text, /capture source: macOS/i);
  assert.match(text, /microphone \+ system audio/i);
  assert.match(text, /Meta.*DIARIZATION/i);
  assert.match(text, /meeting audio is sent to Meta/i);
  assert.equal(nodes(card).find(n => n.type === 'input' && n.props.type === 'checkbox').props.checked, false);
});

test('modern pane discloses an already-running cloud capture even when next defaults are local', () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone', transcriptionProvider: 'local', transcriptionModel: 'base.en' });
  const status = { listening: true, stopping: false, backend: 'macos', last_session: { backend: 'macos', transcription_provider: 'meta', asr_mode: 'DIARIZATION' }, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, independent_models: true, history_opt_out: true, all_projects: true, overlay: true } };
  const ui = pane(undefined, true, false, '', {}, {}, status, {}, { settingsHost: true, noSeedOverrides: true, storage });
  const text = JSON.stringify(disclosedNodes(ui.tree));
  assert.match(text, /meeting audio is sent to Meta/i);
  assert.match(text, /next (capture|start)/i);
});

test('settings-first capture choices persist and are restored without contacting the backend', () => {
  const storage = settingsStore();
  const opts = { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage };
  const ui = pane(undefined, true, false, '', {}, {}, {}, {}, opts);
  let page = settingsPage(ui).render();
  settingNode(page, 'mc-backend-label').props.onChange({ target: { value: 'macos' } });
  page = settingsPage(ui).render();
  settingNode(page, 'Audio source').props.onChange({ target: { value: 'both' } });
  settingNode(page, 'Transcription provider').props.onChange({ target: { value: 'meta' } });
  page = settingsPage(ui).render();
  settingNode(page, 'Meta ASR mode').props.onChange({ target: { value: 'DIARIZATION' } });
  assert.equal(ui.requests.length, 0);
  const saved = storage.map.get('preferences-v1');
  assert.equal(saved.backend, 'macos');
  assert.equal(saved.audioSource, 'both');
  assert.equal(saved.transcriptionProvider, 'meta');
  assert.equal(saved.transcriptionChoiceVersion, 1);
  assert.equal(saved.asrMode, 'DIARIZATION');
  const restored = settingsPage(pane(undefined, true, false, '', {}, {}, {}, {}, opts)).render();
  assert.equal(settingNode(restored, 'mc-backend-label').props.value, 'macos');
  assert.equal(settingNode(restored, 'Audio source').props.value, 'both');
  assert.equal(settingNode(restored, 'Transcription provider').props.value, 'meta');
  assert.equal(settingNode(restored, 'Meta ASR mode').props.value, 'DIARIZATION');
});

test('invalid persisted capture choices fail back to known safe defaults', () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'invalid', audioSource: 'invalid', transcriptionModel: 'invalid', transcriptionProvider: 'invalid', asrMode: 'invalid' });
  const ui = pane(undefined, true, false, '', {}, {}, { capabilities: undefined }, {}, { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage });
  let page = settingsPage(ui).render();
  assert.equal(settingNode(page, 'mc-backend-label').props.value, '');
  settingNode(page, 'mc-backend-label').props.onChange({ target: { value: 'macos' } });
  page = settingsPage(ui).render();
  assert.equal(settingNode(page, 'Audio source').props.value, 'both');
  assert.equal(settingNode(page, 'Transcription provider').props.value, 'local');
  assert.equal(settingNode(page, 'Local transcription model').props.value, 'base.en');
  settingNode(page, 'Transcription provider').props.onChange({ target: { value: 'meta' } });
  assert.equal(settingNode(settingsPage(ui).render(), 'Meta ASR mode').props.value, 'ENDPOINTING');
});

test('late pane effects cannot overwrite newer capture settings or silently start cloud audio', async () => {
  const storage = settingsStore();
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, independent_models: true, history_opt_out: true, all_projects: true, automatic_web_fallback: true, overlay: true } };
  const ui = pane('/projects/alpha', true, false, '', { 3: true }, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, delayPaneEffects: true, storage });
  const staleStart = button(ui.tree, 'Start listening');
  let page = settingsPage(ui).render();
  settingNode(page, 'mc-backend-label').props.onChange({ target: { value: 'macos' } });
  page = settingsPage(ui).render();
  settingNode(page, 'Transcription provider').props.onChange({ target: { value: 'meta' } });
  page = settingsPage(ui).render();
  settingNode(page, 'Audio source').props.onChange({ target: { value: 'system' } });
  settingNode(page, 'Meta ASR mode').props.onChange({ target: { value: 'PUSH_TO_TALK' } });
  ui.flushPaneEffects();
  const saved = storage.map.get('preferences-v1');
  assert.equal(saved.backend, 'macos');
  assert.equal(saved.transcriptionProvider, 'meta');
  assert.equal(saved.audioSource, 'system');
  assert.equal(saved.asrMode, 'PUSH_TO_TALK');
  await staleStart.props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/start'), false, 'a stale pane cannot start under a changed audio disclosure');
  let paneTree = ui.rerender();
  const consentLabel = nodes(paneTree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  const consent = nodes(consentLabel).find(n => n.type === 'input');
  assert.equal(consent.props.checked, false, 'changed source requires fresh permission confirmation');
  assert.match(JSON.stringify(disclosedNodes(paneTree)), /meeting audio is sent to Meta/i);
  consent.props.onChange({ target: { checked: true } });
  paneTree = ui.rerender();
  await button(paneTree, 'Start listening').props.onClick();
  const body = ui.requests.find(r => r.path === '/start').body;
  assert.equal(body.backend, 'macos');
  assert.equal(body.audio_source, 'system');
  assert.equal(body.transcription_provider, 'meta');
  assert.equal(body.asr_mode, 'PUSH_TO_TALK');
  assert.equal(body.consent, true);
  ui.unmount(); ui.unmountSettings();
});

test('switching capture settings away and back still requires fresh permission', async () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'macos', transcriptionProvider: 'local', audioSource: 'microphone', transcriptionModel: 'base.en', asrMode: 'ENDPOINTING' });
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, independent_models: true, history_opt_out: true, all_projects: true, overlay: true } };
  const ui = pane(undefined, true, false, '', {}, {}, status, { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, noSeedOverrides: true, storage });
  let tree = ui.tree;
  let label = nodes(tree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  nodes(label).find(n => n.type === 'input').props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  const staleStart = button(tree, 'Start listening');
  let page = settingsPage(ui).render();
  settingNode(page, 'Transcription provider').props.onChange({ target: { value: 'meta' } });
  page = settingsPage(ui).render();
  settingNode(page, 'Transcription provider').props.onChange({ target: { value: 'local' } });
  await staleStart.props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/start'), false);
  tree = ui.rerender();
  label = nodes(tree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  assert.equal(nodes(label).find(n => n.type === 'input').props.checked, false);
  assert.match(JSON.stringify(disclosedNodes(tree)), /confirm permission again/i);
  ui.unmount(); ui.unmountSettings();
});

function permissionControl(tree) {
  const label = nodes(tree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  return nodes(label).find(n => n.type === 'input' && n.props.type === 'checkbox');
}

const idleCaptureStatus = () => ({ listening: false, stopping: false, capabilities: {
  capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, windows_transcription: true, independent_models: true,
  history_opt_out: true, all_projects: true, overlay: true,
} });

for (const modern of [true, false]) for (const regrant of [false, true]) {
  test(`pending Start honors synchronous consent revocation (modern=${modern}, regrant=${regrant})`, async () => {
    const claim = gate(), storage = settingsStore(), status = idleCaptureStatus();
    storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone', transcriptionModel: 'base.en', transcriptionProvider: 'local', asrMode: 'ENDPOINTING' });
    const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/claim': () => claim.promise }, status,
      { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: modern, effects: true, noSeedOverrides: true, storage });
    await settle();
    assert.equal(ui.requests.filter(r => r.path === '/overlay/claim').length, 1);
    const permission = permissionControl(ui.tree), start = button(ui.tree, 'Start listening');
    assert.equal(permission.props.checked, true);
    assert.equal(start.props.disabled, false);
    const pending = start.props.onClick();
    assert.equal(permissionControl(ui.rerender()).props.disabled, false, 'permission remains revocable during Start');
    permission.props.onChange({ target: { checked: false } });
    if (regrant) permission.props.onChange({ target: { checked: true } });
    claim.resolve({ pane: 'consent-test-pane' });
    await pending;
    assert.equal(ui.requests.filter(r => r.path === '/start').length, 0, 'an old Start cannot use revoked or regranted consent');
    if (!regrant) permissionControl(ui.rerender()).props.onChange({ target: { checked: true } });
    const fresh = ui.rerender();
    await button(fresh, 'Start listening').props.onClick();
    assert.equal(ui.requests.filter(r => r.path === '/start').length, 1, 'fresh consent and a fresh click still start');
    ui.unmount(); ui.unmountSettings();
  });
}

for (const modern of [true, false]) for (const change of ['source', 'stopping', 'offline', 'fetching', 'capability']) {
  test(`pending Start rejects changed ${change} (modern=${modern})`, async () => {
    const claim = gate(), storage = settingsStore(), status = idleCaptureStatus();
    storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone', transcriptionModel: 'base.en', transcriptionProvider: 'local', asrMode: 'ENDPOINTING' });
    const opts = { settingsHost: modern, effects: true, noSeedOverrides: true, storage };
    const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/claim': () => claim.promise }, status,
      { setInterval: () => 0, clearInterval: () => {} }, opts);
    await settle();
    const pending = button(ui.tree, 'Start listening').props.onClick();
    if (change === 'source') {
      if (modern) ui.forcePreferences({ audioSource: 'system' });
      else {
        const source = settingNode(ui.tree, 'Audio source');
        source.props.onChange({ target: { value: 'system' } });
        source.props.onChange({ target: { value: 'microphone' } }); // even an immediate change-back retires the old click
      }
    } else if (change === 'stopping') status.stopping = true;
    else if (change === 'offline') opts.statusError = new Error('synthetic offline');
    else if (change === 'fetching') opts.statusFetchStatus = 'fetching';
    else status.capabilities.macos_transcription = false;
    claim.resolve({ pane: 'stale-start-pane' });
    await pending;
    assert.equal(ui.requests.filter(r => r.path === '/start').length, 0, `must recheck ${change} immediately before posting`);
    ui.unmount(); ui.unmountSettings();
  });
}

for (const modern of [true, false]) test(`pending Start rejects a changed Meetily recording (modern=${modern})`, async () => {
  const claim = gate(), storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'meetily' });
  const ui = pane('', true, false, '', { 3: true, 4: 'meetily', 5: 'recording-a' }, { '/overlay/claim': () => claim.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: modern, effects: true, noSeedOverrides: true, storage });
  await settle();
  const pending = button(ui.tree, 'Attach to Meetily').props.onClick();
  const recording = settingNode(ui.tree, 'mc-rec-label');
  recording.props.onChange({ target: { value: 'recording-b' } });
  recording.props.onChange({ target: { value: 'recording-a' } });
  claim.resolve({ pane: 'recording-test-pane' }); await pending;
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 0);
  ui.unmount(); ui.unmountSettings();
});

test('legacy stale consent grant cannot authorize a different synchronous source selection', async () => {
  const storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone' });
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, { effects: true, noSeedOverrides: true, storage });
  const stale = permissionControl(ui.tree);
  stale.props.onChange({ target: { checked: false } });
  settingNode(ui.tree, 'Audio source').props.onChange({ target: { value: 'system' } });
  stale.props.onChange({ target: { checked: true } });
  let tree = ui.rerender();
  assert.equal(permissionControl(tree).props.checked, false, 'grant for a stale source is not authority');
  await button(tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 0);
  permissionControl(tree).props.onChange({ target: { checked: true } });
  tree = ui.rerender(); await button(tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/start').body.audio_source, 'system');
  ui.unmount();
});

for (const [from, to] of [['macos', 'native'], ['native', 'macos']]) test(`legacy source switch ${from} to ${to} accepts a fresh Start`, async () => {
  const storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: from, audioSource: 'microphone', transcriptionModel: 'base.en', transcriptionProvider: 'meta', transcriptionChoiceVersion: 1, asrMode: 'DIARIZATION' });
  const ui = pane('', true, false, '', { 3: true, 4: from }, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, { effects: true, noSeedOverrides: true, storage });
  await settle();
  settingNode(ui.tree, 'mc-backend-label').props.onChange({ target: { value: to } });
  let tree = ui.rerender();
  assert.equal(permissionControl(tree).props.checked, false, 'source change requires fresh consent');
  permissionControl(tree).props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  await button(tree, 'Start listening').props.onClick();
  const start = ui.requests.find(r => r.path === '/start');
  assert.ok(start, 'a fresh click with the visible selection must not deadlock');
  assert.equal(start.body.backend, to);
  assert.equal(start.body.transcription_provider, to === 'native' ? 'local' : 'meta');
  ui.unmount();
});

test('confirmed pane release unlocks deferred capture defaults without polling stale cache', async () => {
  const release = gate(), storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone' });
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/release': () => release.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, noSeedOverrides: true, storage });
  await button(ui.tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 1);
  status.listening = true; // cached pane status remains listening after close
  ui.unmount();
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, true);
  const before = ui.requests.length;
  release.resolve({ accepted: true, stop_confirmed: true, stopping: false, overlay_stop_confirmed: true });
  await settle();
  const source = settingNode(settingsPage(ui).render(), 'Audio source');
  assert.equal(source.props.disabled, false, 'confirmed release supersedes old listening cache');
  source.props.onChange({ target: { value: 'system' } });
  assert.equal(storage.map.get('preferences-v1').audioSource, 'system');
  assert.equal(ui.requests.length, before, 'settings neither polls nor initiates backend work');
  ui.unmountSettings();
});

for (const result of [
  { accepted: false, stop_confirmed: true, stopping: false },
  { accepted: true, stop_confirmed: false, stopping: false },
  { accepted: true, stop_confirmed: true, stopping: true },
  { accepted: true, stopping: false },
]) test(`unconfirmed pane release does not unlock capture defaults (${JSON.stringify(result)})`, async () => {
  const release = gate(), storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone' });
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/release': () => release.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, noSeedOverrides: true, storage });
  await button(ui.tree, 'Start listening').props.onClick();
  status.listening = true;
  ui.unmount();
  release.resolve(result); await settle();
  const source = settingNode(settingsPage(ui).render(), 'Audio source');
  assert.equal(source.props.disabled, true);
  source.props.onChange({ target: { value: 'system' } });
  assert.equal(storage.map.get('preferences-v1').audioSource, 'microphone');
  ui.unmountSettings();
});

test('a later active status invalidates release proof instead of trusting old idle cache', async () => {
  const release = gate(), poll = gate(), storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone' });
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/release': () => release.promise, '/status': () => poll.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, noSeedOverrides: true, storage });
  await button(ui.tree, 'Start listening').props.onClick();
  ui.unmount();
  release.resolve({ accepted: true, stop_confirmed: true, stopping: false }); await settle();
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, false);
  const later = ui.pollStatus();
  poll.resolve({ listening: true, stopping: false }); await later;
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, true, 'newer active evidence must override release proof');
  ui.unmountSettings();
});

for (const when of ['before', 'during']) test(`status begun ${when} a release cannot unlock capture defaults after rejection`, async () => {
  const release = gate(), poll = gate(), storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone' });
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/release': () => release.promise, '/status': () => poll.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, noSeedOverrides: true, storage });
  await button(ui.tree, 'Start listening').props.onClick();
  let staleStatus;
  if (when === 'before') staleStatus = ui.pollStatus();
  ui.unmount();
  if (when === 'during') staleStatus = ui.pollStatus();
  assert.equal(ui.requests.filter(r => r.path === '/status').length, 1);
  release.resolve({ accepted: false }); await settle();
  poll.resolve({ listening: false, stopping: false }); await staleStatus;
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, true, 'an overlapping idle poll cannot clear uncertain release');
  ui.unmountSettings();
});

test('old active status cannot undo a newer confirmed pane release', async () => {
  const release = gate(), poll = gate(), storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone' });
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/release': () => release.promise, '/status': () => poll.promise }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, noSeedOverrides: true, storage });
  await button(ui.tree, 'Start listening').props.onClick();
  status.listening = true;
  const oldStatus = ui.pollStatus();
  ui.unmount();
  release.resolve({ accepted: true, stop_confirmed: true, stopping: false }); await settle();
  poll.resolve({ listening: true, stopping: false }); await oldStatus;
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, false, 'pre-release GET is not newer active evidence');
  ui.unmountSettings();
});

test('an older release cannot authorize deferred defaults after a new pane takeover', async () => {
  const older = gate(), storage = settingsStore(), status = idleCaptureStatus();
  storage.set('preferences-v1', { backend: 'macos', audioSource: 'microphone' });
  let releases = 0;
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/release': () => ++releases === 1 ? older.promise : { accepted: false }, '/status': () => ({ ...status }) }, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, noSeedOverrides: true, storage });
  await button(ui.tree, 'Start listening').props.onClick();
  status.listening = true;
  ui.replayEffects(); await settle(); // release old owner, claim a new lifecycle
  assert.equal(releases, 1);
  status.listening = false;
  await ui.pollStatus();
  await button(ui.rerender(), 'Start listening').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 2, 'the new pane took over capture');
  status.listening = true;
  ui.unmount(); await settle();
  assert.equal(releases, 2, 'the new pane has its own release');
  older.resolve({ accepted: true, stop_confirmed: true, stopping: false, overlay_stop_confirmed: true });
  await settle();
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, true, 'late old proof cannot release the new owner');
  ui.unmountSettings();
});

test('capture settings reject stale handlers while start is in flight and until an idle status', async () => {
  let finish;
  const storage = settingsStore();
  const state = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, independent_models: true, history_opt_out: true, all_projects: true, overlay: true } };
  const ui = pane('/projects/alpha', true, false, '', { 3: true }, { '/start': () => new Promise(resolve => { finish = resolve; }), '/status': () => state }, state,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, storage });
  let page = settingsPage(ui).render();
  settingNode(page, 'mc-backend-label').props.onChange({ target: { value: 'macos' } });
  let tree = ui.rerender();
  const permission = nodes(tree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  nodes(permission).find(n => n.type === 'input').props.onChange({ target: { checked: true } });
  tree = ui.rerender(); page = settingsPage(ui).render();
  const staleSource = settingNode(page, 'Audio source');
  const pending = button(tree, 'Start listening').props.onClick();
  await settle();
  assert.equal(typeof finish, 'function');
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, true);
  staleSource.props.onChange({ target: { value: 'system' } });
  assert.equal(storage.map.get('preferences-v1').audioSource, 'both');
  await ui.pollStatus(); // an idle GET begun during Start is not a fresh confirmation
  finish({}); await pending;
  staleSource.props.onChange({ target: { value: 'system' } });
  assert.equal(storage.map.get('preferences-v1').audioSource, 'both');
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, true);
  state.listening = true;
  await ui.pollStatus();
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, true);
  state.listening = false;
  await ui.pollStatus();
  assert.equal(settingNode(settingsPage(ui).render(), 'Audio source').props.disabled, false);
  staleSource.props.onChange({ target: { value: 'system' } });
  assert.equal(storage.map.get('preferences-v1').audioSource, 'system');
  ui.unmount(); ui.unmountSettings();
});

test('stale Start and settings callbacks fail closed when capture status becomes unavailable or stopping', async () => {
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, independent_models: true, history_opt_out: true, all_projects: true, overlay: true } };
  const opts = { settingsHost: true, effects: true, storage: settingsStore() };
  const ui = pane('/projects/alpha', true, false, '', {}, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, opts);
  let page = settingsPage(ui).render();
  settingNode(page, 'mc-backend-label').props.onChange({ target: { value: 'macos' } });
  let tree = ui.rerender();
  const label = nodes(tree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  nodes(label).find(n => n.type === 'input').props.onChange({ target: { checked: true } });
  tree = ui.rerender(); page = settingsPage(ui).render();
  const start = button(tree, 'Start listening');
  const source = settingNode(page, 'Audio source');
  opts.statusError = new Error('offline');
  source.props.onChange({ target: { value: 'system' } });
  await start.props.onClick();
  assert.equal(opts.storage.map.get('preferences-v1').audioSource, 'both');
  assert.equal(ui.requests.some(r => r.path === '/start'), false);
  opts.statusError = null;
  opts.statusFetchStatus = 'fetching';
  source.props.onChange({ target: { value: 'system' } });
  await start.props.onClick();
  assert.equal(opts.storage.map.get('preferences-v1').audioSource, 'both');
  assert.equal(ui.requests.some(r => r.path === '/start'), false, 'a cached idle status is not sufficient while a fresh check is pending');
  opts.statusFetchStatus = 'idle';
  status.stopping = true;
  source.props.onChange({ target: { value: 'system' } });
  await start.props.onClick();
  assert.equal(opts.storage.map.get('preferences-v1').audioSource, 'both');
  assert.equal(ui.requests.some(r => r.path === '/start'), false);
  ui.unmount(); ui.unmountSettings();
});

test('capture settings surface source and Meta readiness, and stale selections cannot bypass it', () => {
  const storage = settingsStore();
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, windows_transcription: true, independent_models: true, history_opt_out: true, all_projects: true, overlay: true } };
  const ui = pane('/projects/alpha', true, false, '', {}, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, storage });
  let page = settingsPage(ui).render();
  const oldSource = settingNode(page, 'mc-backend-label');
  status.capabilities.macos_transcription = false;
  delete status.capabilities.windows_transcription;
  oldSource.props.onChange({ target: { value: 'native' } });
  assert.equal(storage.map.get('preferences-v1'), undefined, 'unknown readiness is not permission to select Windows while a pane is mounted');
  status.capabilities.windows_transcription = false;
  status.capabilities.macos_detail = 'Synthetic macOS not ready';
  status.capabilities.windows_detail = 'Synthetic Windows not ready';
  page = settingsPage(ui).render();
  assert.match(JSON.stringify(page), /Synthetic macOS not ready/);
  assert.match(JSON.stringify(page), /Synthetic Windows not ready/);
  oldSource.props.onChange({ target: { value: 'macos' } });
  oldSource.props.onChange({ target: { value: 'native' } });
  assert.equal(storage.map.get('preferences-v1'), undefined);
  status.capabilities.macos_transcription = true;
  oldSource.props.onChange({ target: { value: 'macos' } });
  page = settingsPage(ui).render();
  const oldProvider = settingNode(page, 'Transcription provider');
  status.capabilities.meta_asr = false;
  assert.match(JSON.stringify(settingsPage(ui).render()), /Meta.*unavailable.*key/i);
  oldProvider.props.onChange({ target: { value: 'meta' } });
  assert.equal(storage.map.get('preferences-v1').transcriptionProvider, 'local');
  ui.unmount(); ui.unmountSettings();
});

test('host disposal fences retained capture settings and navigation callbacks', async () => {
  const storage = settingsStore();
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, independent_models: true, history_opt_out: true, all_projects: true, overlay: true } };
  const ui = pane('/projects/alpha', true, false, '', {}, {}, status,
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, storage });
  const staleSource = settingNode(settingsPage(ui).render(), 'mc-backend-label');
  const staleNavigate = button(ui.tree, 'Open settings');
  ui.unregister();
  staleSource.props.onChange({ target: { value: 'macos' } });
  staleNavigate.props.onClick();
  await settle();
  assert.equal(storage.map.get('preferences-v1'), undefined);
  assert.deepEqual(ui.navigations, []);
  assert.equal(ui.requests.some(r => r.path === '/start'), false);
  ui.unmount(); ui.unmountSettings();
});

test('settings-first capture reaches real backend lifecycle with synthetic audio and stops on pane release', { timeout: 10000 }, async t => {
  const backend = await realBackend(t);
  const storage = settingsStore();
  // This fixture deliberately substitutes fake readiness; the backend audio factory is synthetic.
  const status = { listening: false, stopping: false, capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: false, independent_models: true, history_opt_out: true, all_projects: true, automatic_web_fallback: true, overlay: true } };
  const ui = backend.mount({}, undefined, { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage }, status);
  let page = settingsPage(ui).render();
  settingNode(page, 'mc-backend-label').props.onChange({ target: { value: 'macos' } });
  page = settingsPage(ui).render();
  settingNode(page, 'Audio source').props.onChange({ target: { value: 'system' } });
  settingNode(page, 'Local transcription model').props.onChange({ target: { value: 'small.en' } });
  assert.equal(backend.traffic.length, 0, 'settings never start capture or query the backend');
  let tree = ui.rerender();
  const label = nodes(tree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  nodes(label).find(n => n.type === 'input').props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  await button(tree, 'Start listening').props.onClick();
  assert.equal((await backend.rpc({ command: 'inspect' })).running, true);
  const actual = await backend.rest('/status');
  assert.deepEqual(actual.last_session, { backend: 'macos', transcription_model: 'small.en', audio_source: 'system', transcription_provider: 'local', asr_mode: 'ENDPOINTING' });
  assert.equal(backend.traffic.filter(r => r.path === '/start').length, 1);
  ui.unmount();
  await backend.rpc({ command: 'inspect' });
  assert.equal((await backend.rpc({ command: 'inspect' })).running, false);
  ui.unmountSettings();
});

test('confirmed external overlay shutdown unlocks settings without replaying a show', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {
    '/status': { overlay_shown: false, overlay_stop_confirmed: true, busy: false, requests: {} },
  }, {}, { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true });
  await button(ui.tree, 'Show overlay').props.onClick();
  assert.equal(settingNode(settingsPage(ui).render(), 'mc-scope-label').props.disabled, true);
  await ui.pollStatus();
  assert.equal(settingNode(settingsPage(ui).render(), 'mc-scope-label').props.disabled, false);
  assert.equal(ui.requests.filter(r => r.path === '/overlay/show').length, 1);
  ui.unmount(); ui.unmountSettings();
});

test('a status begun during an overlay show cannot unlock its newly shown overlay', async () => {
  let finishShow, finishStatus;
  const show = new Promise(resolve => { finishShow = resolve; });
  const status = new Promise(resolve => { finishStatus = resolve; });
  const ui = pane('/projects/alpha', true, false, '', {}, {
    '/overlay/show': () => show, '/status': () => status,
  }, {}, { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true });
  const showing = button(ui.tree, 'Show overlay').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  const stale = ui.pollStatus();
  finishShow({ shown: true, owner: 'test-owner' });
  await showing;
  finishStatus({ overlay_shown: false, overlay_stop_confirmed: true, busy: false, requests: {} });
  await stale;
  assert.equal(settingNode(settingsPage(ui).render(), 'mc-scope-label').props.disabled, true);
  ui.unmount(); ui.unmountSettings();
});

test('settings observes pane cache without registering a query that can revive capture', () => {
  const ui = pane(undefined, true, false, '', {}, {}, {}, {},
    { settingsHost: true, skipInitialPane: true, noSeedOverrides: true });
  const before = ui.queryOptions.length;
  settingsPage(ui).render();
  assert.equal(ui.queryOptions.length, before, 'settings must not install a queryFn on pane-owned cache keys');
  assert.equal(ui.requests.length, 0, 'settings alone never requests status');
});

test('settings without a mounted pane save deferred defaults without any backend query', async () => {
  const storage = settingsStore();
  const legacy = new Map([['meeting-copilot.scope.v2', JSON.stringify({ project: '/projects/old', answerProvider: 'openai', answerModel: 'old-model', excludeHistory: true,
    transcriptionProvider: 'meta', transcriptionChoiceVersion: 1, secret: 'never-copy-secret', transcript: 'never-copy-transcript', consent: true, web: 'never-copy-query' })]]);
  const globals = { localStorage: { getItem: k => legacy.get(k) || null, setItem: (k, v) => legacy.set(k, v) }, setInterval: () => 0, clearInterval: () => {} };
  const ui = pane(undefined, true, false, '', {}, {}, {}, globals, { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage, effects: true });
  let tree = settingsPage(ui).render();
  assert.match(JSON.stringify(tree), /defaults for the next pane/);
  assert.ok(ui.queryOptions.every(q => q.enabled === false), 'settings only observes cached queries');
  assert.equal(ui.requests.length, 0, 'no /status or other backend access before opening pane');
  settingNode(tree, 'Project path').props.onChange({ target: { value: '/projects/alpha' } });
  tree = settingsPage(ui).render();
  settingNode(tree, 'mc-provider-label').props.onChange({ target: { value: 'openai' } });
  tree = settingsPage(ui).render();
  settingNode(tree, 'Answer model').props.onChange({ target: { value: 'gpt-test' } });
  tree = settingsPage(ui).render();
  fallbackControl(tree).props.onChange({ target: { checked: true } });
  tree = settingsPage(ui).render();
  assert.equal(settingNode(tree, 'Specific public web query'), undefined);
  assert.equal(button(tree, 'Check sources + web'), undefined);
  assert.equal(ui.requests.length, 0, 'editing settings does not contact the backend');
  const saved = storage.map.get('preferences-v1');
  assert.equal(saved.project, '/projects/alpha');
  assert.equal(saved.answerModel, 'gpt-test');
  assert.equal(saved.webFallback, true);
  assert.equal(saved.transcriptionProvider, 'meta');
  assert.equal(saved.transcriptionChoiceVersion, 1, 'explicit cloud selection retains its provenance');
  assert.doesNotMatch(JSON.stringify(saved), /never-copy|not saved/);
  for (const key of ['secret', 'transcript', 'consent', 'web', 'web_query']) assert.equal(Object.hasOwn(saved, key), false);
  const paneTree = ui.rerender();
  assert.ok(disclosedNodes(paneTree).some(n => n.props['data-web-fallback-disclosure']));
  assert.equal(ui.requests.some(r => ['/start', '/assist', '/overlay/show'].includes(r.path)), false);
  ui.unmount(); ui.unmountSettings();
  const restarted = pane(undefined, true, false, '', {}, {}, {}, globals, { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage });
  const restored = settingsPage(restarted).render();
  assert.equal(settingNode(restored, 'Project path').props.value, '/projects/alpha');
  assert.equal(settingNode(restored, 'Answer model').props.value, 'gpt-test');
  assert.equal(fallbackControl(restored).props.checked, true);
  assert.equal(settingNode(restored, 'Specific public web query'), undefined);
  assert.equal(restarted.requests.length, 0);
});

for (const bad of [{ overlay_shown: null }, { overlay_shown: true }, { overlay_shown: undefined }, { overlay_stop_confirmed: false }, { overlay_stop_confirmed: undefined }, { busy: true }, { busy: undefined }, { requests: { active: { status: 'running' } } }, { requests: undefined }, { overlay_cleanup_error: 'unconfirmed' }]) {
  test(`settings fail closed for mounted pane state ${JSON.stringify(bad)}`, () => {
    const state = {};
    const storage = settingsStore();
    const ui = pane('/projects/alpha', true, false, '', {}, {}, state, {}, { settingsHost: true, storage });
    const page = settingsPage(ui), stale = page.render();
    Object.assign(state, bad);
    const locked = page.render();
    for (const label of ['mc-scope-label', 'Exclude past sessions', 'mc-provider-label', 'Answer model']) assert.equal(settingNode(locked, label).props.disabled, true, label);
    assert.equal(fallbackControl(locked).props.disabled, true);
    assert.match(JSON.stringify(locked), /hide the overlay.*finish or cancel/i);
    settingNode(stale, 'mc-scope-label').props.onChange({ target: { value: '__all_projects__' } });
    settingNode(stale, 'Exclude past sessions').props.onChange({ target: { checked: true } });
    settingNode(stale, 'mc-provider-label').props.onChange({ target: { value: 'openrouter' } });
    settingNode(stale, 'Answer model').props.onChange({ target: { value: 'changed' } });
    fallbackControl(stale).props.onChange({ target: { checked: true } });
    assert.equal(storage.map.size, 0, 'stale handlers cannot save');
    assert.equal(ui.requests.length, 0);
  });
}

test('settings guard covers overlay Show and answer admission before status can catch up', async () => {
  for (const action of ['Show overlay', 'Suggest answer']) {
    let complete;
    const path = action === 'Show overlay' ? '/overlay/show' : '/assist';
    const ui = pane('/projects/alpha', true, false, '', {}, { [path]: () => new Promise(resolve => { complete = resolve; }) }, {},
      { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true });
    const page = settingsPage(ui), stale = page.render();
    const pending = button(ui.tree, action).props.onClick();
    await settle();
    assert.equal(typeof complete, 'function');
    const locked = page.render();
    assert.equal(settingNode(locked, 'mc-scope-label').props.disabled, true);
    assert.equal(fallbackControl(locked).props.disabled, true);
    settingNode(stale, 'mc-scope-label').props.onChange({ target: { value: '__all_projects__' } });
    fallbackControl(stale).props.onChange({ target: { checked: true } });
    assert.equal(settingNode(page.render(), 'mc-scope-label').props.value, '/projects/alpha');
    assert.equal(fallbackControl(page.render()).props.checked, false);
    complete(action === 'Show overlay' ? { shown: true, owner: 'owner' } : { request_id: 'running' });
    await pending;
    assert.equal(settingNode(page.render(), 'mc-scope-label').props.disabled, true, 'confirmed operation remains locked until idle evidence');
    ui.unmount(); ui.unmountSettings();
  }
});

test('an immediate pane click sends the newest scope, never a stale snapshot', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {}, {}, { settingsHost: true });
  const staleSuggest = button(ui.tree, 'Suggest answer');
  const page = settingsPage(ui);
  fallbackControl(page.render()).props.onChange({ target: { checked: true } });
  await staleSuggest.props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 1);
  assert.equal(ui.requests.find(r => r.path === '/assist').body.web_fallback, true);
});

test('unavailable preference storage is disclosed instead of claiming defaults were saved', () => {
  const storage = { get: (_key, fallback) => fallback, set: () => {} }; // host silently drops writes
  const ui = pane(undefined, true, false, '', {}, {}, {}, {},
    { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage });
  let tree = settingsPage(ui).render();
  settingNode(tree, 'Project path').props.onChange({ target: { value: '/projects/session-only' } });
  tree = settingsPage(ui).render();
  assert.equal(settingNode(tree, 'Project path').props.value, '/projects/session-only');
  assert.match(JSON.stringify(tree), /could not be saved.*session/i);
  assert.ok(ui.notices.some(n => n.kind === 'warning' && /could not be saved/i.test(n.message)));
});

test('project migration and edits preserve ordinary names that contain token-like words', () => {
  for (const project of ['/projects/tokenizer', '/projects/password-manager', '/projects/sk-learn']) {
    const storage = settingsStore();
    const legacy = new Map([['meeting-copilot.scope.v2', JSON.stringify({ project, answerProvider: 'openai', answerModel: 'tokenizer' })]]);
    const ui = pane(undefined, true, false, '', {}, {}, {}, { localStorage: { getItem: k => legacy.get(k) || null } },
      { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage });
    let tree = settingsPage(ui).render();
    assert.equal(settingNode(tree, 'Project path').props.value, project);
    assert.equal(settingNode(tree, 'Answer model').props.value, 'tokenizer');
    settingNode(tree, 'Project path').props.onChange({ target: { value: project + '-new' } });
    tree = settingsPage(ui).render();
    assert.equal(settingNode(tree, 'Project path').props.value, project + '-new');
    assert.equal(storage.map.get('preferences-v1').project, project + '-new');
  }
});

test('secret-like model input and old accidental secrets are never persisted as preferences', () => {
  const storage = settingsStore();
  const legacy = new Map([['meeting-copilot.scope.v2', JSON.stringify({ answerProvider: 'openai', answerModel: 'sk-legacy-secret' })]]);
  const ui = pane(undefined, true, false, '', {}, {}, {}, { localStorage: { getItem: k => legacy.get(k) || null } },
    { settingsHost: true, skipInitialPane: true, noSeedOverrides: true, storage });
  let tree = settingsPage(ui).render();
  assert.equal(settingNode(tree, 'Answer model').props.value, '', 'do not migrate a credential mistaken for a model');
  settingNode(tree, 'Answer model').props.onChange({ target: { value: 'sk-example-secret' } });
  tree = settingsPage(ui).render();
  assert.equal(settingNode(tree, 'Answer model').props.value, '');
  assert.doesNotMatch(JSON.stringify([...storage.map.values()]), /sk-(legacy|example)-secret/);
});

for (const throws of [false, true]) test(`older or partial host keeps inline settings and web action (throws=${throws})`, () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {}, {}, { settingsHost: throws, settingsHostThrows: throws });
  assert.ok(settingNode(ui.tree, 'mc-scope-label'));
  assert.ok(settingNode(ui.tree, 'mc-provider-label'));
  assert.ok(fallbackControl(ui.tree));
  assert.ok(settingNode(ui.tree, 'Specific public web query'));
  assert.ok(button(ui.tree, 'Check sources + web'));
  assert.match(JSON.stringify(ui.tree), /Optional API key overrides/);
});

test('late pane persistence cannot roll back a newer Settings edit', () => {
  const storage = settingsStore();
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {},
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true, delayPaneEffects: true, storage });
  fallbackControl(settingsPage(ui).render()).props.onChange({ target: { checked: true } });
  assert.equal(storage.map.get('preferences-v1').webFallback, true);
  ui.flushPaneEffects();
  assert.equal(storage.map.get('preferences-v1').webFallback, true, 'capture effect merges current answer preferences');
  assert.equal(fallbackControl(settingsPage(ui).render()).props.checked, true);
  ui.unmount(); ui.unmountSettings();
});

for (const [excludeHistory, webFallback] of [[false, true], [true, false]]) test(`separate settings and mounted pane roots use migrated updated full scope (history excluded=${excludeHistory}, web=${webFallback})`, async () => {
  const storage = settingsStore();
  const old = new Map([['meeting-copilot.scope.v2', JSON.stringify({ project: '/projects/old', answerProvider: 'openai', answerModel: 'old-model', excludeHistory: !excludeHistory, webFallback: !webFallback })]]);
  const ui = pane(undefined, true, false, '', {}, { '/assist': { request_id: 'new-answer' } }, {},
    { localStorage: { getItem: k => old.get(k) || null }, setInterval: () => 0, clearInterval: () => {} },
    { settingsHost: true, effects: true, delayPaneEffects: true, noSeedOverrides: true, storage,
      providers: [{ id: 'openai', label: 'OpenAI', configured: true }, { id: 'openrouter', label: 'OpenRouter', configured: true }] });
  const page = settingsPage(ui);
  const immediateSuggest = button(ui.tree, 'Suggest answer');
  let tree = page.render();
  assert.equal(settingNode(tree, 'mc-scope-label').props.value, '/projects/old', 'v2 project migrates');
  assert.equal(settingNode(tree, 'Answer model').props.value, 'old-model', 'v2 model migrates');
  settingNode(tree, 'mc-scope-label').props.onChange({ target: { value: '/projects/alpha' } });
  tree = page.render();
  settingNode(tree, 'mc-provider-label').props.onChange({ target: { value: 'openrouter' } });
  tree = page.render();
  settingNode(tree, 'Answer model').props.onChange({ target: { value: 'vendor/new-model' } });
  tree = page.render();
  settingNode(tree, 'Exclude past sessions').props.onChange({ target: { checked: excludeHistory } });
  tree = page.render();
  fallbackControl(tree).props.onChange({ target: { checked: webFallback } });
  ui.flushPaneEffects(); // an older pane render must not persist its stale snapshot
  const saved = storage.map.get('preferences-v1');
  assert.equal(saved.project, '/projects/alpha');
  assert.equal(saved.answerProvider, 'openrouter');
  assert.equal(saved.answerModel, 'vendor/new-model');
  assert.equal(saved.excludeHistory, excludeHistory);
  assert.equal(saved.webFallback, webFallback);
  assert.equal(ui.requests.some(r => ['/assist', '/start', '/overlay/show'].includes(r.path)), false);
  await immediateSuggest.props.onClick(); // simulate a click before the pane root commits
  const body = ui.requests.find(r => r.path === '/assist').body;
  assert.equal(body.project, '/projects/alpha');
  assert.equal(body.all_projects, false);
  assert.equal(body.answer_provider, 'openrouter');
  assert.equal(body.answer_model, 'vendor/new-model');
  assert.equal(body.include_history, excludeHistory ? false : undefined);
  assert.equal(body.web_fallback, webFallback);
  ui.unmount(); ui.unmountSettings();
});

test('old pane web callback cannot target a remounted pane lifecycle', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, { '/assist': { request_id: 'web' } }, {},
    { setInterval: () => 0, clearInterval: () => {} }, { settingsHost: true, effects: true });
  settingNode(ui.tree, 'Specific public web query').props.onChange({ target: { value: 'public docs' } });
  const stale = button(ui.rerender(), 'Check sources + web');
  assert.equal(stale.props.disabled, false);
  ui.replayEffects();
  await stale.props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/assist'), false);
  const fresh = ui.rerender();
  settingNode(fresh, 'Specific public web query').props.onChange({ target: { value: 'public docs again' } });
  await button(ui.rerender(), 'Check sources + web').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/assist').body.web_query, 'public docs again');
  ui.unmount(); ui.unmountSettings();
});

test('unmounted settings callbacks and failed status cannot change mounted pane preferences', () => {
  const qc = { settingsHost: true, effects: true, statusError: null };
  const storage = settingsStore(); qc.storage = storage;
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {},
    { setInterval: () => 0, clearInterval: () => {} }, qc);
  const page = settingsPage(ui), stale = page.render();
  qc.statusError = new Error('unavailable');
  assert.equal(fallbackControl(page.render()).props.disabled, true);
  fallbackControl(stale).props.onChange({ target: { checked: true } });
  assert.equal(storage.map.get('preferences-v1')?.webFallback ?? false, false);
  qc.statusError = null;
  ui.unmountSettings();
  fallbackControl(stale).props.onChange({ target: { checked: true } });
  assert.equal(storage.map.get('preferences-v1')?.webFallback ?? false, false);
  ui.unmount();
});

test('legacy web defaults stay off and unproven cloud opt-in stays local in native settings', () => {
  const legacy = new Map([['meeting-copilot.scope', JSON.stringify({ project: '/projects/alpha', webFallback: true, web: 'private query', transcriptionProvider: 'meta', answerProvider: 'openai', answerModel: 'gpt-test' })]]);
  const storage = settingsStore();
  const ui = pane(undefined, true, false, '', {}, {}, {}, { localStorage: { getItem: k => legacy.get(k) || null } },
    { settingsHost: true, noSeedOverrides: true, storage, skipInitialPane: true });
  const tree = settingsPage(ui).render();
  assert.equal(fallbackControl(tree).props.checked, false);
  assert.equal(settingNode(tree, 'Specific public web query'), undefined);
  fallbackControl(tree).props.onChange({ target: { checked: false } });
  assert.equal(storage.map.get('preferences-v1').transcriptionProvider, 'local');
  assert.doesNotMatch(JSON.stringify(storage.map.get('preferences-v1')), /private query/);
});

test('explicit answer-tracking reset needs a fresh idle status before settings unlock', async () => {
  const idle = { overlay_shown: false, overlay_stop_confirmed: true, busy: false, requests: {} };
  const ui = pane('/projects/alpha', true, false, '', {}, {
    '/assist': () => { throw new Error('lost response'); }, '/status': idle,
  }, {}, { window: { confirm: () => true }, setInterval: () => 0, clearInterval: () => {} },
    { settingsHost: true, effects: true });
  await button(ui.tree, 'Suggest answer').props.onClick();
  assert.equal(fallbackControl(settingsPage(ui).render()).props.disabled, true);
  await button(ui.rerender(), 'Reset local answer tracking').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 1);
  assert.ok(ui.requests.some(r => r.path === '/status'), 'fresh read after explicit reset');
  assert.equal(fallbackControl(settingsPage(ui).render()).props.disabled, false);
  ui.unmount(); ui.unmountSettings();
});

test('explicit reset does not unlock settings while fresh status remains busy', async () => {
  const status = { overlay_shown: false, overlay_stop_confirmed: true, busy: true, requests: { active: { status: 'running' } } };
  const ui = pane('/projects/alpha', true, false, '', {}, {
    '/assist': () => { throw new Error('lost response'); }, '/status': () => status,
  }, {}, { window: { confirm: () => true }, setInterval: () => 0, clearInterval: () => {} },
    { settingsHost: true, effects: true });
  await button(ui.tree, 'Suggest answer').props.onClick();
  await button(ui.rerender(), 'Reset local answer tracking').props.onClick();
  assert.equal(fallbackControl(settingsPage(ui).render()).props.disabled, true);
  status.busy = false; status.requests = {};
  await ui.pollStatus();
  assert.equal(fallbackControl(settingsPage(ui).render()).props.disabled, false);
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 1);
  ui.unmount(); ui.unmountSettings();
});

test('Windows capture explains missing dependencies and blocks Start without blocking answers', async () => {
  const ui = pane('/projects/alpha', true, false, '', { 3: true, 4: 'native' }, {}, {
    capabilities: { all_projects: true, independent_models: true, history_opt_out: true,
      windows_transcription: false, windows_detail: 'Install optional SoundCard and faster-whisper dependencies.' },
  });
  assert.equal(button(ui.tree, 'Start listening').props.disabled, true);
  assert.match(JSON.stringify(ui.tree), /optional SoundCard/);
  assert.equal(!!button(ui.tree, 'Suggest answer').props.disabled, false);
  await button(ui.tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/start'), false);
});

test('Windows capture exposes source and model, and never sends a hidden cloud transcription choice', async () => {
  const store = new Map([['meeting-copilot.scope.v2', JSON.stringify({ backend: 'native', transcriptionProvider: 'meta', transcriptionChoiceVersion: 1 })]]);
  const ui = pane('/projects/alpha', true, false, '', { 3: true, 4: 'native' }, {}, {
    capabilities: { all_projects: true, independent_models: true, history_opt_out: true,
      capture_preflight: 1, model_discovery: true, windows_transcription: true, windows_detail: 'Experimental; hardware unverified.' },
  }, { localStorage: { getItem: key => store.get(key), setItem: (key, value) => store.set(key, value) } });
  nodes(ui.tree).find(n => n.props?.['aria-label'] === 'Audio source').props.onChange({ target: { value: 'system' } });
  nodes(ui.tree).find(n => n.props?.['aria-label'] === 'Local transcription model').props.onChange({ target: { value: 'tiny.en' } });
  let tree = ui.rerender();
  assert.match(JSON.stringify(tree), /all audio on the default output/);
  assert.equal(permissionControl(tree).props.checked, false);
  permissionControl(tree).props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  await button(tree, 'Start listening').props.onClick();
  const request = ui.requests.find(r => r.path === '/start');
  assert.equal(request.body.backend, 'native');
  assert.equal(request.body.audio_source, 'system');
  assert.equal(request.body.transcription_model, 'tiny.en');
  assert.equal(request.body.transcription_provider, 'local');
  assert.doesNotMatch(JSON.stringify(tree), /Cloud transcription: meeting audio is sent/);
});

test('Windows resume ignores the hidden macOS provider and ASR settings', async () => {
  const store = new Map([['meeting-copilot.scope.v2', JSON.stringify({ backend: 'native', audioSource: 'microphone', transcriptionProvider: 'meta', transcriptionChoiceVersion: 1, asrMode: 'DIARIZATION' })]]);
  const ui = pane('/projects/alpha', true, false, '', { 3: true, 4: 'native' }, {}, {
    segments: [{ text: 'Synthetic retained question' }],
    last_session: { backend: 'native', audio_source: 'microphone', transcription_model: 'base.en', transcription_provider: 'local', asr_mode: 'ENDPOINTING' },
    capabilities: { capture_preflight: 1, model_discovery: true, windows_transcription: true },
  }, { localStorage: { getItem: key => store.get(key), setItem() {} }, window: { confirm() { assert.fail('same native settings must resume without clearing'); } } });
  await button(ui.tree, 'Resume listening').props.onClick();
  const request = ui.requests.find(r => r.path === '/start');
  assert.equal(request.body.transcription_provider, 'local');
  assert.equal(request.body.asr_mode, 'ENDPOINTING');
  assert.equal(request.body.new_meeting, false);
});

test('project-only mode sends a request-scoped history opt-out and fails closed on old backends', async () => {
  for (const supported of [true, false]) {
    const ui = pane('/projects/alpha', true, false, '', {}, {}, {capabilities:{all_projects:true, independent_models:true, history_opt_out:supported}});
    const toggle = nodes(ui.tree).find(n => n.type === 'input' && n.props['aria-label'] === 'Exclude past sessions');
    assert.ok(toggle, 'explicit history opt-out is available');
    assert.equal(toggle.props.checked, false, 'existing users retain history by default');
    toggle.props.onChange({target:{checked:true}});
    const tree = ui.rerender();
    const ask = nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer'));
    assert.equal(!!ask.props.disabled, !supported, 'old backends cannot silently ignore opt-out');
    if (supported) {
      await ask.props.onClick();
      assert.equal(ui.requests.find(r => r.path === '/assist').body.include_history, false);
    }
  }
});

test('history exclusion blocks replay of older history-on requests', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {
    capabilities:{all_projects:true, independent_models:true, history_opt_out:true},
    recent:[{request_id:'old',status:'done',question:'Old question?',answer:'Old answer',request:{question:'Old question?',project:'/projects/alpha'}}]
  });
  nodes(ui.tree).find(n => n.type === 'input' && n.props['aria-label'] === 'Exclude past sessions').props.onChange({target:{checked:true}});
  const tree = ui.rerender();
  await nodes(tree).find(n => n.type === 'button' && n.children.includes('Ask again')).props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/assist'), false);
  assert.match(JSON.stringify(ui.rerender()), /saved answer used past sessions/i);
});

test('history-off replay preserves the original request scope', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {
    capabilities:{all_projects:true, independent_models:true, history_opt_out:true},
    recent:[{request_id:'old',status:'done',question:'Synthetic question?',answer:'Synthetic answer',request:{question:'Synthetic question?',project:'/projects/alpha',include_history:false}}]
  });
  nodes(ui.tree).find(n => n.type === 'input' && n.props['aria-label'] === 'Exclude past sessions').props.onChange({target:{checked:true}});
  await nodes(ui.rerender()).find(n => n.type === 'button' && n.children.includes('Ask again')).props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/assist').body.include_history, false);
});

test('history setting cannot be changed while the overlay is shown', () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {overlay_shown:true});
  const toggle = nodes(ui.tree).find(n => n.type === 'input' && n.props['aria-label'] === 'Exclude past sessions');
  assert.equal(toggle.props.disabled, true);
});

test('settings use a short heading and disclose answer data at the action', () => {
  const { tree } = pane('', true, false);
  const summary = nodes(tree).find(n => n.type === 'summary' && JSON.stringify(n.children).includes('Answer setup'));
  assert.match(JSON.stringify(summary.children), /Answer setup/);
  assert.doesNotMatch(JSON.stringify(summary.children), /scope, models, overlay, web/);
  assert.deepEqual(nodes(tree).find(n => n.props.id === 'mc-backend-label').children, ['Capture source']);
  const visible = JSON.stringify(disclosedNodes(tree));
  assert.match(visible, /question.*recent transcript.*selected sources.*sent to this model.*when you ask/i);
  assert.match(visible, /permission.*capture\/read.*meeting/i);
});

test('web settings explain opt-in and public queries where used', () => {
  const ui = pane('', true, false);
  assert.equal(disclosedNodes(ui.tree).some(n => n.props['data-web-fallback-disclosure']), false);
  const setup = nodes(ui.tree).find(n => n.type === 'details' && nodes(n).some(x => x.type === 'summary' && JSON.stringify(x.children).includes('Answer setup')));
  setup.props.onToggle({ currentTarget: { open: true } });
  let tree = ui.rerender();
  const toggle = fallbackControl(tree);
  assert.equal(toggle.props.checked, false);
  const toggleLabel = nodes(tree).find(n => n.type === 'label' && nodes(n).includes(toggle));
  assert.match(JSON.stringify(toggleLabel), /Automatic web fallback.*off/i);
  toggle.props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  const fallbackWarning = disclosedNodes(tree).find(n => n.props['data-web-fallback-disclosure']);
  assert.ok(fallbackWarning, 'fallback warning stays visible at the answer action');
  assert.match(JSON.stringify(fallbackWarning.children), /automatic web fallback.*on.*public query.*filtering.*not.*guarantee/i);
  const webDetails = nodes(tree).find(n => n.type === 'details' && nodes(n).some(x => x.props['aria-label'] === 'Specific public web query'));
  const queryNotice = nodes(webDetails).find(n => n.props['data-web-query-disclosure']);
  assert.ok(queryNotice, 'public query disclosure appears beside its action');
  assert.match(JSON.stringify(queryNotice.children), /query.*sent.*web/i);
  assert.match(JSON.stringify(queryNotice.children), /overrides automatic fallback/i);
  assert.match(JSON.stringify(queryNotice.children), /private/i);
  const openSetup = nodes(tree).find(n => n.type === 'details' && n.props.open && n.props.onToggle);
  openSetup.props.onToggle({ currentTarget: { open: false } });
  assert.ok(disclosedNodes(ui.rerender()).some(n => n.props['data-web-fallback-disclosure']), 'warning remains visible when settings are closed');
});

test('read-only overlay notice remains visible with settings closed', () => {
  const { tree } = pane('', true, false, 'public query');
  assert.match(JSON.stringify(disclosedNodes(tree)), /overlay is read-only.*ask questions here/i);
  assert.doesNotMatch(JSON.stringify(tree), /tap a question to stream/i);
});

test('legacy automatic Meta preference migrates to local without proof', async () => {
  const store = new Map([['meeting-copilot.scope.v2', JSON.stringify({ backend: 'macos', transcriptionProvider: 'meta' })]]);
  const ui = pane('', true, false, '', {}, {}, { capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true } }, { localStorage: { getItem: k => store.get(k), setItem: (k,v) => store.set(k,v) } });
  assert.equal(nodes(ui.tree).find(n => n.props['aria-label'] === 'Transcription provider').props.value, 'local');
});

test('saved explicit unavailable Meta disables capture with actionable guidance', async () => {
  const store = new Map([['meeting-copilot.scope.v2', JSON.stringify({ backend: 'macos', transcriptionProvider: 'meta', transcriptionChoiceVersion: 1 })]]);
  const ui = pane('', true, false, '', { 3: true }, {}, { capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: false } }, { localStorage: { getItem: k => store.get(k), setItem: (k,v) => store.set(k,v) } });
  assert.equal(button(ui.tree, 'Start listening').props.disabled, true);
  assert.match(JSON.stringify(ui.tree), /Meta.*unavailable.*local.*key/i);
  const metaOption = nodes(ui.tree).find(n => n.type === 'option' && n.props.value === 'meta');
  assert.match(metaOption.props.title, /Hermes Settings.*Plugins.*meeting-copilot-live/);
  await button(ui.tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.some(r => r.path === '/start'), false, 'stale callback is also guarded');
});

for (const path of ['/overlay/lifecycle', '/overlay/claim']) test(`initial lifecycle failure never reflects raw sensitive transport text (${path})`, async () => {
  const ui = pane('', true, false, '', {}, { [path]: () => { throw new Error('fake-sensitive-initial-error'); } }, {}, { setInterval: () => 0, clearInterval: () => {} }, { effects: true });
  await settle();
  assert.doesNotMatch(JSON.stringify(ui.rerender()), /fake-sensitive/);
  assert.match(JSON.stringify(ui.rerender()), /reopen.*pane/i);
  ui.unregister();
});

// The plugin-owned credential editor was retired for Hermes catalog rule 11.
// Setup must be read-only, including across all pane lifecycle boundaries.
test('BYOK setup points to Hermes and never renders credential entry or makes credential calls', () => {
  const ui=pane('alpha',true,false);
  const tree=ui.tree;
  assert.match(JSON.stringify(tree), /Hermes Settings.*Plugins.*meeting-copilot-live/);
  assert.match(JSON.stringify(tree), /Never enter keys in chat or model fields/);
  assert.ok(!nodes(tree).some(n=>n.props?.type==='password'));
  assert.ok(!ui.requests.some(r=>r.path.startsWith('/credentials')));
  ui.unmount();ui.unregister();
  assert.ok(!ui.requests.some(r=>r.path.startsWith('/credentials')));
});
test('fresh panes cannot submit an unavailable Hermes default and keep history excluded', async () => {
  const ui=pane('alpha',true,false,'',{}, {}, {}, {}, {freshDefaults:true});
  const toggle=nodes(ui.tree).find(n=>n.props['aria-label']==='Exclude past sessions');
  assert.equal(toggle.props.checked,true);
  const suggest=button(ui.tree,'Suggest answer');
  assert.equal(suggest.props.disabled,true);
  await suggest.props.onClick();
  assert.ok(!ui.requests.some(r=>r.path==='/assist'));
});

for (const storageKey of ['meeting-copilot.scope.v2','meeting-copilot.scope']) test(`persisted unsupported Meta selection clears visibly and cannot submit (${storageKey})`, async () => {
  const store=new Map([[storageKey,JSON.stringify({answerProvider:'meta',answerModel:'muse-spark-1.3'})]]);
  const ui=pane('alpha',true,false,'',{}, {}, {}, {localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)}}, {freshDefaults:true});
  const provider=nodes(ui.tree).find(n=>n.props['aria-labelledby']==='mc-provider-label');
  assert.equal(provider.props.value,'');
  assert.ok(!nodes(ui.tree).some(n=>n.props['aria-label']==='Answer model'));
  assert.equal(button(ui.tree,'Suggest answer').props.disabled,true);
  assert.equal(button(ui.tree,'Show overlay').props.disabled,false);
  await button(ui.tree,'Suggest answer').props.onClick();
  assert.ok(!ui.requests.some(r=>r.path==='/assist'));
});
for (const provider of ['openai','openrouter']) test(`valid saved BYOK selection stays visible and submits exactly (${provider})`, async () => {
  const store=new Map([['meeting-copilot.scope.v2',JSON.stringify({answerProvider:provider,answerModel:'vendor/exact-model',excludeHistory:false})]]);
  const ui=pane('alpha',true,false,'',{}, {}, {}, {localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)}}, {freshDefaults:true,providers:[{id:provider,label:provider,custom_model:true,configured:true}]});
  assert.equal(nodes(ui.tree).find(n=>n.props['aria-labelledby']==='mc-provider-label').props.value,provider);
  assert.equal(nodes(ui.tree).find(n=>n.props['aria-label']==='Answer model').props.value,'vendor/exact-model');
  assert.equal(button(ui.tree,'Suggest answer').props.disabled,false);
  await button(ui.tree,'Suggest answer').props.onClick();
  const request=ui.requests.find(r=>r.path==='/assist');
  assert.equal(request.body.answer_provider,provider);
  assert.equal(request.body.answer_model,'vendor/exact-model');
});
test('unsupported provider blocks Suggest but permits the read-only overlay', async () => {
  const ui=pane('alpha',true,false,'',{12:'openrouter',13:'vendor/exact-model'});
  assert.equal(button(ui.tree,'Suggest answer').props.disabled,true);
  assert.equal(button(ui.tree,'Show overlay').props.disabled,false);
  await button(ui.tree,'Suggest answer').props.onClick();
  assert.ok(!ui.requests.some(r=>r.path==='/assist'));
});

// Effect-capable lifecycle tests use deterministic timers (no React package).
function lifecyclePane(respond = {}, settings = {}, statusExtra = {}) {
  const timers = new Map(); let next = 0;
  const p = pane('alpha', true, false, '', settings, {
    '/overlay/lifecycle': { revision: 0, epoch: 'backend-epoch' },
    '/overlay/claim': { pane: 'pane-capability' },
    '/overlay/show': { shown: true, owner: 'show-owner' }, ...respond,
  }, statusExtra, {
    setInterval: () => 0, clearInterval: () => {},
    setTimeout: fn => { timers.set(++next, fn); return next; },
    clearTimeout: id => timers.delete(id),
  }, { effects: true });
  return { ...p, async flushTimers() { const work = [...timers.values()]; timers.clear(); for (const fn of work) await fn(); } };
}
function button(tree, label) { return nodes(tree).find(n => n.type === 'button' && n.children.includes(label)); }

function alertFor(tree, message) {
  return nodes(tree).find(n => n.props?.role === 'alert' && JSON.stringify(n.children).includes(message));
}

test('unclassified legacy errors only recheck even with prior backend and stale retry data', async () => {
  for (const priorBackend of ['native', 'macos', 'meetily']) {
    const status = { backend: priorBackend, listening: false,
      error: 'Unclassified legacy failure',
      error_request: { question: 'Old question', transcript: 'Private old transcript' } };
    const p = lifecyclePane({}, { 3: true, 4: 'macos' }, status);
    try {
      const alert = alertFor(p.rerender(), 'Answer failed. Check answer setup');
      assert.ok(alert);
      assert.doesNotMatch(JSON.stringify(p.rerender()), /Unclassified legacy failure/);
      assert.ok(!button(alert, 'Restart capture'), 'legacy summary cannot authorize capture');
      assert.ok(!button(alert, 'Retry'), 'retained request cannot authorize answer retry');
      const recheck = button(alert, 'Recheck status');
      assert.ok(recheck);
      assert.equal(recheck.props.disabled, false);
      await recheck.props.onClick();
      assert.ok(!p.requests.some(r => ['/start', '/assist'].includes(r.path)));
    } finally { p.unmount(); }
  }
});

test('capture error never replays stale answer data after source changes', async () => {
  const status = { error: 'Answer failed', answer_error: 'Answer failed', error_source: 'answer',
    error_request: { question: 'Old question', transcript: 'Private old transcript' }, backend: 'macos' };
  const p = lifecyclePane({}, { 3: true, 4: 'macos' }, status);
  assert.ok(button(alertFor(p.rerender(), 'Answer failed'), 'Retry'));
  Object.assign(status, { error: 'Microphone failed', answer_error: null, error_source: 'capture',
    capture_error: 'Microphone failed', listening: false });
  let tree = p.rerender();
  const capture = alertFor(tree, 'Microphone failed');
  assert.ok(button(capture, 'Restart capture'));
  assert.ok(!button(capture, 'Retry'));
  await button(capture, 'Restart capture').props.onClick();
  assert.ok(p.requests.some(r => r.path === '/start'));
  assert.ok(!p.requests.some(r => r.path === '/assist'));
  Object.assign(status, { error: 'System capture failed', capture_error: 'System capture failed', listening: true });
  tree = p.rerender();
  await button(alertFor(tree, 'System capture failed'), 'Recheck status').props.onClick();
  assert.ok(!p.requests.some(r => r.path === '/assist'));
  p.unmount();
});

test('progress overlay and forget boundary errors keep their own messages and actions', async () => {
  const overlay = 'Overlay shutdown is not confirmed; retry Hide.';
  const boundary = 'Could not install the Meetily forget boundary: retry Clear.';
  const progress = 'Answer progress unavailable: network.';
  const status = { error: overlay, overlay_cleanup_error: overlay, meetily_clear_error: boundary,
    error_request: { question: 'Stale question', transcript: 'Private stale transcript' }, listening: false };
  const p = lifecyclePane({}, { 6: progress }, status);
  let tree = p.rerender();
  assert.ok(button(alertFor(tree, boundary), 'Clear again'));
  assert.ok(button(alertFor(tree, overlay), 'Retry hide overlay'));
  await button(alertFor(tree, progress), 'Recheck status').props.onClick();
  tree = p.rerender();
  assert.ok(alertFor(tree, progress), 'rechecking is not evidence progress recovered');
  assert.ok(button(alertFor(tree, boundary), 'Clear again'));
  assert.ok(button(alertFor(tree, overlay), 'Retry hide overlay'));
  assert.doesNotMatch(JSON.stringify(tree), /Backend unavailable: undefined/);
  await button(alertFor(tree, boundary), 'Clear again').props.onClick();
  assert.ok(p.requests.some(r => r.path === '/clear'));
  assert.ok(!p.requests.some(r => r.path === '/assist'));
  assert.ok(alertFor(p.rerender(), overlay));
  p.unmount();
});

test('simultaneous answer capture stop boundary and backend errors stay independently actionable', async () => {
  const messages = { answer_error: 'Answer provider failed', capture_error: 'Microphone failed',
    stop_error: 'Capture shutdown unconfirmed', overlay_cleanup_error: 'Overlay cleanup denied',
    meetily_clear_error: 'Meetily forget boundary failed' };
  const stored = { question: 'Actual failed question', transcript: 'Exact failed transcript' };
  const p = pane('', true, false, '', {}, {}, { ...messages, error: messages.stop_error,
    error_source: 'stop', stopping: true, error_request: stored }, {},
    { statusError: new Error('Connection lost') });
  for (const [key, message] of Object.entries(messages)) {
    const label = key === 'answer_error' ? 'Retry' : key === 'meetily_clear_error' ? 'Clear again'
      : key === 'overlay_cleanup_error' ? 'Retry hide overlay' : 'Recheck status';
    assert.ok(button(alertFor(p.tree, key === 'answer_error' ? 'Answer failed. Check answer setup' : message), label), `${key} must pair its own action`);
  }
  assert.ok(button(alertFor(p.tree, 'Backend unavailable. Check the backend connection'), 'Retry'));
  assert.doesNotMatch(JSON.stringify(p.tree), /Answer provider failed|Connection lost/);
  await button(alertFor(p.tree, 'Answer failed. Check answer setup'), 'Retry').props.onClick();
  const assist = p.requests.find(r => r.path === '/assist');
  assert.equal(assist.body.question, stored.question);
  assert.equal(assist.body.transcript, stored.transcript);
});

test('legacy answer diagnostics are private without granting stale retry authority', async () => {
  const p = pane('', true, false, '', {}, {}, {
    error: 'synthetic-sensitive-legacy-provider-error',
    error_request: { question: 'Old question', transcript: 'Old private context' },
  });
  const alert = alertFor(p.tree, 'Answer failed. Check answer setup');
  assert.ok(alert);
  assert.doesNotMatch(JSON.stringify(p.tree), /synthetic-sensitive-legacy-provider-error/);
  assert.ok(!button(alert, 'Retry'));
  await button(alert, 'Recheck status').props.onClick();
  assert.ok(!p.requests.some(r => r.path === '/assist'));
});

test('non-answer local error replaces failed assist without retaining its retry', async () => {
  const p = pane('', true, false, '', { 1: 'Old question', 3: true, 4: 'macos' }, {
    '/assist': () => { throw new Error('Answer submission failed'); },
    '/start': () => { throw new Error('Microphone could not start'); },
  });
  await button(p.tree, 'Suggest answer').props.onClick();
  let tree = p.rerender();
  assert.ok(button(alertFor(tree, 'Answer submission was not confirmed'), 'Recheck status'));
  assert.doesNotMatch(JSON.stringify(tree), /Answer submission failed/);
  await button(tree, 'Start listening').props.onClick();
  tree = p.rerender();
  const capture = alertFor(tree, 'Start was not confirmed');
  assert.doesNotMatch(JSON.stringify(tree), /Microphone could not start/);
  assert.ok(button(capture, 'Dismiss'));
  assert.ok(!button(capture, 'Retry'));
  await button(capture, 'Dismiss').props.onClick();
  assert.equal(p.requests.filter(r => r.path === '/assist').length, 1);
});

test('local progress error and overlay cleanup have independent paired actions', async () => {
  const warning = 'Overlay shutdown is not confirmed; retry Hide.';
  const progress = 'Answer progress unavailable: network. The result still lands below when it completes.';
  const p = lifecyclePane({}, { 3: true, 4: 'macos', 6: progress }, {
    error: warning, overlay_cleanup_error: warning, overlay_stop_confirmed: false,
    listening: false, backend: 'macos',
  });
  let tree = p.rerender();
  const alerts = nodes(tree).filter(n => n.props?.role === 'alert');
  const local = alerts.find(n => JSON.stringify(n).includes(progress));
  const overlay = alerts.find(n => JSON.stringify(n).includes(warning));
  assert.ok(local && overlay && local !== overlay);
  assert.ok(button(local, 'Recheck status'));
  assert.ok(!button(local, 'Retry hide overlay'));
  assert.ok(button(overlay, 'Retry hide overlay'));
  assert.equal(alerts.filter(n => JSON.stringify(n).includes(warning)).length, 1, 'the warning is visible once, with its paired retry');
  assert.equal(button(tree, 'Start listening').props.disabled, true);
  await button(local, 'Recheck status').props.onClick();
  tree = p.rerender();
  assert.match(JSON.stringify(tree), /Answer progress unavailable/);
  assert.ok(!p.requests.some(r => r.path === '/assist'), 'progress recheck never replays');
  assert.match(JSON.stringify(tree), /Overlay shutdown is not confirmed/);
  assert.ok(!p.requests.some(r => r.path === '/overlay/hide'));
  await button(nodes(tree).find(n => n.props?.role === 'alert' && JSON.stringify(n).includes(warning)), 'Retry hide overlay').props.onClick();
  assert.equal(p.requests.filter(r => r.path === '/overlay/hide').length, 1);
  p.unmount();
});

test('attributed capture restart preserves cloud availability and overlay safety gates', async () => {
  for (const overlayUncertain of [false, true]) {
    const store = new Map([['meeting-copilot.scope.v2', JSON.stringify({ backend: 'macos', transcriptionProvider: overlayUncertain ? 'local' : 'meta', transcriptionChoiceVersion: 1 })]]);
    const ui = pane('', true, false, '', { 3: true }, {}, {
      error_source: 'capture', capture_error: 'Microphone failed',
      ...(overlayUncertain ? { overlay_cleanup_error: 'Overlay shutdown is not confirmed' } : {}),
      capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: false },
    }, { localStorage: { getItem: k => store.get(k), setItem: (k, v) => store.set(k, v) } });
    const restart = button(alertFor(ui.tree, 'Microphone failed'), 'Restart capture');
    assert.ok(restart);
    assert.equal(restart.props.disabled, true);
    await restart.props.onClick();
    assert.ok(!ui.requests.some(r => r.path === '/start'), 'handler must also refuse blocked capture');
    assert.ok(!ui.requests.some(r => r.path === '/assist'));
  }
});

test('pane Pause and Clear carry lifecycle authorization; unregister only releases', async () => {
  const p = lifecyclePane({}, {}, { listening: true, backend: 'macos', segments: [{ text: 'Meeting' }] });
  await button(p.tree, 'Pause listening').props.onClick();
  await button(p.rerender(), 'Clear meeting').props.onClick();
  p.unregister();
  await Promise.resolve();
  const pause = p.requests.find(r => r.path === '/pause');
  const clear = p.requests.find(r => r.path === '/clear');
  assert.equal(pause.body.pane, 'pane-capability');
  assert.equal(clear.body.pane, pause.body.pane);
  assert.ok(clear.body.action > pause.body.action);
  assert.equal(p.requests.filter(r => r.path === '/pause').length, 1);
  const release = p.requests.find(r => r.path === '/overlay/release');
  assert.equal(release.body.pane, pause.body.pane);
  assert.ok(release.body.action > clear.body.action);
  p.unmount();
  assert.equal(p.requests.filter(r => r.path === '/overlay/release').length, 1);
});

for (const failedPath of ['/overlay/lifecycle', '/overlay/claim']) test(`failed lifecycle lookup/claim shows reopen guidance (${failedPath})`, async () => {
  const p = lifecyclePane({ [failedPath]: () => { throw new Error('network unavailable'); } });
  await button(p.tree, 'Show overlay').props.onClick();
  const text = JSON.stringify(p.rerender());
  assert.doesNotMatch(text, /network unavailable/, 'transport details stay private');
  assert.match(text, /Overlay action not confirmed\.[^"]*[Cc]lose and reopen this pane/);
  await button(p.rerender(), 'Show overlay').props.onClick();
  const expected = failedPath === '/overlay/lifecycle' ? 2 : 1;
  assert.equal(p.requests.filter(r => r.path === '/overlay/lifecycle').length, expected);
  assert.equal(p.requests.filter(r => r.path === failedPath).length, expected);
  assert.ok(!p.requests.some(r => r.path === '/overlay/show'));
  p.unmount();
});

test('empty-session source switch immediately retires local overlay', async () => {
  const p = lifecyclePane({}, { 3: true, 4: 'macos', 11: 'system' }, {
    last_session: { backend: 'macos', transcription_model: 'base.en', audio_source: 'microphone' },
  });
  await button(p.tree, 'Show overlay').props.onClick();
  assert.ok(button(p.rerender(), 'Hide overlay'));
  await button(p.rerender(), 'Start listening').props.onClick();
  assert.ok(button(p.rerender(), 'Show overlay'));
  assert.ok(!button(p.rerender(), 'Hide overlay'));
  p.unmount();
});

test('real effects do not auto-sync unchanged Show scope', async () => {
  const p = lifecyclePane();
  await button(p.tree, 'Show overlay').props.onClick();
  p.rerender();
  await p.flushTimers();
  assert.equal(p.requests.filter(r => r.path === '/overlay/scope').length, 0);
  p.unmount();
});

test('real effects synchronize an adversarially forced scope with its owner', async () => {
  const p = lifecyclePane({ '/overlay/scope': { accepted: true } });
  await button(p.tree, 'Show overlay').props.onClick();
  let tree = p.rerender();
  p.forcePreferences({ project: '' }); // bypass the public guard to exercise sync defense-in-depth
  tree = p.rerender();
  await p.flushTimers();
  const syncs = p.requests.filter(r => r.path === '/overlay/scope');
  assert.equal(syncs.length, 1);
  assert.equal(syncs[0].body.scope.owner, 'show-owner');
  assert.equal(syncs[0].body.scope.project, null);
  assert.equal(syncs[0].body.scope.all_projects, false);
  assert.ok(syncs[0].body.scope.seq > 0);
  p.unmount();
});

test('unmount fences a delayed Show and never sends global hide/pause', async () => {
  let resolve;
  const p = lifecyclePane({ '/overlay/show': () => new Promise(r => { resolve = r; }) });
  const pending = button(p.tree, 'Show overlay').props.onClick();
  await settle();
  assert.ok(resolve, 'Show must have reached backend before cleanup');
  p.unmount();
  resolve({ shown: true, owner: 'old-show-owner' });
  await pending;
  const show = p.requests.find(r => r.path === '/overlay/show');
  const releases = p.requests.filter(r => r.path === '/overlay/release');
  assert.ok(releases.length, 'cleanup sends ordered release even before Show responds');
  assert.equal(releases[0].body.pane, show.body.pane);
  assert.ok(releases[0].body.action > show.body.action);
  assert.ok(!p.requests.some(r => r.path === '/pause' || (r.path === '/overlay/hide' && !r.body.pane)));
});

test('unmount while claiming ownership cleans exact capability without showing', async () => {
  let resolve;
  const p = lifecyclePane({ '/overlay/claim': () => new Promise(r => { resolve = r; }) });
  const pending = button(p.tree, 'Show overlay').props.onClick();
  for (let i = 0; i < 12 && !resolve; i++) await Promise.resolve();
  assert.ok(resolve, 'ownership request is pending');
  p.unmount();
  resolve({ pane: 'late-capability' });
  await pending;
  assert.ok(!p.requests.some(r => r.path === '/overlay/show'));
  assert.equal(p.requests.find(r => r.path === '/overlay/release')?.body.pane, 'late-capability');
});

test('effect remount gets a fresh capability; old cleanup never releases the new one', async () => {
  let claims = 0;
  const p = lifecyclePane({ '/overlay/claim': () => ({ pane: `pane-${++claims}` }) });
  for (let i = 0; i < 12; i++) await Promise.resolve();
  const tree = p.replayEffects();
  await button(tree, 'Show overlay').props.onClick();
  assert.equal(claims, 2);
  assert.equal(p.requests.find(r => r.path === '/overlay/show').body.pane, 'pane-2');
  assert.deepEqual(p.requests.filter(r => r.path === '/overlay/release').map(r => r.body.pane), ['pane-1']);
  p.unmount();
  assert.deepEqual(p.requests.filter(r => r.path === '/overlay/release').map(r => r.body.pane), ['pane-1', 'pane-2']);
});

async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
function changeProject(p, value) {
  // Force a corrupt/external mutation, not an allowed UI preference change.
  p.forcePreferences({ project: value });
  return p.rerender();
}

test('pending lifecycle cannot start capture after pane closes', async () => {
  let resolve;
  const p = lifecyclePane({ '/overlay/lifecycle': () => new Promise(r => { resolve = r; }) }, { 3: true, 4: 'macos' });
  const pending = button(p.tree, 'Start listening').props.onClick();
  await settle();
  const earlyStarts = p.requests.filter(r => r.path === '/start');
  p.unmount();
  resolve({ revision: 0 });
  await pending;
  assert.equal(earlyStarts.length, 0, 'capture waits for an owner capable of safe cleanup');
  assert.ok(!p.requests.some(r => ['/start', '/pause', '/overlay/claim'].includes(r.path)));
});

test('started capture carries the capability and cleanup supersedes it', async () => {
  const p = lifecyclePane({}, { 3: true, 4: 'macos' });
  await button(p.tree, 'Start listening').props.onClick();
  p.unmount();
  const start = p.requests.find(r => r.path === '/start');
  const release = p.requests.find(r => r.path === '/overlay/release');
  assert.equal(start.body.pane, 'pane-capability');
  assert.ok(release.body.action > start.body.action);
});

test('superseded plugin registration cannot commit its delayed lifecycle lookup', async () => {
  const shared = { crypto: webcrypto };
  const globals = { globalThis: shared, setInterval: () => 0, clearInterval: () => {} };
  let deliverOld;
  const older = pane('', true, false, '', {}, {
    '/overlay/lifecycle': () => new Promise(resolve => { deliverOld = resolve; }),
  }, {}, globals, { effects: true });
  await settle();
  assert.equal(typeof deliverOld, 'function');
  const newer = pane('', true, false, '', {}, {}, {}, globals, { effects: true });
  await button(newer.tree, 'Show overlay').props.onClick();
  // The newer pane has claimed before the old GET is even handled.
  deliverOld({ revision: 1, epoch: 'backend-epoch' });
  await settle();
  assert.ok(!older.requests.some(r => r.path === '/overlay/claim'), 'retired registration cannot commit');
  const newClaim = newer.requests.find(r => r.path === '/overlay/claim').body;
  assert.equal(newClaim.creation, 2, 'creation identity is assigned before either GET finishes');
  assert.equal(newClaim.epoch, 'backend-epoch');
  older.rerender();
  older.rerender();
  older.unregister();
  newer.unregister();
  assert.ok(!older.requests.some(r => r.path === '/pause'));
  assert.ok(!newer.requests.some(r => r.path === '/pause'));
});

test('legacy retry tickets never trigger a new lifecycle claim', async () => {
  const p = lifecyclePane({ '/overlay/claim': { accepted: false, retry_revision: 1 } });
  await button(p.tree, 'Show overlay').props.onClick();
  assert.equal(p.requests.filter(r => r.path === '/overlay/claim').length, 1);
  assert.equal(p.requests.filter(r => r.path === '/overlay/lifecycle').length, 1);
  assert.ok(!p.requests.some(r => r.path === '/overlay/show'));
  p.unmount();
});

test('claim conflict with a newer owner never retries or starts capture', async () => {
  const p = lifecyclePane({ '/overlay/claim': { accepted: false } }, { 3: true, 4: 'macos' });
  await button(p.tree, 'Start listening').props.onClick();
  assert.equal(p.requests.filter(r => r.path === '/overlay/claim').length, 1);
  assert.ok(!p.requests.some(r => r.path === '/start'));
  p.unmount();
});

for (const manual of [false, true]) test(`restoring narrow scope supersedes intervening broad request (manual=${manual})`, async () => {
  const resolveBroad = []; let backendScope = { project: 'alpha', seq: 0 };
  const p = lifecyclePane({ '/overlay/scope': (_, { body }) => {
    const scope = body.scope;
    if (scope.seq >= backendScope.seq) backendScope = scope;
    if (scope.all_projects) return new Promise(r => { resolveBroad.push(r); });
    return { accepted: true };
  } });
  await button(p.tree, 'Show overlay').props.onClick();
  let tree = changeProject(p, '__all_projects__');
  let broad;
  if (manual) broad = button(tree, 'Sync scope').props.onClick();
  else broad = p.flushTimers();
  await settle();
  assert.ok(resolveBroad.length);
  changeProject(p, 'alpha');
  await p.flushTimers();
  for (const resolve of resolveBroad) resolve({ accepted: true });
  await broad;
  assert.equal(backendScope.project, 'alpha');
  assert.equal(backendScope.all_projects, false);
  assert.equal(p.requests.filter(r => r.path === '/overlay/scope').at(-1).body.scope.project, 'alpha');
  p.unmount();
});

for (const manual of [false, true]) test(`acknowledged broad scope can return to original narrow scope (manual=${manual})`, async () => {
  let backendScope;
  const p = lifecyclePane({ '/overlay/scope': (_, { body }) => {
    backendScope = body.scope;
    return { accepted: true };
  } });
  await button(p.tree, 'Show overlay').props.onClick();
  changeProject(p, '__all_projects__');
  await settle();
  if (manual) await button(p.rerender(), 'Sync scope').props.onClick();
  assert.equal(backendScope.all_projects, true);
  changeProject(p, 'alpha');
  await settle();
  assert.equal(backendScope.project, 'alpha');
  assert.equal(backendScope.all_projects, false);
  assert.ok(!JSON.stringify(p.rerender()).includes('Settings changed — syncing'));
  p.unmount();
});

test('effect-capable Show then first Start retains coherent Hide UI', async () => {
  const p = lifecyclePane({}, { 3: true, 4: 'macos' });
  await button(p.tree, 'Show overlay').props.onClick();
  await button(p.rerender(), 'Start listening').props.onClick();
  await settle();
  assert.ok(button(p.rerender(), 'Hide overlay'));
  const shown = p.requests.find(r => r.path === '/overlay/show');
  const started = p.requests.find(r => r.path === '/start');
  assert.equal(started.body.pane, shown.body.pane);
  assert.ok(started.body.action > shown.body.action);
  assert.ok(!p.requests.some(r => ['/overlay/scope', '/overlay/hide', '/overlay/release'].includes(r.path)));
  p.unmount();
});

test('scope failure retires overlay rather than retaining broader permissions', async () => {
  const p = lifecyclePane({ '/overlay/scope': () => { throw new Error('sync failed'); } });
  await button(p.tree, 'Show overlay').props.onClick();
  changeProject(p, '__all_projects__');
  await p.flushTimers();
  await settle();
  assert.ok(p.requests.some(r => r.path === '/overlay/hide' && r.body.pane === 'pane-capability'));
  assert.ok(button(p.rerender(), 'Show overlay'));
  p.unmount();
});

test('Clear retires local Hide state and permits immediate Show', async () => {
  const p = lifecyclePane({}, {}, { segments: [{ text: 'Who approves?' }] });
  await button(p.tree, 'Show overlay').props.onClick();
  await button(p.rerender(), 'Clear meeting').props.onClick();
  const reopen = button(p.rerender(), 'Show overlay');
  assert.ok(reopen, 'Clear already retired the overlay, without an eight-second grace');
  await reopen.props.onClick();
  assert.equal(p.requests.filter(r => r.path === '/overlay/show').length, 2);
  assert.equal(p.requests.filter(r => r.path === '/overlay/hide').length, 0);
  p.unmount();
});

function nodes(tree) {
  return tree && typeof tree === 'object' ? [tree, ...(tree.children || []).flatMap(nodes)] : [];
}

function fallbackControl(tree) {
  const label = nodes(tree).find(n => n.type === 'label' && JSON.stringify(n.children).includes('Automatic web fallback:'));
  return nodes(label).find(n => n.type === 'input' && n.props.type === 'checkbox');
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

test('restart command releases owned capture and reopens exactly one pane', async () => {
  const { contributions, revealed, requests } = lifecyclePane();
  await settle();
  const restart = contributions.find(c => c.area === 'palette' && c.data.label === 'Restart Meeting Copilot');
  assert.ok(restart, 'A restart command must be discoverable in Cmd+K');
  await restart.data.run();
  assert.ok(requests.some(r => r.path === '/overlay/release' && r.body.pane), 'Restart must release owned capture first');
  assert.ok(!requests.some(r => r.path === '/pause'), 'never fall back to legacy Pause');
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
  const caps = { capabilities: { all_projects: true, automatic_web_fallback: true, independent_models: true, history_opt_out: true, capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true, overlay: true, overlay_stream: true, overlay_cancel: true } };
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

test('capture permission exposes the complete visible consent to assistive technology', () => {
  const ui = pane('', true, false, '', { 3: false, 4: 'macos' });
  const capture = nodes(ui.tree).find(n => n.type === 'section' && n.props['aria-label'] === 'Capture session');
  const label = nodes(capture).find(n => n.type === 'label'
    && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
  assert.ok(label, 'the consent text must be part of the checkbox label');
  const input = nodes(label).find(n => n.type === 'input' && n.props.type === 'checkbox');
  assert.ok(input, 'the checkbox must be associated with its visible consent');
  assert.equal(input.props['aria-label'], undefined, 'a short aria-label must not override the visible consent');
  assert.equal(input.props['aria-labelledby'], undefined, 'the full visible label must be the accessible name');
  assert.match(JSON.stringify(label.children), /use its context with my configured model/);
  assert.equal(input.props.checked, false);
  assert.equal(button(ui.tree, 'Start listening').props.disabled, true);
  input.props.onChange({ target: { checked: true } });
  assert.equal(button(ui.rerender(), 'Start listening').props.disabled, false);
  assert.equal(ui.requests.length, 0, 'checking consent alone does not initiate capture');
});

test('capture consent is described by the cloud disclosure only when cloud audio is selected', () => {
  for (const [backend, provider, expected] of [['macos', 'meta', true], ['macos', 'local', false], ['meetily', 'meta', false]]) {
    const { tree } = pane('', true, false, '', { 4: backend, 25: provider });
    const capture = nodes(tree).find(n => n.type === 'section' && n.props['aria-label'] === 'Capture session');
    const label = nodes(capture).find(n => n.type === 'label'
      && JSON.stringify(n.children).includes('I have permission to capture/read this meeting'));
    const input = nodes(label).find(n => n.type === 'input' && n.props.type === 'checkbox');
    const notice = nodes(capture).find(n => n.props['data-cloud-capture-disclosure'] === true);
    if (expected) {
      assert.ok(notice?.props.id, 'the cloud disclosure must have a linkable identity');
      assert.equal(input.props['aria-describedby'], notice.props.id, 'focus on consent must announce cloud audio transmission');
      assert.match(JSON.stringify(notice.children), /meeting audio is sent to Meta/i);
    } else {
      assert.equal(notice, undefined, `${backend}/${provider} must not announce cloud audio`);
      assert.equal(input.props['aria-describedby'], undefined, `${backend}/${provider} must not reference an absent disclosure`);
    }
  }
});

test('web controls expose their privacy disclosure to assistive technology', () => {
  const ui = pane('', true, false, '', {});
  let tree = ui.tree;
  let label = nodes(tree).find(n => n.type === 'label'
    && JSON.stringify(n.children).includes('Automatic web fallback: off'));
  let checkbox = nodes(label).find(n => n.type === 'input' && n.props.type === 'checkbox');
  assert.ok(checkbox, 'the fallback checkbox must be inside its visible label');
  assert.equal(checkbox.props['aria-label'], undefined, 'a short aria-label must not override the visible privacy text');
  checkbox.props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  label = nodes(tree).find(n => n.type === 'label'
    && JSON.stringify(n.children).includes('Automatic web fallback: on'));
  assert.match(JSON.stringify(label.children), /public query.*filtering.*not a privacy guarantee/i);
  checkbox = nodes(label).find(n => n.type === 'input' && n.props.type === 'checkbox');
  assert.equal(checkbox.props.checked, true);
  const input = nodes(tree).find(n => n.props['aria-label'] === 'Specific public web query');
  const notice = nodes(tree).find(n => n.props['data-web-query-disclosure']);
  assert.ok(notice?.props.id, 'the explicit-query warning must have a linkable identity');
  assert.equal(input.props['aria-describedby'], notice.props.id, 'the query control must describe what leaves the device');
  assert.match(JSON.stringify(notice.children), /query is sent to the web.*Do not include private details/i);
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

function disclosedNodes(node) {
  if (!node || typeof node !== 'object') return [];
  return [{ ...node, children: (node.children || []).filter(child => typeof child !== 'object') },
    ...(node.type === 'details' && !node.props.open ? [] : (node.children || []).flatMap(disclosedNodes))];
}

for (const timing of ['cached', 'delayed']) test(`local capture default is independent of capability timing (${timing})`, async () => {
  const store = new Map();
  const status = { capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: timing === 'cached' } };
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, status, {
    localStorage: { getItem: key => store.get(key) || null, setItem: (key, value) => store.set(key, value) },
    setInterval: () => 0, clearInterval: () => {},
  }, { effects: true });
  await settle(); ui.rerender();
  status.capabilities.meta_asr = true;
  ui.rerender(); await settle();
  const tree = ui.rerender();
  assert.equal(nodes(tree).find(n => n.props['aria-label'] === 'Transcription provider').props.value, 'local');
  await button(tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/start').body.transcription_provider, 'local');
  ui.unmount();
});

test('explicit persisted cloud preference survives unrelated preference edits and discloses at Start', async () => {
  const store = new Map([['meeting-copilot.scope.v2', JSON.stringify({ transcriptionProvider: 'meta', transcriptionChoiceVersion: 1 })]]);
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, { capabilities: { capture_preflight: 1, model_discovery: true, macos_transcription: true, meta_asr: true } }, {
    localStorage: { getItem: key => store.get(key) || null, setItem: (key, value) => store.set(key, value) },
    setInterval: () => 0, clearInterval: () => {},
  }, { effects: true });
  await settle();
  let tree = ui.rerender();
  const fallback = nodes(tree).find(n => n.type === 'input' && n.props['aria-label'] === 'Allow automatic web fallback');
  if (fallback) fallback.props.onChange({ target: { checked: true } });
  // Change a real common preference, not the provider itself.
  nodes(tree).find(n => n.props['aria-label'] === 'Audio source').props.onChange({ target: { value: 'system' } });
  tree = ui.rerender();
  assert.equal(JSON.parse(store.get('meeting-copilot.scope.v2')).transcriptionProvider, 'meta');
  assert.match(JSON.stringify(disclosedNodes(tree)), /meeting audio is sent to Meta/);
  assert.doesNotMatch(JSON.stringify(tree), /Transcription remains local/);
  assert.equal(permissionControl(tree).props.checked, false);
  permissionControl(tree).props.onChange({ target: { checked: true } });
  tree = ui.rerender();
  await button(tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.find(r => r.path === '/start').body.transcription_provider, 'meta');
  ui.unmount();
});

test('overlay privacy and shortcut remain beside the control; detailed guidance is in Help', () => {
  const ui = pane('', true, false, '', {}, {}, {}, {}, { settingsHost: true });
  const capture = nodes(ui.tree).find(n => n.props['aria-label'] === 'Capture session');
  const text = JSON.stringify(disclosedNodes(capture));
  assert.match(text, /⌃⌥⌘O/);
  assert.match(text, /exclusion.*best.effort.*not a guarantee/i);
  assert.doesNotMatch(text, /drag.*position presets/i);
  const help = nodes(ui.tree).find(n => n.type === 'details' && nodes(n).some(x => x.type === 'summary' && x.children.includes('Help')));
  assert.ok(help && !help.props.open);
  assert.match(JSON.stringify(help), /drag.*position presets/i);
  assert.match(JSON.stringify(help), /Show in recordings/);
  assert.ok(button(capture, 'Show overlay'));
  assert.equal(ui.requests.some(r => r.path === '/start' || r.path === '/assist'), false);
});

test('quick overlay failure and screen-share privacy warning are disclosed without opening details', async () => {
  const ui = pane('', true, false, '', {}, {
    '/overlay/show': () => { throw new Error('fake-sensitive-transport-detail'); },
  });
  await button(ui.tree, 'Show overlay').props.onClick();
  const capture = nodes(ui.rerender()).find(n => n.props['aria-label'] === 'Capture session');
  const text = JSON.stringify(disclosedNodes(capture));
  assert.match(text, /screen.shar/i);
  assert.match(text, /overlay.*not confirmed/i);
  assert.match(text, /Retry|reopen/i);
  assert.doesNotMatch(JSON.stringify(ui.rerender()), /fake-sensitive-transport-detail/);
  assert.equal(ui.requests.filter(r => r.path === '/overlay/show').length, 1);
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
  const { tree, requests } = pane('', true, false, '', { 12: 'openai', 13: 'gpt-test' });
  assert.ok(nodes(tree).find(n => n.type === 'input' && n.props['aria-label'] === 'Answer model' && n.props.value === 'gpt-test'));
  await nodes(tree).find(n => n.type === 'button' && n.children.includes('Suggest answer')).props.onClick();
  const body = requests.find(r => r.path === '/assist').body;
  assert.equal(body.answer_provider, 'openai');
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
  const separate = pane('', true, false, 'public product documentation');
  await button(separate.tree, 'Check sources + web').props.onClick();
  assert.equal(separate.requests.at(-1).body.web_query, 'public product documentation');
  assert.equal(separate.requests.at(-1).body.web_fallback, false);
});

test('overlay section shows explicitly, syncs current scope, and warns about screen share', async () => {
  const { tree } = pane('alpha');
  const text = JSON.stringify(tree);
  assert.match(text, /Overlay scope/);
  assert.match(text, /Show overlay/);
  assert.match(text, /may appear in screenshots, recordings or screen sharing/);
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
  const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
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
  const src = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
  const imported = reactImports(src);
  const called = new Set([...src.matchAll(/(?<![A-Za-z0-9_.])use[A-Z]\w*(?=\()/g)].map(m => m[0]));
  const provided = new Set(['useState', 'useRef', 'useEffect', 'useQueryClient', 'useQuery', 'useValue']);
  const localHooks = new Set([...src.matchAll(/\bfunction\s+(use[A-Z]\w*)\s*\(/g)].map(m => m[1]));
  for (const hook of called) {
    if (localHooks.has(hook)) continue;
    assert.ok(imported.has(hook) || provided.has(hook), `${hook} must be imported or harness-provided`);
    if (hook.startsWith('use') && !['useState', 'useRef', 'useEffect', 'useQueryClient', 'useQuery', 'useValue'].includes(hook)) {
      assert.ok(imported.has(hook), `${hook} is not a known harness global and must be imported from react`);
    }
  }
  assert.ok(imported.has('useRef'), 'useRef must be imported from react');
});

test('react import parser tolerates CRLF checkouts', () => {
  const src = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
  const crlf = src.replace(/\n/g, '\r\n');
  const imported = reactImports(crlf);
  assert.ok(imported.has('useRef'), 'CRLF must not fuse the import tail into a name');
  assert.ok(![...imported].some(s => s.includes('react')), 'no `} from \'react\'` fragments as names');
});

test('rejected scope sync closes the overlay with an actionable warning', async () => {
  const { rerender } = pane('alpha', true, false, '', {}, { '/overlay/scope': { accepted: false } });
  const findButton = (tree, label) => nodes(tree).filter(n => n.type === 'button')
    .find(n => (Array.isArray(n.children) ? n.children.join('') : String(n.children || '')) === label);
  let tree = rerender();
  await findButton(tree, 'Show overlay').props.onClick();
  tree = rerender();
  await findButton(tree, 'Sync scope').props.onClick();
  tree = rerender();
  assert.ok(JSON.stringify(tree).includes('Overlay closed because scope synchronization failed'), 'rejection must stay visible, not pretend synchronization succeeded');
  assert.ok(findButton(tree, 'Show overlay'), 'reopen must apply the visible scope');
});

test('scope sync carries the owner minted by show', async () => {
  const { rerender, requests } = pane('alpha', true, false, '', {}, {
    '/overlay/show': { shown: true, owner: 'owner-A' },
    '/overlay/scope': { accepted: true },
  });
  const button = (label) => nodes(rerender()).find(n => n.type === 'button' && n.children.includes(label));
  await button('Show overlay').props.onClick();
  await button('Sync scope').props.onClick();
  assert.equal(requests.find(r => r.path === '/overlay/scope').body.scope.owner, 'owner-A');
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
  assert.ok(JSON.stringify(tree).includes('Read-only overlay updated. Ask questions in the pane.'));
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

test('classified capture errors offer restart, local errors offer dismiss', async () => {
  const { tree: local } = pane('', true, false, '', { 6: 'boom' }, {}, {});
  assert.ok(nodes(local).some(n => n.type === 'button' && n.children.includes('Dismiss')));
  const { tree: remote } = pane('', true, false, '', {}, {}, { error: 'capture failed', error_source: 'capture' });
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

test('uncertain answer submission rechecks status without replay', async () => {
  const { tree, requests } = pane('edited', true, false, '', { 6: 'boom', 23: { question: 'original?', extra: {} }, 24: true }, {}, {});
  const retry = nodes(tree).find(n => n.type === 'button' && n.children.includes('Recheck status'));
  assert.ok(retry, 'uncertain submission offers status recheck');
  await retry.props.onClick();
  assert.ok(!requests.some(r => r.path === '/assist'), 'status recheck does not replay');
});

test('dismiss never sends anything', async () => {
  const { tree, requests } = pane('', true, false, '', { 6: 'boom' }, {}, {});
  const dismiss = nodes(tree).find(n => n.type === 'button' && n.children.includes('Dismiss'));
  assert.ok(dismiss, 'local errors offer Dismiss');
  await dismiss.props.onClick();
  assert.ok(!requests.some(r => r.path === '/assist'), 'dismiss must not send');
});

test('capture errors offer an honest capture restart instead of re-asking', async () => {
  const { tree, requests } = pane('edited', true, false, '', { 3: true, 4: 'macos', 23: { question: 'original?', extra: {} } }, {}, { error: 'capture failed', error_source: 'capture' });
  const retry = nodes(tree).find(n => n.type === 'button' && (n.children.includes('Restart capture') || n.children.includes('Retry')));
  assert.ok(retry, 'capture errors offer recovery');
  assert.equal(retry.props.disabled, false);
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
  const { tree, requests } = pane('', true, false, '', {}, {}, { error_request: stored, error: 'RuntimeError: down', answer_error: 'RuntimeError: down', error_source: 'answer' });
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
  const { tree } = pane('', true, false, '', { 6: 'local hiccup' }, {}, { error: 'RuntimeError: down', error_request: stored, answer_error: 'RuntimeError: down', error_source: 'answer' });
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
  const label = nodes(tree).find(n => n.type === 'section' && n.props['aria-label'] === 'Answer composer');
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
  const setup = nodes(tree).find(n => n.type === 'details' && n.props['data-answer-setup']);
  assert.ok(setup, 'answer setup renders');
  const stack = (setup.children || []).find(c => c && typeof c === 'object' && c.type === 'div'
    && c.props.style && c.props.style.display === 'flex');
  assert.ok(stack, 'setup content uses an inner flex stack');
  assert.ok(stack.props.style.gap >= 12, `expected gap >= 12, got ${stack.props.style.gap}`);
});

test('transcript segments use compact accent rows without losing speaker attribution', () => {
  const { tree } = pane('', true, true, '', {}, {}, { segments: [
    { speaker: 'Alice', text: 'hello there', timestamp: 1 },
    { speaker: 'Bob', text: 'general kenobi', timestamp: 2 },
  ] });
  const rows = nodes(tree).filter(n => n.props.className === 'mc-transcript-row');
  assert.equal(rows.length, 2);
  for (const [index, row] of rows.entries()) {
    assert.ok(nodes(row).some(n => n.type === 'i' && n.props['aria-hidden'] && n.props.style.background.startsWith('var(')));
    assert.ok(nodes(row).some(n => n.type === 'strong' && n.children.includes(index ? 'Bob' : 'Alice')));
    assert.match(JSON.stringify(row), index ? /general kenobi/ : /hello there/);
  }
});

test('setup rhythm: cards use grids and the web toggle is a tile', async () => {
  const { tree } = pane('', true, false, '', {}, {}, {});
  const setup = nodes(tree).find(n => n.type === 'details' && n.props['data-answer-setup']);
  const stack = (setup.children || []).find(c => c && typeof c === 'object' && c.type === 'div'
    && c.props.style && c.props.style.display === 'flex');
  assert.ok(stack && stack.props.style.gap >= 14, `expected setup gap >= 14, got ${stack && stack.props.style.gap}`);
  const modelCard = nodes(tree).find(n => n.type === 'section'
    && JSON.stringify(n.children).includes('Answer model'));
  assert.ok(modelCard, 'answer model card renders');
  assert.equal(modelCard.props.style.display, 'grid');
  assert.ok(modelCard.props.style.gap >= 10);
  const overlayCard = nodes(tree).find(n => n.type === 'section'
    && JSON.stringify(n.children).includes('Overlay scope'));
  assert.ok(overlayCard, 'overlay card renders');
  assert.equal(overlayCard.props.style.display, 'grid');
  const toggle = nodes(tree).find(n => n.type === 'label'
    && n.props.style && String(n.props.style.border || '').includes('var(--ui-stroke-secondary)')
    && JSON.stringify(n.children).includes('Automatic web fallback'));
  assert.ok(toggle, 'web fallback toggle renders as a bordered tile');
  assert.ok(toggle.props.style.borderRadius >= 10);
  assert.ok(toggle.props.style.padding.includes('12px'));
});

test('each speaker keeps a distinct theme color and direct name on every compact row', () => {
  const { tree } = pane('', true, true, '', {}, {}, { segments: [
    { speaker: 'Alice', text: 'first', timestamp: 1 },
    { speaker: 'Bob', text: 'second', timestamp: 2 },
    { speaker: 'Alice', text: 'third', timestamp: 3 },
  ] });
  const rows = nodes(tree).filter(n => n.props.className === 'mc-transcript-row');
  assert.equal(rows.length, 3);
  const colors = rows.map(row => nodes(row).find(n => n.type === 'i').props.style.background);
  assert.equal(colors[0], colors[2], 'same speaker, same color');
  assert.notEqual(colors[0], colors[1], 'different speakers, different colors');
  assert.ok(colors.every(color => color.includes('var(--ui-')), 'speaker colors follow the active theme');
  for (const [index, row] of rows.entries()) {
    const name = nodes(row).find(n => n.type === 'strong');
    assert.equal(name.props.style.color, colors[index]);
    assert.ok(name.children.includes(index === 1 ? 'Bob' : 'Alice'), 'direct names do not rely on color or a separate legend');
  }
});

test('desktop plugin uses theme colors and matches package versions', () => {
  const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /#[0-9a-f]{3,8}\b|\brgba?\(/i, 'plugin colors must use the host theme');
  const plugin = vm.runInNewContext(source.replace(/^import .*;\r?\n/gm, '').replace('export default', 'const plugin =') + '\nplugin;');
  const manifest = readFileSync(new URL('../plugin.yaml', import.meta.url), 'utf8');
  const match = /^version:\s*["']?([^"'\s]+)["']?\s*$/m.exec(manifest);
  assert.ok(match, 'Python plugin declares a version');
  const dashboard = JSON.parse(readFileSync(new URL('../dashboard/manifest.json', import.meta.url), 'utf8'));
  assert.equal(plugin.version, match[1]);
  assert.equal(dashboard.version, match[1]);
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
  const setup = nodes(tree).find(n => n.type === 'details' && n.props['data-answer-setup']);
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
  assert.ok(sessionNodes.some(n => n.type === 'label'
    && JSON.stringify(n.children).includes('I have permission to capture/read this meeting')
    && nodes(n).some(child => child.type === 'input' && child.props.type === 'checkbox')),
    'permission check lives in the top card');
  assert.ok(sessionNodes.some(n => n.type === 'button'
    && (n.children.includes('Start listening') || n.children.includes('Attach to Meetily')
      || n.children.includes('Resume') || n.children.includes('Resume listening'))),
    'primary start lives in the top card');
  const source = flat[sourceAt];
  assert.ok(!nodes(source).some(n => n.type === 'label'
    && JSON.stringify(n.children).includes('I have permission to capture/read this meeting')
    && nodes(n).some(child => child.type === 'input' && child.props.type === 'checkbox')),
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

test('uncertain replay rechecks without sending the request again', async () => {
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
  const retry2 = nodes(tree2).find(n => n.type === 'button' && n.children.includes('Recheck status'));
  assert.ok(retry2, 'uncertain replay offers a status recheck');
  await retry2.props.onClick();
  const bodies = requests.filter(r => r.path === '/assist').map(r => r.body);
  assert.equal(bodies.length, 1);
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
  // Consent, history exclusion, and web fallback: no duplicate controls.
  const boxes = all.filter(n => n.type === 'input' && n.props.type === 'checkbox');
  assert.ok(boxes.length <= 3, `expected at most consent, history and web controls, found ${boxes.length}`);
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
  const { tree, requests } = pane('', true, true, '', { 3: true, 4: 'macos' }, {}, { segments: [{ speaker: 'you', text: 'hi', timestamp: 1 }] });
  const btn = nodes(tree).filter(n => n.type === 'button').find(n => n.children.includes('New meeting'));
  assert.ok(btn, 'new meeting button exists when prior content');
  await btn.props.onClick();
  const start = requests.find(r => r.path === '/start');
  assert.ok(start, '/start called');
  assert.equal(start.body.new_meeting, true);
});


test('read-only overlay does not receive the pane web query', async () => {
  const { tree, requests } = pane('', true, true, 'explicit q');
  const btn = nodes(tree).filter(n => n.type === 'button').find(n => n.children.includes('Show overlay'));
  assert.ok(btn, 'show overlay offered');
  await btn.props.onClick();
  const show = requests.find(r => r.path === '/overlay/show');
  assert.ok(show, '/overlay/show called');
  assert.equal(show.body.scope.web_query, undefined);
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

const contextLabels = ['Catch me up', 'Extract actions', 'Suggest a question'];
const contextStatus = { segments: [{ speaker: 'you', text: 'The sample review moved to Monday.' }] };

test('recent-context actions make no automatic calls and preserve explicit BYOK scope', async () => {
  for (const label of contextLabels) {
    const ui = pane('/projects/alpha', true, false, 'unused public query', { 12: 'openai', 13: 'gpt-test' }, {}, contextStatus);
    assert.equal(ui.requests.length, 0);
    const quick = nodes(ui.tree).find(n => n.type === 'details' && n.props['aria-label'] === 'Recent meeting quick actions');
    assert.equal(!!quick.props.open, false, 'quick actions are progressively disclosed');
    assert.doesNotMatch(JSON.stringify(disclosedNodes(quick)), /not the whole meeting/);
    quick.props.open = true;
    assert.match(JSON.stringify(disclosedNodes(quick)), /not the whole meeting/);
    const action = button(ui.tree, label);
    assert.equal(action.props.disabled, false);
    await action.props.onClick();
    const requests = ui.requests.filter(r => r.path === '/assist');
    assert.equal(requests.length, 1);
    const body = requests[0].body;
    assert.equal(body.answer_provider, 'openai');
    assert.equal(body.answer_model, 'gpt-test');
    assert.equal(body.project, '/projects/alpha');
    assert.equal(body.all_projects, false);
    assert.equal(body.web_fallback, false);
    assert.equal(body.web_query, undefined, 'typed web query is not automatically used');
    assert.equal(body.response_contract, 'answer-admission-v1');
    assert.match(body.question, /recent available meeting transcript/);
    if (label === 'Extract actions') {
      assert.match(body.question, /explicitly stated; otherwise write unspecified/);
      assert.match(body.question, /proposed actions from agreed commitments/);
    }
    const composer = nodes(ui.rerender()).find(n => n.type === 'textarea');
    assert.equal(composer.props.value, body.question, 'user sees the prompt that was sent');
  }
});

test('quick actions preserve excluded-history or explicitly enabled web scope without weakening either', async () => {
  for (const historyExcluded of [true, false]) {
    const ui = pane('/projects/alpha', true, false, '', {}, {}, contextStatus);
    nodes(ui.tree).find(n => n.props['aria-label'] === 'Exclude past sessions').props.onChange({ target: { checked: historyExcluded } });
    fallbackControl(ui.tree).props.onChange({ target: { checked: !historyExcluded } });
    await button(ui.rerender(), 'Catch me up').props.onClick();
    const body = ui.requests.find(r => r.path === '/assist').body;
    assert.equal(body.include_history, historyExcluded ? false : undefined);
    assert.equal(body.web_fallback, !historyExcluded);
  }
  const ui = pane('/projects/alpha', true, false, '', {}, {}, contextStatus);
  nodes(ui.tree).find(n => n.props['aria-label'] === 'Exclude past sessions').props.onChange({ target: { checked: true } });
  fallbackControl(ui.tree).props.onChange({ target: { checked: true } });
  const action = button(ui.rerender(), 'Catch me up');
  assert.equal(action.props.disabled, true, 'incompatible privacy choices still fail closed');
  await action.props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 0);
});

test('quick actions fail closed without transcript, while busy, or without model support', async () => {
  const cases = [
    { status: {} },
    { status: { segments: [{ text: '   ' }] } },
    { status: { ...contextStatus, busy: true } },
    { status: contextStatus, supported: false },
    { status: contextStatus, settings: { 12: 'unavailable', 13: 'model' } },
  ];
  for (const c of cases) {
    const ui = pane('', c.supported !== false, false, '', c.settings || {}, {}, c.status);
    for (const label of contextLabels) {
      const action = button(ui.tree, label);
      assert.equal(action.props.disabled, true);
      await action.props.onClick(); // stale/programmatic callback must also be fenced
    }
    assert.equal(ui.requests.filter(r => r.path === '/assist').length, 0);
  }
});

test('quick action double click cannot submit twice while admission is pending', async () => {
  let release;
  const admission = new Promise(resolve => { release = resolve; });
  const ui = pane('', true, false, '', {}, { '/assist': () => admission }, contextStatus);
  const action = button(ui.tree, 'Catch me up');
  const first = action.props.onClick();
  await button(ui.tree, 'Extract actions').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 1);
  release({ request_id: 'quick-action', status: 'queued' });
  await first;
});

test('evidence pane and confirmed export show the full bounded model excerpt', async () => {
  const copies = [], prompts = [];
  // Astral symbols require code-point slicing to match Python's provider payload.
  const sourceText = '😀'.repeat(1025) + ' IMPORTANT SYNTHETIC FACT ' + 'z'.repeat(1300);
  const expected = Array.from(sourceText).slice(0, 2050).join('');
  const omitted = Array.from(sourceText).slice(0, 2051).join('');
  const ui = pane('', true, false, '', {}, {}, { result: {
    answer: 'Synthetic answer [1]', sources: [{ title: 'Synthetic source', source: 'docs/synthetic.md', text: sourceText }],
  } }, { window: { confirm: message => { prompts.push(message); return true; } },
    navigator: { clipboard: { writeText: async text => copies.push(text) } } });
  const paneText = JSON.stringify(ui.tree);
  assert.ok(paneText.includes('IMPORTANT SYNTHETIC FACT'), 'the pane must include facts after the old 900-character cutoff');
  assert.ok(paneText.includes(expected), 'display the same Unicode code-point prefix as the provider bound');
  assert.ok(!paneText.includes(omitted), 'do not display source text beyond the provider excerpt limit');
  assert.match(paneText, /excerpt.*2,050.*characters/i);
  assert.equal(copies.length, 0);
  await button(ui.tree, 'Copy answer + evidence').props.onClick();
  assert.match(prompts[0], /2,050 characters per source/);
  assert.ok(copies[0].includes(expected));
  assert.ok(!copies[0].includes(omitted));
  assert.match(copies[0], /excerpt.*2,050.*characters/i);
});

test('evidence export copies only displayed answer and bounded indexed excerpts after confirmation', async () => {
  const copies = [], prompts = [];
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {
    result: { answer: 'Review Monday [1].', model_route: 'openai / chosen-model',
      warnings: ['Partial response: verify this.'], request: { transcript: 'PRIVATE REQUEST SNAPSHOT', api_key: 'SECRET' },
      sources: [{ title: 'Planning note', source: 'docs/plan.md', text: 'x'.repeat(950), updated_at: 0 },
        { title: 'Second source', source: 'https://example.test/note', text: 'Supporting quote' }] },
    segments: [{ text: 'NEW LIVE TRANSCRIPT MUST NOT LEAK' }],
  }, { window: { confirm: message => { prompts.push(message); return true; } },
    navigator: { clipboard: { writeText: async text => copies.push(text) } } });
  assert.equal(copies.length, 0, 'export is not automatic');
  await button(ui.tree, 'Copy answer + evidence').props.onClick();
  assert.equal(copies.length, 1);
  assert.match(prompts[0], /confidential/);
  assert.match(copies[0], /Review Monday \[1\]/);
  assert.match(copies[0], /\[1\] Planning note/);
  assert.match(copies[0], /\[2\] Second source/);
  assert.match(copies[0], /Reported model route: openai \/ chosen-model/);
  assert.match(copies[0], /Partial response/);
  assert.match(copies[0], /not meeting segments or timestamps/);
  assert.match(copies[0], /Source updated \(not meeting time\): 1970-01-01T00:00:00.000Z/);
  assert.ok(copies[0].includes('x'.repeat(950)));
  assert.doesNotMatch(copies[0], /PRIVATE REQUEST|SECRET|NEW LIVE/);
  assert.equal(ui.requests.length, 0, 'copy sends no backend or provider request');
  assert.match(ui.notices.at(-1).message, /copied/);
});

test('evidence export never touches clipboard without a positive privacy confirmation', async () => {
  for (const browser of [{}, { window: { confirm: () => false } }]) {
    let writes = 0;
    const ui = pane('', true, false, '', {}, {}, { result: { answer: 'Synthetic answer' } }, {
      ...browser, navigator: { clipboard: { writeText: async () => writes++ } },
    });
    await button(ui.tree, 'Copy answer + evidence').props.onClick();
    assert.equal(writes, 0);
    assert.equal(ui.requests.length, 0);
  }
});

test('evidence export explicitly reports absent sources and clipboard failures', async () => {
  let copy;
  const ui = pane('', true, false, '', {}, {}, { result: { answer: 'Unverified suggestion' } }, {
    window: { confirm: () => true }, navigator: { clipboard: { writeText: async text => { copy = text; throw new Error('denied'); } } },
  });
  await button(ui.tree, 'Copy answer + evidence').props.onClick();
  assert.match(copy, /No retrieved source excerpts/);
  assert.match(copy, /not a complete meeting transcript/);
  assert.match(JSON.stringify(ui.rerender()), /Clipboard unavailable/);
  assert.equal(ui.notices.length, 0);
});

test('evidence export is unavailable without a displayed answer', () => {
  const empty = pane('', true, false, '', {}, {}, {});
  assert.equal(button(empty.tree, 'Copy answer + evidence'), undefined);
  const blank = pane('', true, false, '', {}, {}, { result: { answer: '' } });
  assert.equal(button(blank.tree, 'Copy answer + evidence').props.disabled, true);
});

test('SDK clipboard handles all explicit copies and preserves sensitive confirmation', async () => {
  const writes = [], prompts = [];
  const snapshot = { schema: 'transcript-snapshot-v1', provider_json: '{"secret":"SYNTHETIC"}', segments: [] };
  const ui = pane('', true, false, '', {}, {}, { result: { answer: 'CURRENT', sources: [], transcript_snapshot: snapshot },
    recent: [{ answer: 'RECENT', status: 'done' }] },
    { navigator: {}, window: { confirm: text => { prompts.push(text); return true; } }, setTimeout: () => 0 },
    { os: { writeClipboard: async text => { writes.push(text); return true; } } });
  for (const label of ['Copy', 'Copy this answer', 'Copy answer + evidence', 'Copy sent transcript context']) {
    const copy = button(ui.tree, label); assert.ok(copy, label); await copy.props.onClick();
  }
  assert.equal(writes.length, 4);
  assert.equal(writes[0], 'CURRENT'); assert.equal(writes[1], 'RECENT');
  assert.match(writes[2], /CURRENT/); assert.equal(writes[3], snapshot.provider_json);
  assert.equal(prompts.length, 2); assert.match(prompts.join(' '), /confidential/);
  assert.doesNotMatch(JSON.stringify(ui.rerender()), /Clipboard unavailable/);
  ui.unmount();
});

test('SDK clipboard never copies sensitive context without consent or after route changes during confirmation', async () => {
  for (const mode of ['declined', 'missing', 'route-change']) {
    const route = publicRoute('default', 'source-a'), writes = [];
    const snapshot = { schema: 'transcript-snapshot-v1', provider_json: '{"secret":"SYNTHETIC"}', segments: [] };
    const browser = mode === 'missing' ? { window: {} } : { window: { confirm: () => {
      if (mode === 'route-change') route.activate('default', 'source-b');
      return mode !== 'declined';
    } } };
    const ui = pane('', true, false, '', {}, {}, { result: { answer: 'CURRENT', sources: [], transcript_snapshot: snapshot } },
      browser, { route, os: { writeClipboard: async text => { writes.push(text); return true; } } });
    await button(ui.tree, 'Copy sent transcript context').props.onClick();
    assert.equal(writes.length, 0, mode); ui.unmount();
  }
});

test('SDK clipboard denial or rejection does not fall back to browser clipboard', async () => {
  for (const writeClipboard of [async () => false, async () => { throw new Error('denied'); }]) {
    let browserWrites = 0;
    const ui = pane('', true, false, '', {}, {}, { result: { answer: 'CURRENT', sources: [] } },
      { navigator: { clipboard: { writeText: async () => browserWrites++ } }, setTimeout: () => 0 },
      { os: { writeClipboard } });
    await button(ui.tree, 'Copy').props.onClick();
    assert.equal(browserWrites, 0, 'explicit SDK failure must not be bypassed');
    assert.match(JSON.stringify(ui.rerender()), /Clipboard unavailable/);
    ui.unmount();
  }
});

test('legacy browser clipboard fallback is used only when SDK door is absent', async () => {
  const writes = [];
  const ui = pane('', true, false, '', {}, {}, { result: { answer: 'CURRENT', sources: [] } },
    { navigator: { clipboard: { writeText: async value => writes.push(value) } }, setTimeout: () => 0 });
  await button(ui.tree, 'Copy').props.onClick();
  assert.deepEqual(writes, ['CURRENT']); ui.unmount();
});

const sentSnapshotFixture = { schema: 'transcript-snapshot-v1', snapshot_id: 'synthetic-snapshot',
  provider_json: '{"snapshot_id":"synthetic-snapshot","segments":[{"ref":"T1","text":"PRIVATE SENT CONTEXT"}]}',
  omitted_segments: 2, segments: [{ ref: 'T1', speaker: 'meeting', text: 'PRIVATE SENT CONTEXT',
    timestamp: 12, timestamp_kind: 'meetily_audio_start_time_uninterpreted', shortened: true }] };

test('recent answers retain their own source and transcript context without automatic export', async () => {
  const copies = [], prompts = [];
  const previousSnapshot = { ...sentSnapshotFixture, snapshot_id: 'previous',
    provider_json: '{"segments":[{"ref":"T1","text":"PREVIOUS SENT CONTEXT"}]}',
    segments: [{ ref: 'T1', speaker: 'Prior', text: 'PREVIOUS SENT CONTEXT', timestamp: null }] };
  const currentSnapshot = { ...sentSnapshotFixture, snapshot_id: 'current',
    provider_json: '{"segments":[{"ref":"T1","text":"CURRENT SENT CONTEXT"}]}',
    segments: [{ ref: 'T1', speaker: 'Now', text: 'CURRENT SENT CONTEXT', timestamp: null }] };
  const ui = pane('', true, false, '', {}, {}, {
    result: { answer: 'New answer [T1]', transcript_snapshot: currentSnapshot },
    recent: [{ request_id: 'old', question: 'Earlier question', status: 'done', answer: 'Old answer [T1] [1]',
      transcript_snapshot: previousSnapshot, sources: [{ title: 'Old file', text: 'OLD EVIDENCE EXCERPT' }] }],
    segments: [{ speaker: 'Live', text: 'NEW LIVE WORDS' }],
  }, { window: { confirm: message => { prompts.push(message); return true; } },
    navigator: { clipboard: { writeText: async text => copies.push(text) } } });
  const recent = nodes(ui.tree).find(n => n.type === 'details'
    && n.children.some(child => child?.type === 'summary' && JSON.stringify(child.children).includes('Recent answers')));
  assert.ok(recent);
  assert.match(JSON.stringify(recent), /PREVIOUS SENT CONTEXT/);
  assert.match(JSON.stringify(recent), /OLD EVIDENCE EXCERPT/);
  assert.doesNotMatch(JSON.stringify(recent), /CURRENT SENT CONTEXT|NEW LIVE WORDS/);
  const copyContext = nodes(recent).find(n => n.type === 'button' && n.children.includes('Copy sent transcript context'));
  assert.ok(copyContext, 'each retained answer should expose its own sent transcript context');
  assert.equal(copies.length, 0, 'history must never export automatically');
  await copyContext.props.onClick();
  assert.equal(copies[0], previousSnapshot.provider_json, 'copy must use this historical snapshot, not the latest one');
  assert.match(prompts[0], /confidential meeting text/);
  assert.equal(ui.requests.length, 0, 'viewing and copying history does not make a provider request');
});

test('sent transcript context is separately disclosed and exported exactly only on confirmation', async () => {
  const copies = [], confirms = [];
  const ui = pane('', true, false, '', {}, {}, { result: { answer: 'Suggestion [T1]',
    transcript_snapshot: sentSnapshotFixture }, segments: [{ text: 'NEW LIVE WORDS' }] }, {
    window: { confirm: msg => { confirms.push(msg); return true; } },
    navigator: { clipboard: { writeText: async text => copies.push(text) } },
  });
  assert.equal(copies.length, 0);
  assert.match(JSON.stringify(ui.tree), /not verified claims/);
  assert.match(JSON.stringify(ui.tree), /not verified utterance time/);
  await button(ui.tree, 'Copy answer + evidence').props.onClick();
  assert.doesNotMatch(copies[0], /PRIVATE SENT CONTEXT|NEW LIVE WORDS/);
  await button(ui.tree, 'Copy sent transcript context').props.onClick();
  assert.equal(copies[1], sentSnapshotFixture.provider_json);
  assert.match(confirms[1], /confidential meeting text/);
  assert.match(confirms[1], /history or sync/);
  assert.equal(ui.requests.length, 0);
});

test('failed historical requests never claim unconfirmed context was sent', () => {
  const ui = pane('', true, false, '', {}, {}, { recent: [{ request_id: 'failed', question: 'Failed request',
    status: 'error', answer: '', transcript_snapshot: sentSnapshotFixture }] });
  const recent = nodes(ui.tree).find(n => n.type === 'details'
    && n.children.some(child => child?.type === 'summary' && JSON.stringify(child.children).includes('Recent answers')));
  assert.ok(recent);
  assert.doesNotMatch(JSON.stringify(recent), /Transcript context sent with this answer|PRIVATE SENT CONTEXT/);
  assert.equal(nodes(recent).find(n => n.type === 'button' && n.children.includes('Copy sent transcript context')), undefined);
});

test('historical transcript references disclose missing context and reject invalid or unconfirmed copies', async () => {
  for (const snapshot of [undefined, { ...sentSnapshotFixture, provider_json: 'x'.repeat(16001) }, sentSnapshotFixture]) {
    const copies = [];
    const ui = pane('', true, false, '', {}, {}, { recent: [{ request_id: 'prior', question: 'Earlier',
      status: 'done', answer: 'Prior answer [T1]', transcript_snapshot: snapshot }] }, {
      window: { confirm: () => false }, navigator: { clipboard: { writeText: async text => copies.push(text) } },
    });
    const recent = nodes(ui.tree).find(n => n.type === 'details'
      && n.children.some(child => child?.type === 'summary' && JSON.stringify(child.children).includes('Recent answers')));
    if (!snapshot) {
      assert.match(JSON.stringify(recent), /Transcript context for this answer is unavailable/);
    } else {
      const copy = nodes(recent).find(n => n.type === 'button' && n.children.includes('Copy sent transcript context'));
      assert.equal(copy.props.disabled, snapshot.provider_json.length > 16000);
      await copy.props.onClick();
    }
    assert.deepEqual(copies, []);
    assert.equal(ui.requests.length, 0);
  }
});

test('sent transcript export refuses missing confirmation and handles clipboard denial', async () => {
  for (const confirm of [undefined, () => false]) {
    const ui = pane('', true, false, '', {}, {}, { result: { answer: 'Answer', transcript_snapshot: sentSnapshotFixture } }, {
      window: { confirm }, navigator: { clipboard: { writeText: async () => assert.fail('must not copy') } },
    });
    await button(ui.tree, 'Copy sent transcript context').props.onClick();
    assert.equal(ui.requests.length, 0);
  }
  const ui = pane('', true, false, '', {}, {}, { result: { answer: 'Answer', transcript_snapshot: sentSnapshotFixture } }, {
    window: { confirm: () => true },
  });
  await button(ui.tree, 'Copy sent transcript context').props.onClick();
  assert.match(JSON.stringify(ui.rerender()), /Clipboard unavailable/);
});

test('snapshot retry forwards only server replay identity and original text, never live metadata', async () => {
  const req = { question: 'Saved question', transcript: 'saved input', replay_request_id: 'prior-request',
    include_history: false, project: '/projects/alpha', answer_provider: 'openai', answer_model: 'gpt-test' };
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {
    answer_error: 'failed', error: 'failed', error_source: 'answer', error_request: req,
    segments: [{ text: 'new live words' }],
  });
  const retry = nodes(ui.tree).find(n => n.type === 'button' && n.children.some(x => typeof x === 'string' && x.startsWith('Retry')));
  assert.ok(retry);
  await retry.props.onClick();
  const request = ui.requests.find(r => r.path === '/assist');
  assert.equal(request.body.replay_request_id, 'prior-request');
  assert.equal(request.body.transcript, 'saved input');
  assert.equal(request.body.transcript_snapshot, undefined);
});

test('visual workspace keeps capture and privacy controls disclosed and styles scoped', () => {
  const ui = pane('', true, false, '', {}, {}, { listening: true, backend: 'macos' });
  assert.equal(ui.tree.props.className, 'mc-workspace');
  assert.ok(nodes(ui.tree).find(n => n.type === 'h1' && n.children.includes('Meeting Copilot')));
  assert.ok(disclosedNodes(ui.tree).find(n => n.type === 'button' && n.children.includes('Pause listening')));
  assert.match(JSON.stringify(disclosedNodes(ui.tree)), /Screen-share privacy/);
  const css = nodes(ui.tree).find(n => n.type === 'style').children.join('');
  assert.match(css, /\.mc-workspace .*focus-visible/);
  assert.match(css, /@container \(max-width: 300px\)/);
  assert.match(css, /prefers-reduced-motion/);
  assert.equal(ui.requests.length, 0);
});
test('visual answer card retains full evidence and empty state never replaces an answer', () => {
  const empty = pane('').tree;
  assert.ok(nodes(empty).find(n => n.props.className === 'mc-empty'));
  const ui = pane('', true, false, '', {}, {}, { result: { answer: 'Synthetic answer [1].', sources: [{ title: 'Synthetic source', text: 'Synthetic evidence.', source: 'demo/launch.md' }] } });
  assert.equal(nodes(ui.tree).filter(n => n.props.className === 'mc-empty').length, 0);
  assert.equal(nodes(ui.tree).find(n => n.props['data-answer-result']).props.className, 'mc-answer');
  assert.ok(nodes(ui.tree).find(n => n.props.className === 'mc-evidence-card'));
  assert.match(JSON.stringify(ui.tree), /Synthetic evidence/);
});

for (const [name, config] of [
  ['blocked', { preflight: { ready: false, checks: [{ id: 'permission', status: 'blocked', label: 'Microphone permission', detail: 'Allow access.' }] } }],
  ['expired', { preflight: { receivedAt: Date.now() - 20000 } }],
  ['wrong settings', { preflight: { signature: 'stale-settings' } }],
  ['empty checks', { preflight: { checks: [] } }],
  ['failed', { preflightError: new Error('offline') }],
  ['refresh in flight', { preflightFetchStatus: 'fetching' }],
]) test(`preflight ${name} disables and fences Start`, async () => {
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, {}, {}, config);
  const start = button(ui.tree, 'Start listening');
  assert.equal(start.props.disabled, true);
  await start.props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 0);
});

test('local capture readiness does not require an answer provider or key', async () => {
  const ui = pane('', true, false, '', { 3: true, 4: 'macos', 12: '', 13: '' }, {}, {}, {}, { providers: [] });
  assert.equal(button(ui.tree, 'Start listening').props.disabled, false);
  await button(ui.tree, 'Start listening').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 1);
  assert.equal(ui.requests.filter(r => r.path === '/models/discover').length, 0);
});

for (const pending of [false, true]) test(`removed preflight capability fences retained Start (pending=${pending})`, async () => {
  const claim = gate(), status = idleCaptureStatus();
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, { '/overlay/claim': () => claim.promise }, status);
  const start = button(ui.tree, 'Start listening');
  let work;
  if (pending) { work = start.props.onClick(); await settle(); }
  delete status.capabilities.capture_preflight;
  if (!pending) work = start.props.onClick();
  claim.resolve({ accepted: true, pane: 'test-pane' });
  await work;
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 0);
});

test('capture checks only send selected settings and consent, never answers or recording requests', async () => {
  const ui = pane('', true, false, '', { 3: true, 4: 'macos', 11: 'system' }, { '/preflight': { version: 1, ready: false, checks: [] } });
  const query = ui.queryOptions.find(q => q.queryKey[1] === 'preflight');
  await query.queryFn();
  const request = ui.requests.find(r => r.path === '/preflight');
  assert.equal(request.body.audio_source, 'system');
  assert.equal(request.body.consent, true);
  assert.equal(request.body.transcription_provider, 'local');
  assert.equal(request.body.question, undefined);
  assert.equal(request.body.answer_model, undefined);
  assert.equal(ui.requests.filter(r => ['/start', '/permissions/request', '/models/discover'].includes(r.path)).length, 0);
});

function modelOptionsPane(response, globals = {}, extra = {}) {
  return pane('/projects/alpha', true, false, '', {}, { '/models/discover': response }, {},
    { window: { confirm: () => true }, ...globals },
    { settingsHost: true, providers: [{ id: 'openai', label: 'OpenAI', configured: true }, { id: 'openrouter', label: 'OpenRouter', configured: true }], ...extra });
}

test('provider model options load only explicitly and preserve manual selection', async () => {
  const confirmations = [];
  const ui = modelOptionsPane({ provider: 'openai', models: ['gpt-first', 'gpt-second'], status: 'Account list; compatibility unverified.' },
    { window: { confirm: text => { confirmations.push(text); return true; } } });
  const page = settingsPage(ui);
  const initial = page.render();
  assert.equal(ui.requests.filter(r => r.path === '/models/discover').length, 0);
  await button(initial, 'Load model options…').props.onClick();
  const request = ui.requests.find(r => r.path === '/models/discover');
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)), { provider: 'openai', consent: true });
  assert.match(confirmations[0], /saved API key/);
  assert.match(confirmations[0], /No meeting, question or project content/);
  let tree = page.render();
  assert.equal(settingNode(tree, 'Answer model').props.value, 'gpt-test', 'loading never picks a model');
  settingNode(tree, 'Listed model options').props.onChange({ target: { value: 'gpt-second' } });
  tree = page.render();
  assert.equal(settingNode(tree, 'Answer model').props.value, 'gpt-second');
  settingNode(tree, 'Answer model').props.onChange({ target: { value: 'gpt-custom' } });
  assert.equal(settingNode(page.render(), 'Answer model').props.value, 'gpt-custom');
});

for (const change of ['manual model', 'provider away and back']) test(`stale model discovery after ${change} retires loading and permits retry`, async () => {
  const pending = gate();
  const ui = modelOptionsPane(() => pending.promise);
  const page = settingsPage(ui);
  const load = button(page.render(), 'Load model options…').props.onClick;
  const work = load();
  await load();
  assert.equal(ui.requests.filter(r => r.path === '/models/discover').length, 1, 'repeated clicks do not duplicate requests');
  if (change === 'manual model') settingNode(page.render(), 'Answer model').props.onChange({ target: { value: 'gpt-manual' } });
  else {
    settingNode(page.render(), 'mc-provider-label').props.onChange({ target: { value: 'openrouter' } });
    settingNode(page.render(), 'mc-provider-label').props.onChange({ target: { value: 'openai' } });
  }
  pending.resolve({ provider: 'openai', models: ['gpt-stale'], status: 'Unverified' });
  await work;
  const tree = page.render();
  assert.equal(button(tree, 'Load model options…').props.disabled, false);
  assert.equal(settingNode(tree, 'Listed model options'), undefined);
  assert.equal(settingNode(tree, 'Answer model').props.value, change === 'manual model' ? 'gpt-manual' : '');
});

test('cancelled model discovery and missing key do not send a provider request', async () => {
  for (const allowed of [false, true]) {
    const ui = modelOptionsPane({}, { window: { confirm: () => allowed } },
      allowed ? { providers: [{ id: 'openai', configured: false }] } : {});
    await button(settingsPage(ui).render(), 'Load model options…').props.onClick();
    assert.equal(ui.requests.filter(r => r.path === '/models/discover').length, 0);
  }
});

test('OS permissions require a separate confirmed action and never start capture', async () => {
  const ui = pane('', true, false, '', { 3: false, 4: 'macos' }, {
    '/permissions/request': { version: 1, status: 'unknown', capture_started: false },
  }, {}, { window: { confirm: () => true } });
  await button(ui.tree, 'Request macOS permissions…').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/start').length, 0);
  assert.equal(ui.requests.filter(r => r.path === '/permissions/request').length, 1);
  assert.match(JSON.stringify(ui.rerender()), /Permission setup is not confirmed/);
});

test('new preferences follow Hermes default without a separate answer key', async () => {
  const ui = pane('/projects/alpha', true, false, '', {}, {}, {}, {}, {
    noSeedOverrides: true, settingsHost: true,
    storage: settingsStore(),
    providers: [{ id: 'hermes', label: 'Follow Hermes default', configured: true, status: 'Host-scoped route available' }],
  });
  let tree = settingsPage(ui).render();
  assert.equal(settingNode(tree, 'mc-provider-label').props.value, 'hermes');
  assert.equal(settingNode(tree, 'Answer model'), undefined);
  assert.match(JSON.stringify(tree), /current Hermes default model/);
  assert.equal(button(tree, 'Load model options…'), undefined);
  settingNode(tree, 'mc-scope-label').props.onChange({ target: { value: '/projects/alpha' } });
  await button(ui.rerender(), 'Suggest answer').props.onClick();
  const body = ui.requests.find(r => r.path === '/assist')?.body;
  assert.equal(body?.answer_provider, 'hermes');
  assert.equal(body?.answer_model, 'default');
  assert.equal(ui.requests.filter(r => r.path === '/models/discover').length, 0);
});

test('saved explicit answer override is preserved and can deliberately follow Hermes again', () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { answerProvider: 'openrouter', answerModel: 'vendor/saved', project: '/projects/alpha' });
  const ui = pane('', true, false, '', {}, {}, {}, {}, { noSeedOverrides: true, settingsHost: true, storage,
    providers: [{ id: 'hermes', configured: true }, { id: 'openrouter', configured: true }] });
  const page = settingsPage(ui);
  let tree = page.render();
  assert.equal(settingNode(tree, 'mc-provider-label').props.value, 'openrouter');
  assert.equal(settingNode(tree, 'Answer model').props.value, 'vendor/saved');
  settingNode(tree, 'mc-provider-label').props.onChange({ target: { value: 'hermes' } });
  tree = page.render();
  assert.equal(settingNode(tree, 'mc-provider-label').props.value, 'hermes');
  assert.equal(settingNode(tree, 'Answer model'), undefined);
  assert.equal(storage.map.get('preferences-v1').answerModel, 'default');
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 0);
});

test('unavailable Hermes default is visible and never falls back to configured BYOK', async () => {
  const ui = pane('', true, false, '', {}, {}, {}, {}, { noSeedOverrides: true, settingsHost: true,
    providers: [{ id: 'hermes', configured: false, status: 'Hermes default is unavailable in this host mode.' }, { id: 'openai', configured: true }] });
  const page = settingsPage(ui).render();
  assert.match(JSON.stringify(page), /Hermes default is unavailable/);
  await button(ui.tree, 'Suggest answer').props.onClick();
  assert.equal(ui.requests.filter(r => r.path === '/assist').length, 0);
  assert.equal(settingNode(page, 'mc-provider-label').props.value, 'hermes');
});

// Native details expose their summary while closed. This checks disclosure
// structure only, not browser geometry, contrast or SDK control rendering.
function compactVisible(node) {
  if (node == null || typeof node === 'boolean') return [];
  if (typeof node !== 'object') return [node];
  if (Array.isArray(node)) return node.flatMap(compactVisible);
  if (['style', 'svg'].includes(node.type) || node.props?.hidden || node.props?.['aria-hidden'] === true) return [];
  const children = node.type === 'details' && !node.props.open
    ? node.children.filter(child => child?.type === 'summary') : node.children;
  return [node, ...children.flatMap(compactVisible)];
}
const compactText = tree => compactVisible(tree).filter(value => typeof value !== 'object').join(' ');

test('compact ready pane collapses passed detail without hiding consent or performing work', () => {
  const checks = Array.from({ length: 11 }, (_, i) => ({ id: `check-${i}`, status: 'ready', label: `Fixture ${i}`,
    detail: 'Synthetic detailed successful verification kept available on demand.' }));
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, {}, {}, { preflight: { checks } });
  const passed = nodes(ui.tree).find(n => n.props['data-passed-checks']);
  assert.ok(passed && !passed.props.open);
  const text = compactText(ui.tree);
  assert.match(text, /11 checks passed/);
  assert.doesNotMatch(text, /Synthetic detailed successful verification/);
  assert.match(text, /I have permission to capture\/read this meeting/);
  assert.match(text, /Ready to start/);
  assert.equal(button(ui.tree, 'Start listening').props.disabled, false);
  assert.ok(text.split(/\s+/).length < 230, 'Routine ready UI avoids expanded setup paragraphs');
  assert.equal(ui.requests.length, 0, 'rendering or native disclosure does not start capture or contact a provider');
  assert.equal(passed.props.onToggle, undefined, 'native disclosure needs no backend or side effects');
});

test('compact blocked and unknown checks retain remedies and explicit permission setup', () => {
  const checks = [
    { id: 'microphone_permission', status: 'blocked', label: 'Microphone permission', detail: 'Allow Microphone in System Settings, then recheck.' },
    { id: 'runtime', status: 'unknown', label: 'Audio runtime', detail: 'The local runtime could not be verified. Repair the installed runtime.' },
    { id: 'ready', status: 'ready', label: 'Consent', detail: 'Synthetic consent passed.' },
  ];
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, {}, {}, { preflight: { ready: false, checks } });
  const text = compactText(ui.tree);
  assert.match(text, /Needs setup: Microphone permission/);
  assert.match(text, /Allow Microphone in System Settings, then recheck/);
  assert.match(text, /Not verified: Audio runtime/);
  assert.match(text, /Repair the installed runtime/);
  assert.ok(compactVisible(ui.tree).some(n => n?.type === 'button' && n.children.includes('Request macOS permissions…')));
  assert.equal(button(ui.tree, 'Start listening').props.disabled, true);
  assert.equal(ui.requests.length, 0, 'blocked render never prompts for permission');
});

for (const [name, status, behavior, consent] of [
  ['stop unconfirmed', { stopping: true, stop_confirmed: false }, {}, true],
  ['status offline', {}, { statusError: new Error('synthetic offline') }, true],
  ['preflight offline', {}, { preflightError: new Error('synthetic offline') }, true],
  ['expired checks', {}, { preflight: { receivedAt: Date.now() - 30000 } }, true],
  ['missing contract', { capabilities: { macos_transcription: true } }, {}, true],
  ['no consent', {}, {}, false],
  ['overlay cleanup', { overlay_cleanup_error: 'Synthetic shutdown unconfirmed' }, {}, true],
]) test(`compact readiness never contradicts ${name}`, () => {
  const ui = pane('', true, false, '', { 3: consent, 4: 'macos' }, {}, status, {}, behavior);
  assert.doesNotMatch(compactText(ui.tree), /Ready to start/);
  assert.ok(!button(ui.tree, 'Start listening') || button(ui.tree, 'Start listening').props.disabled);
  const checkStatus = nodes(ui.tree).find(n => n.props['aria-label'] === 'Required capture checks');
  assert.ok(!nodes(checkStatus).some(n => n.props['data-tone'] === 'ready'));
  assert.equal(ui.requests.length, 0);
});

for (const [state, expected] of [['running', 'Listening'], ['loading', 'Starting…'], ['starting', 'Starting…']]) {
  test(`compact active rail preserves ${state} state and Pause alongside answer errors`, async () => {
    const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, {
      listening: true, backend: 'macos', error: 'Synthetic answer failure', capture: { state },
    });
    const rail = nodes(ui.tree).find(n => n.props['data-capture-rail']);
    assert.match(compactText(rail), new RegExp(`${expected} · needs attention`));
    assert.equal(nodes(rail).some(n => n.type === 'details'), false, 'capture rail cannot collapse');
    assert.equal(nodes(ui.tree).filter(n => n.type === 'button' && n.children.includes('Pause listening')).length, 1);
    assert.equal(button(rail, 'Pause listening').props.disabled, undefined);
    await button(rail, 'Pause listening').props.onClick();
    assert.equal(ui.requests.filter(r => r.path === '/pause').length, 1);
    assert.equal(ui.requests.filter(r => r.path === '/start' || r.path === '/assist').length, 0);
  });
}

test('compact stop retry and audio loss remain disclosed with all setup details closed', () => {
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' }, {}, {
    stopping: true, stop_confirmed: false, capture: { native_dropped_chunks: 3, worker_dropped_chunks: 2 },
  });
  const rail = nodes(ui.tree).find(n => n.props['data-capture-rail']);
  assert.ok(button(rail, 'Retry stop'));
  const text = compactText(ui.tree);
  assert.match(text, /Stopping — unconfirmed/);
  assert.match(text, /audio may still be active/);
  assert.match(text, /3 native frame\(s\)/);
  assert.match(text, /2 worker chunk\(s\)/);
});

test('compact visual controls retain native disclosure, accessible names, focus and reduced motion', () => {
  const ui = pane('', true, false, '', { 3: true, 4: 'macos' });
  for (const details of nodes(ui.tree).filter(n => n.type === 'details')) {
    assert.equal(details.children[0].type, 'summary');
    assert.ok(compactText(details.children[0]).trim(), 'every native disclosure has a textual name');
  }
  for (const svg of nodes(ui.tree).filter(n => n.type === 'svg')) assert.equal(svg.props['aria-hidden'], true);
  assert.ok(button(ui.tree, 'Recheck').props['aria-label']);
  const stylesheet = nodes(ui.tree).find(n => n.type === 'style').children.join('');
  assert.match(stylesheet, /\.mc-rail\s*\{[^}]*position:\s*sticky/);
  assert.match(stylesheet, /focus-visible/);
  assert.match(stylesheet, /prefers-reduced-motion:\s*reduce/);
  assert.match(stylesheet, /max-width:\s*300px/);
  assert.equal(ui.requests.length, 0);
});

test('Meetily Detach keeps its recording boundary beside the always-visible action', () => {
  const ui = pane('', true, false, '', { 3: true, 4: 'meetily', 5: 'synthetic-recording' }, {}, { listening: true, backend: 'meetily' });
  const rail = nodes(ui.tree).find(n => n.props['data-capture-rail']);
  assert.ok(button(rail, 'Detach'));
  assert.match(compactText(rail), /Detach does not stop Meetily recording/);
  assert.equal(ui.requests.length, 0);
});

for (const settingsHost of [false, true]) test(`required Meetily recording selector is disclosed without expanding setup (settings host ${settingsHost})`, () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'meetily' });
  const ui = pane('', true, false, '', { 4: 'meetily' }, {}, {}, {}, { settingsHost, storage });
  const selector = compactVisible(ui.tree).find(n => n?.type === 'select' && n.props['aria-labelledby'] === 'mc-rec-label');
  assert.ok(selector, 'mandatory selector is outside every closed disclosure');
  assert.equal(!!selector.props.disabled, false);
  assert.match(compactText(ui.tree), /Active Meetily recording/);
  assert.equal(button(ui.tree, 'Attach to Meetily').props.disabled, true);
  assert.equal(ui.requests.length, 0);
});

test('compact capture errors preserve the readable snapshot failure instead of only its code', () => {
  const ui = pane('', true, false, '', { 4: 'meetily' }, {}, { listening: true, backend: 'meetily',
    capture_error: 'snapshot_unavailable', capture: { error: 'snapshot_unavailable', message: 'Waiting for a valid JSON snapshot' } });
  assert.match(compactText(ui.tree), /Waiting for a valid JSON snapshot/);
  const message = compactVisible(ui.tree).find(n => n?.props?.['data-capture-message']);
  assert.equal(message.props.role, 'status');
  const routine = pane('', true, false, '', { 4: 'meetily' }, {}, { capture: { message: 'Listening to selected recording' } });
  assert.doesNotMatch(compactText(routine.tree), /Listening to selected recording/);
});

test('nonzero audio with only empty completed chunks gets a compact actionable note, not a silence claim', () => {
  const diagnostics = { microphone: { received_samples: 96000, completed: 2, empty: 2, peak: .2, rms: .01 },
    system: { received_samples: 96000, completed: 2, empty: 1, peak: .2, rms: .01 } };
  const ui = pane('', true, false, '', { 4: 'macos' }, {}, { listening: true, capture: { audio_diagnostics: diagnostics } });
  const note = compactVisible(ui.tree).find(n => n?.props?.['data-audio-attention']);
  assert.match(compactText(note), /Microphone: audio received, no words detected/);
  assert.match(compactText(note), /input, speech language and transcription model/);
  assert.doesNotMatch(compactText(note), /System|silence|failed|broken/);
  assert.equal(ui.requests.length, 0);
  for (const row of [
    { received_samples: 0, completed: 2, empty: 2, peak: .1 },
    { received_samples: 10, completed: 2, empty: 2 },
    { received_samples: 10, completed: 2, empty: 2, peak: 0, rms: 0 },
    { received_samples: 10, completed: 0, empty: 0, peak: .1 },
    { received_samples: 10, completed: 2, empty: 2, peak: Infinity },
  ]) {
    const other = pane('', true, false, '', {}, {}, { capture: { audio_diagnostics: { microphone: row } } });
    assert.ok(!nodes(other.tree).some(n => n.props['data-audio-attention']), 'absent/invalid evidence cannot establish nonzero empty recognition');
  }
});

test('composer shortcut is visible and machine-readable as well as retaining its key handler', () => {
  const ui = pane('', true, false);
  assert.match(compactText(ui.tree), /⌘\/Ctrl\+Enter to ask/);
  const textarea = nodes(ui.tree).find(n => n.type === 'textarea');
  assert.equal(textarea.props['aria-keyshortcuts'], 'Meta+Enter Control+Enter');
  const hint = compactVisible(ui.tree).find(n => n?.props?.id === textarea.props['aria-describedby']);
  assert.match(compactText(hint), /⌘\/Ctrl\+Enter/);
  assert.equal(typeof textarea.props.onKeyDown, 'function');
});

test('unconfirmed Meta stop retains the transmission warning even with next defaults local', () => {
  const storage = settingsStore();
  storage.set('preferences-v1', { backend: 'macos', transcriptionProvider: 'local' });
  const ui = pane('', true, false, '', {}, {}, { listening: false, stopping: true, stop_confirmed: false, backend: 'macos',
    last_session: { backend: 'macos', transcription_provider: 'meta' } }, {}, { settingsHost: true, storage });
  const notice = compactVisible(ui.tree).find(n => n?.props?.['data-cloud-capture-disclosure']);
  assert.match(compactText(notice), /Meta cloud transcription may still be active/);
  assert.match(compactText(notice), /Audio transmission is not confirmed stopped; retry stop/);
  assert.ok(button(ui.tree, 'Retry stop'));
  assert.doesNotMatch(compactText(ui.tree), /Ready to start/);
  assert.equal(ui.requests.length, 0);
});
