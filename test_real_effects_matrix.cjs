// Separate opt-in gate: use existing React/ReactDOM/jsdom, never install them.
const { spawnSync } = require('node:child_process');
const assert = require('node:assert/strict');
const cases = [
  ['normal', {}],
  ['readback-failure', { READBACK_FAULT: '1' }],
  ['partial-write-shutdown', { WRITE_SHUTDOWN_FAULT: '1' }],
  ['conflict-shutdown-no-readback', { CONFLICT_SHUTDOWN_FAULT: '1' }],
  ['delayed-retirement', { DELAYED_RETIREMENT: '1' }],
  ['delayed-retirement-readback-failure', { DELAYED_RETIREMENT: '1', READBACK_FAULT: '1' }],
  ['delayed-status-fallback', { DELAYED_STATUS: '1' }],
  ['valid-claim-rejection', { CLAIM_CONFLICT: '1' }],
];
for (const code of ['409', '503']) {
  cases.push([`delayed-${code}-pause`, { DELAYED_FAILURE: code }]);
  cases.push([`delayed-${code}-new-owner`, { DELAYED_FAILURE: code, REPLACE_OVERLAY: '1' }]);
}
for (const action of ['start', 'pause', 'clear']) {
  cases.push([`authorized-${action}-adapter`, { ACTION_DIAGNOSTIC: action }]);
  cases.push([`authorized-${action}-transport`, { ACTION_DIAGNOSTIC: action, ACTION_TRANSPORT_FAULT: '/' + action }]);
}
for (const fault of ['lookup', 'claim']) {
  for (const action of ['start', 'pause', 'clear']) {
    cases.push([`${fault}-${action}`, { INITIAL_LIFECYCLE_FAULT: fault, LIFECYCLE_ACTION: action }]);
  }
}
let passed = 0;
for (const strict of [false, true]) {
  for (const [name, mode] of cases) {
    const env = { PATH: process.env.PATH, TMPDIR: process.env.TMPDIR,
      MEETING_REACT_TEST_MODULES: process.env.MEETING_REACT_TEST_MODULES,
      ...(process.env.PYTHON ? { PYTHON: process.env.PYTHON } : {}),
      ...mode, ...(strict ? { STRICT: '1' } : {}) };
    const result = spawnSync(process.execPath, [__dirname + '/test_real_effects.cjs'],
      { env, encoding: 'utf8', timeout: 45000 });
    const label = `${strict ? 'StrictMode' : 'normal'}: ${name}`;
    if (result.status !== 0) console.error(result.stdout, result.stderr, result.error || '');
    assert.equal(result.status, 0, label);
    console.log('PASS ' + label);
    passed++;
  }
}
assert.equal(passed, cases.length * 2);
console.log(JSON.stringify({ passed, failed: 0 }));
