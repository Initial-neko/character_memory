import test from 'node:test';
import assert from 'node:assert/strict';
import {Endpointer} from '../src/ui/endpointer.mjs';

const RATE = 16000;
const chunk = (ms, level) => {
  const data = new Float32Array(Math.round((RATE * ms) / 1000));
  if (level) for (let i = 0; i < data.length; i++) data[i] = Math.sin(i / 3) * level;
  return data;
};
const feed = (ep, ms, level, step = 40) => {
  const out = [];
  for (let t = 0; t < ms; t += step) out.push(ep.push(chunk(Math.min(step, ms - t), level)));
  return out.filter(Boolean);
};

test('pure silence never produces an utterance', () => {
  const ep = new Endpointer({rate: RATE});
  assert.deepEqual(feed(ep, 3000, 0), []);
  assert.equal(ep.listening, false);
});

test('speech followed by silence ends the utterance with the spoken span', () => {
  const ep = new Endpointer({rate: RATE, silenceMs: 600});
  assert.deepEqual(feed(ep, 500, 0), []);
  assert.deepEqual(feed(ep, 900, 0.2), []);
  const done = feed(ep, 900, 0);
  assert.equal(done.length, 1);
  assert.equal(done[0].reason, 'silence');
  assert.ok(done[0].samples.length >= RATE * 0.9, 'keeps the whole spoken span');
  assert.ok(done[0].spokenMs >= 900);
});

test('a short blip is discarded instead of becoming a sentence', () => {
  const ep = new Endpointer({rate: RATE, silenceMs: 500, minSpeechMs: 300});
  assert.deepEqual(feed(ep, 300, 0), []);
  assert.deepEqual(feed(ep, 100, 0.3), []);
  assert.deepEqual(feed(ep, 2500, 0), []);
  assert.equal(ep.listening, false);
});

test('continuous speech is cut at the maximum length', () => {
  const ep = new Endpointer({rate: RATE, maxSpeechMs: 1000, silenceMs: 5000});
  feed(ep, 300, 0);
  const done = feed(ep, 1600, 0.25);
  assert.equal(done.length, 1);
  assert.equal(done[0].reason, 'length');
});

test('reset drops a half-spoken utterance after mute or playback', () => {
  const ep = new Endpointer({rate: RATE, silenceMs: 500});
  feed(ep, 300, 0);
  feed(ep, 400, 0.2);
  ep.reset();
  assert.equal(ep.listening, false);
  assert.deepEqual(feed(ep, 2000, 0), []);
});

test('speech already playing during calibration is still detected', () => {
  const ep = new Endpointer({rate: RATE, silenceMs: 500});
  assert.deepEqual(feed(ep, 900, 0.3), []);
  assert.ok(ep.threshold <= 0.03, 'threshold is capped so loud calibration cannot deafen it');
  const done = feed(ep, 900, 0);
  assert.equal(done.length, 1);
  assert.equal(done[0].reason, 'silence');
});
