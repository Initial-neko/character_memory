import test from 'node:test';
import assert from 'node:assert/strict';
const module = await import('../src/call-controller.mjs').catch(() => ({}));
const create = (...args) => { assert.equal(typeof module.CallController, 'function', 'CallController must exist'); return new module.CallController(...args); };
const session = {sessionId:'session-a',personaRevision:'rin-1',configRevision:1};

test('default call owns no resources and requires a real session before start', () => {
  const call=create(); assert.equal(call.snapshot.phase,'IDLE');
  assert.throws(()=>call.start({}),/session/); assert.equal(call.snapshot.phase,'IDLE');
});
test('minimize preserves active call; mute is input-only; end releases all resources once', () => {
  const call=create(); call.start(session);call.connected();
  let stopped=0;call.own('microphone',()=>stopped++);call.own('playback',()=>stopped++);
  call.minimize();assert.equal(call.snapshot.phase,'ACTIVE');assert.equal(call.snapshot.minimized,true);
  call.muteInput(true);assert.equal(stopped,0);assert.equal(call.snapshot.inputMuted,true);
  call.expand();assert.equal(call.snapshot.minimized,false);
  call.end('user');call.end('user');assert.equal(stopped,2);assert.equal(call.snapshot.phase,'IDLE');
});
test('late provider output is fenced by generation, session, persona and config revision', () => {
  const call=create();call.start(session);const original=call.token();assert.equal(call.accepts(original),true);
  call.end();call.start(session);assert.equal(call.accepts(original),false);
  assert.equal(call.accepts({...call.token(),sessionId:'other'}),false);
  assert.equal(call.accepts({...call.token(),personaRevision:'other'}),false);
  assert.equal(call.accepts({...call.token(),configRevision:99}),false);
});
test('switching session pauses and releases capture and playback without migrating ownership', () => {
  const call=create();call.start(session);call.connected();let disposed=0;call.own('screen',()=>disposed++);
  const token=call.token();call.navigate('session-b');
  assert.equal(call.snapshot.phase,'PAUSED');assert.equal(call.snapshot.sessionId,'session-a');
  assert.equal(disposed,1);assert.equal(call.accepts(token),false);
  assert.throws(()=>call.connected(),/state/);
});
test('stopping only screen leaves voice active; external ended uses the same cleanup', () => {
  const call=create();call.start(session);call.connected();let mic=0,screen=0;
  call.own('microphone',()=>mic++);call.own('screen',()=>screen++);call.screenStarted('sample window');
  call.stopScreen();call.stopScreen();assert.equal(screen,1);assert.equal(mic,0);
  assert.equal(call.snapshot.phase,'ACTIVE');assert.equal(call.snapshot.screen,'OFF');
  call.end();assert.equal(mic,1);
});
test('permission dialog resolves after end: late resource is immediately released', () => {
  const call=create();call.start(session);const token=call.token();call.end();let stopped=0;
  assert.equal(call.own('microphone',()=>stopped++,token),false);assert.equal(stopped,1);
});
test('cleanup failure does not leave other resources or call active', () => {
  const call=create();call.start(session);let stopped=0;
  call.own('microphone',()=>{throw Error('device lost')});call.own('playback',()=>stopped++);
  call.end();assert.equal(stopped,1);assert.equal(call.snapshot.phase,'IDLE');
  assert.equal(call.snapshot.cleanupErrors.length,1);
});
test('configuration change invalidates work and pauses existing call', () => {
  const call=create();call.start(session);call.connected();const token=call.token();
  call.configure(2);assert.equal(call.snapshot.phase,'PAUSED');assert.equal(call.accepts(token),false);
});
test('subscriptions release and disposal cannot be reactivated', () => {
  const call=create();let changes=0;const off=call.subscribe(()=>changes++);call.start(session);off();call.end();
  assert.equal(changes,1);call.dispose();assert.throws(()=>call.start(session),/disposed/);
});
