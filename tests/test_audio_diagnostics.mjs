import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
const fn = source.slice(source.indexOf('function audioDiagnosticText('), source.indexOf('// Scoped to the pane'));
const format = vm.runInNewContext(`${fn}\naudioDiagnosticText;`);
test('diagnostics distinguish absent, zero-count, zero-level and empty transcription', () => {
  assert.equal(format(null), '');
  assert.equal(format({microphone: {peak: 0, rms: 0}}), '');
  assert.match(format({microphone: {received_samples: 0, peak: 0, rms: 0}}), /no samples received/);
  assert.match(format({system: {received_samples: 10, peak: 0, rms: 0}}), /zero-level input/);
  assert.match(format({system: {received_samples: 10, peak: .3, rms: .1, completed: 2, empty: 2}}), /input received; 2 chunk\(s\) transcribed, 2 with no words/);
  assert.equal(format({microphone: {received_samples: Infinity}}), '');
  assert.equal(format({microphone: {received_samples: -1}}), '');
});

const attention = vm.runInNewContext(`${fn}\naudioAttentionText;`);
test('attention requires bounded, nonzero audio evidence and all completed chunks empty', () => {
  assert.equal(attention(null), '');
  const row = {received_samples: 16000, completed: 1, empty: 1, peak: .1, rms: .01};
  assert.match(attention({microphone: row}), /Microphone: audio received, no words detected/);
  for (const patch of [{received_samples: 1e13}, {completed: 1e13, empty: 1e13}, {completed: 1.5, empty: 1.5},
    {empty: 0}, {empty: '1'}, {peak: NaN, rms: NaN}, {peak: 2, rms: undefined}]) {
    assert.equal(attention({microphone: {...row, ...patch}}), '');
  }
});
