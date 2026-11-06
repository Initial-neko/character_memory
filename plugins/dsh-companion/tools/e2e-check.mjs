#!/usr/bin/env node
/**
 * End-to-end check of the companion UI, the way a human would use it:
 * open the big screen, type a message, wait for her reply, shrink to the pet.
 *
 *   node tools/e2e-check.mjs "http://127.0.0.1:50140/?token=..."
 *
 * Starts its own headless Chromium, exits 0 only if every step passes.
 */
import {spawn, execFileSync} from 'node:child_process';
import {mkdirSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(here, '..', '.e2e');
const CHROME = process.env.CHROME || 'C:/Users/cute/AppData/Local/ms-playwright/chromium-1248/chrome-win64/chrome.exe';
const PORT = 9234;
const url = process.argv[2];
if (!url) {
  console.log('usage: node tools/e2e-check.mjs "http://127.0.0.1:50140/?token=..."');
  process.exit(2);
}

mkdirSync(outDir, {recursive: true});
const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${path.join(outDir, 'chrome')}`,
  '--no-first-run', '--no-default-browser-check',
  '--enable-unsafe-swiftshader', '--use-angle=swiftshader',
  '--disable-dev-shm-usage', '--window-size=1440,900', 'about:blank',
], {stdio: 'ignore', detached: false});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const record = (step, ok, detail) => {
  results.push({step, ok, detail});
  console.log(`${ok ? 'PASS' : 'FAIL'} ${step}: ${detail}`);
};

let ws = null;
let seq = 0;
const pending = new Map();
const logs = [];

async function connect() {
  for (let i = 0; i < 40; i += 1) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = targets.find((t) => t.type === 'page');
      if (page) {
        ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
        ws.onmessage = (event) => {
          const data = JSON.parse(event.data);
          if (data.id && pending.has(data.id)) {
            const {resolve, reject} = pending.get(data.id);
            pending.delete(data.id);
            if (data.error) reject(Error(JSON.stringify(data.error))); else resolve(data.result);
            return;
          }
          if (data.method === 'Runtime.exceptionThrown') logs.push('EXC ' + (data.params.exceptionDetails?.exception?.description || '').slice(0, 200));
          if (data.method === 'Runtime.consoleAPICalled' && data.params.type === 'error') logs.push('ERR ' + data.params.args.map((a) => a.value ?? a.description).join(' ').slice(0, 200));
        };
        return;
      }
    } catch {}
    await sleep(500);
  }
  throw Error('chromium did not expose a page');
}

const send = (method, params = {}) => new Promise((resolve, reject) => {
  const message = {id: ++seq, method, params};
  pending.set(message.id, {resolve, reject});
  ws.send(JSON.stringify(message));
});
const evaluate = async (expression) => {
  const result = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (result.exceptionDetails) throw Error(result.exceptionDetails.text ?? 'evaluate failed');
  return result.result.value;
};
async function clickText(text, scope = 'button') {
  const point = await evaluate(`(() => {
    const node = [...document.querySelectorAll(${JSON.stringify(scope)})].find((n) => (n.textContent || '').trim().includes(${JSON.stringify(text)}));
    if (!node) return null;
    const r = node.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
  })()`);
  if (!point) return false;
  await send('Input.dispatchMouseEvent', {type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1});
  await send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1});
  return true;
}

const messages = () => evaluate(`[...document.querySelectorAll('.dsh-companion-bigchat-msg')].map((n) => n.className.split(' ').pop() + ':' + n.textContent.slice(0, 40))`);

try {
  await connect();
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', {url});
  await sleep(8000);

  const trigger = await clickText('陪伴');
  record('open trigger', trigger, trigger ? 'clicked 陪伴' : '陪伴 button missing');
  await sleep(3500);
  const opened = await evaluate(`!!document.querySelector('.dsh-companion-bigchat')`);
  record('big screen opens', opened, opened ? 'big chat visible' : 'big chat missing');

  const typed = await evaluate(`(() => {
    const ta = document.querySelector('.dsh-companion-bigchat-chat textarea');
    if (!ta) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, '在吗，测试一下');
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    return true;
  })()`);
  record('composer accepts text', typed, typed ? 'typed a line' : 'textarea missing');

  const sent = await clickText('发给她', '.dsh-companion-bigchat-chat button');
  record('send button', sent, sent ? 'clicked 发给她' : 'send button missing');

  let list = [];
  for (let i = 0; i < 12; i += 1) {
    await sleep(5000);
    list = await messages();
    if (list.some((m) => m.startsWith('assistant'))) break;
  }
  const gotUser = list.some((m) => m.startsWith('user'));
  const gotReply = list.some((m) => m.startsWith('assistant'));
  record('user message shown', gotUser, gotUser ? 'found the user line' : 'user line never appeared');
  record('assistant reply shown', gotReply, gotReply ? 'found the reply' : 'no reply within 60s');

  const error = await evaluate(`document.querySelector('.dsh-companion-bigchat-error')?.textContent ?? ''`);
  record('no inline error', !error, error || 'none');

  const shrunk = await clickText('缩小', '.dsh-companion-bigchat-bar button');
  await sleep(2500);
  const pet = await evaluate(`(() => {
    const node = document.querySelector('.dsh-companion-pet');
    const r = node?.getBoundingClientRect();
    return {pet: !!node, rect: r ? [Math.round(r.width), Math.round(r.height)] : null, canvas: node ? node.querySelectorAll('canvas').length : 0};
  })()`);
  record('shrink to pet', shrunk && pet.pet && pet.canvas > 0, JSON.stringify(pet));

  const shot = await send('Page.captureScreenshot', {format: 'png'});
  writeFileSync(path.join(outDir, 'final.png'), Buffer.from(shot.data, 'base64'));
  record('no console errors', logs.length === 0, logs.length ? JSON.stringify(logs.slice(-4)) : 'clean');
} catch (error) {
  record('run', false, String(error.message || error));
} finally {
  try { ws?.close(); } catch {}
  try { chrome.kill(); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(JSON.stringify({ok: failed.length === 0, failed: failed.map((r) => r.step), screenshot: path.join(outDir, 'final.png')}, null, 2));
process.exit(failed.length === 0 ? 0 : 1);
