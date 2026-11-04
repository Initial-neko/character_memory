import test from 'node:test';
import assert from 'node:assert/strict';
import {ScreenWatcher} from '../src/ui/screen-watcher.mjs';

test('a watcher reads only while started and within budget', async () => {
  let calls = 0;
  const watcher = new ScreenWatcher({intervalMs: 1000, maxReads: 2, onTick: async () => { calls += 1; }});
  assert.equal(await watcher.tick(), 'skip');
  watcher.start();
  assert.equal(await watcher.tick(), 'read');
  assert.equal(await watcher.tick(), 'read');
  assert.equal(calls, 2);
  assert.equal(watcher.state.remaining, 0);
  assert.equal(watcher.active, false);
  assert.equal(await watcher.tick(), 'skip');
});

test('a slow look is not overlapped by the next tick', async () => {
  let release;
  const watcher = new ScreenWatcher({maxReads: 5, onTick: () => new Promise((resolve) => { release = resolve; })});
  watcher.start();
  const first = watcher.tick();
  assert.equal(await watcher.tick(), 'skip');
  release();
  assert.equal(await first, 'read');
  watcher.stop();
});

test('stop keeps the spent budget instead of refunding it', async () => {
  const watcher = new ScreenWatcher({maxReads: 10, onTick: async () => {}});
  watcher.start();
  await watcher.tick();
  watcher.stop();
  assert.equal(watcher.state.reads, 1);
  assert.equal(watcher.state.remaining, 9);
  watcher.start();
  assert.equal(watcher.active, true);
  watcher.stop();
});
