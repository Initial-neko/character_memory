import test from 'node:test';
import assert from 'node:assert/strict';
import {checkTranscript} from '../src/ui/transcript-gate.mjs';

test('chinese speech passes and reports its shape', () => {
  const result = checkTranscript('今天一起去公园散步吧');
  assert.equal(result.ok, true);
  assert.equal(result.han, 10);
});

test('kana, latin and punctuation-only noise are rejected', () => {
  assert.equal(checkTranscript('そうですね、はい').reason, 'no-chinese');
  assert.equal(checkTranscript('thank you very much').reason, 'no-chinese');
  assert.equal(checkTranscript('……，。！').reason, 'too-short');
  assert.equal(checkTranscript('我.').reason, 'too-short');
  assert.equal(checkTranscript('嗯。').reason, 'too-short');
  assert.equal(checkTranscript('').reason, 'too-short');
  assert.equal(checkTranscript('1234').reason, 'no-chinese');
});

test('short fragments are rejected before they become messages', () => {
  assert.equal(checkTranscript('嗯').reason, 'too-short');
  assert.equal(checkTranscript('好').reason, 'too-short');
  assert.equal(checkTranscript('好的').ok, true);
});

test('mixed text with too little chinese is rejected', () => {
  assert.equal(checkTranscript('ok 那就好 ok ok ok').reason, 'mostly-non-chinese');
  assert.equal(checkTranscript('好 ok').ok, true);
});

test('the gate is configurable instead of hard-coded', () => {
  assert.equal(checkTranscript('hello', {requireChinese: false}).ok, true);
  assert.equal(checkTranscript('嗯', {minChars: 1}).ok, true);
  assert.equal(checkTranscript('中文 mixed english here', {chineseRatio: 0.8}).reason, 'mostly-non-chinese');
});
