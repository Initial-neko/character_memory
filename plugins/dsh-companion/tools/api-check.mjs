#!/usr/bin/env node
/**
 * Backend contract check for the companion plugin.
 *
 *   node tools/api-check.mjs "http://127.0.0.1:50140"
 *
 * Verifies the JSON endpoints, every client module the browser will ask for,
 * and the SSE stream. Exits non-zero on the first broken contract.
 */
const base = (process.argv[2] || 'http://127.0.0.1:50140').replace(/\/$/, '');
const results = [];
const record = (step, ok, detail) => {
  results.push({step, ok, detail});
  console.log(`${ok ? 'PASS' : 'FAIL'} ${step}: ${detail}`);
};

const ASSETS = [
  'ui/pet', 'ui/bigchat', 'ui/stage', 'ui/orchestrator', 'ui/styles', 'ui/panel',
  'ui/settings', 'ui/trigger', 'ui/capsule', 'ui/store', 'client-ui',
  'live2d-view', 'config', 'screen-capture', 'host-bridge', 'call-controller',
];

async function getJson(path) {
  const response = await fetch(`${base}${path}`, {signal: AbortSignal.timeout(6000)});
  const text = await response.text();
  let body = null;
  try { body = JSON.parse(text); } catch {}
  return {status: response.status, body, text};
}

const config = await getJson('/companion/api/config').catch((error) => ({status: 0, error}));
record('GET /companion/api/config', config.status === 200 && config.body?.value && typeof config.body.revision === 'number',
  config.status === 200 ? `revision ${config.body?.revision}` : `HTTP ${config.status} ${config.error ?? ''}`);

const state = await getJson('/companion/api/state').catch((error) => ({status: 0, error}));
record('GET /companion/api/state', state.status === 200, state.status === 200 ? 'ok' : `HTTP ${state.status}`);

for (const name of ASSETS) {
  const response = await fetch(`${base}/companion/assets/${name}.mjs`, {signal: AbortSignal.timeout(6000)}).catch(() => null);
  record(`asset ${name}.mjs`, response?.status === 200, `HTTP ${response?.status ?? 'error'}`);
}

const mic = await fetch(`${base}/companion/api/mic/ensure`, {method: 'POST', signal: AbortSignal.timeout(20000)}).catch(() => null);
record('POST /companion/api/mic/ensure', mic?.status === 200, `HTTP ${mic?.status ?? 'error'}`);

let sseOk = false;
try {
  const controller = new AbortController();
  const response = await fetch(`${base}/companion/api/events?sessionId=api-check`, {signal: controller.signal, headers: {accept: 'text/event-stream'}});
  sseOk = response.status === 200 && (response.headers.get('content-type') || '').includes('event-stream');
  controller.abort();
} catch {}
record('GET /companion/api/events (SSE)', sseOk, sseOk ? 'event-stream' : 'not an event stream');

const failed = results.filter((item) => !item.ok);
console.log(JSON.stringify({ok: failed.length === 0, failed: failed.map((item) => item.step)}, null, 2));
process.exit(failed.length === 0 ? 0 : 1);
