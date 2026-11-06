import test from 'node:test';
import assert from 'node:assert/strict';
import {drainFloat32} from '../src/ui/local-mic.mjs';

function bytes(values) {
  const buffer = new ArrayBuffer(values.length * 4);
  const view = new DataView(buffer);
  values.forEach((value, index) => view.setFloat32(index * 4, value, true));
  return new Uint8Array(buffer);
}

test('drainFloat32 returns whole samples and carries the remainder', () => {
  const first = drainFloat32(new Uint8Array(0), bytes([0.5, -0.25, 0.125]).slice(0, 10));
  assert.deepEqual([...first.samples], [0.5, -0.25]);
  assert.equal(first.rest.length, 2);
  const second = drainFloat32(first.rest, bytes([0.125]).slice(2));
  assert.deepEqual([...second.samples], [0.125]);
  assert.equal(second.rest.length, 0);
});

test('drainFloat32 joins a sample split across chunks', () => {
  const whole = bytes([0.75, -0.5]);
  const a = drainFloat32(new Uint8Array(0), whole.slice(0, 3));
  assert.equal(a.samples.length, 0);
  const b = drainFloat32(a.rest, whole.slice(3));
  assert.deepEqual([...b.samples], [0.75, -0.5]);
});

test('drainFloat32 ignores an empty chunk', () => {
  const out = drainFloat32(new Uint8Array(0), new Uint8Array(0));
  assert.equal(out.samples.length, 0);
  assert.equal(out.rest.length, 0);
});
