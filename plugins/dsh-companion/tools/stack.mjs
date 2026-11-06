#!/usr/bin/env node
/**
 * One place to see and run the companion stack.
 *
 *   node tools/stack.mjs status   what is running right now (default)
 *   node tools/stack.mjs up       start whatever is missing
 *   node tools/stack.mjs down     stop everything on these ports
 *   node tools/stack.mjs check    run the voice round-trip check
 */
import {spawn, execFileSync} from 'node:child_process';
import {existsSync, readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(here, '..');
const projectRoot = process.env.CM_ROOT || 'C:/Users/cute/projects/character_memory';
const runtimeHome = process.env.DSH_HOME || 'C:/Users/cute/Downloads/deepseek临时工作区/_runtime-home';
const PYTHON = process.env.PYTHON || 'C:/Users/cute/miniconda3/python.exe';
const GSV_PYTHON = path.join(projectRoot, '.external/GSV-TTS-Lite/.venv/Scripts/python.exe');
const ASR_MODULE = process.env.ASR_MODULE || 'C:/Users/cute/.dsh/profiles/desktop/node_modules/sherpa-onnx-node/sherpa-onnx.js';
const ASR_MODEL = process.env.ASR_MODEL || 'C:/Users/cute/AppData/Local/dsh-voice-mode/models/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17';

function readEnv(file) {
  if (!existsSync(file)) return {};
  const out = {};
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const match = /^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/.exec(line);
    if (!match) continue;
    out[match[1]] = match[2].replace(/^["']|["']$/g, '');
  }
  return out;
}

const gsvEnv = {
  ...process.env,
  ...readEnv(path.join(projectRoot, '.env')),
  PYTHONPATH: [path.join(projectRoot, 'src'), path.join(projectRoot, '.external/GSV-TTS-Lite')].join(';'),
  GSV_TTS_HOST: '127.0.0.1',
  GSV_TTS_PORT: '9014',
  GSV_TTS_VOICE: 'momo',
  GSV_TTS_PERSONA_ROOT: path.join(projectRoot, 'personas'),
  GSV_TTS_VOICES_ROOT: path.join(projectRoot, 'voices'),
  GSV_TTS_PRELOAD: '1',
  GSV_TTS_DEVICE: 'cuda',
};

const SERVICES = [
  {name: 'asr', port: 50131, health: '/health', cwd: pluginRoot, cmd: 'node',
    args: ['tools/asr-server.mjs', '--module', ASR_MODULE, '--model-dir', ASR_MODEL, '--language', 'zh', '--port', '50131']},
  {name: 'mic', port: 50132, health: '/health', cwd: pluginRoot, cmd: PYTHON,
    args: ['tools/mic-server.py', '--port', '50132', '--device', '19']},
  {name: 'screen', port: 50134, health: '/health', cwd: pluginRoot, cmd: PYTHON,
    args: ['tools/screen-server.py', '--port', '50134']},
  {name: 'gsv', port: 9014, health: '/health', cwd: projectRoot, cmd: GSV_PYTHON,
    args: ['-m', 'character_memory.gsv_tts_runtime'], env: gsvEnv},
  {name: 'web', port: 50140, health: null, cwd: 'C:/Users/cute/Downloads/deepseek临时工作区', cmd: 'dsh',
    args: ['--profile', 'companion-test', '--port', '50140', '--no-open'],
    env: {...process.env, DSH_HOME: runtimeHome, DSH_AGENTS_HOME: runtimeHome}},
];

function listening(port) {
  try {
    const out = execFileSync('netstat', ['-ano'], {encoding: 'utf8'});
    for (const line of out.split(/\r?\n/)) {
      if (!line.includes(`:${port}`) || !line.includes('LISTENING')) continue;
      const parts = line.trim().split(/\s+/);
      return Number(parts[parts.length - 1]);
    }
  } catch {}
  return null;
}

async function probe(service) {
  const pid = listening(service.port);
  if (!pid) return {state: 'down', pid: null, detail: 'not listening'};
  if (!service.health) return {state: 'up', pid, detail: 'listening'};
  try {
    const response = await fetch(`http://127.0.0.1:${service.port}${service.health}`, {signal: AbortSignal.timeout(2500)});
    const data = await response.json();
    const ready = data.ready === true || data.ok === true;
    return {state: ready ? 'up' : 'starting', pid, detail: data.error || (ready ? 'ready' : 'not ready')};
  } catch (error) {
    return {state: 'starting', pid, detail: String(error.message || error)};
  }
}

async function status() {
  const rows = [];
  for (const service of SERVICES) rows.push({service, ...(await probe(service))});
  console.log('service  port   state     pid    detail');
  for (const row of rows) {
    console.log(`${row.service.name.padEnd(8)} ${String(row.service.port).padEnd(6)} ${row.state.padEnd(9)} ${String(row.pid ?? '-').padEnd(6)} ${row.detail}`);
  }
  return rows;
}

function start(service) {
  const child = spawn(service.cmd, service.args, {
    cwd: service.cwd,
    env: service.env || process.env,
    stdio: 'ignore',
    windowsHide: true,
    detached: true,
    shell: process.platform === 'win32',
  });
  child.unref();
}

async function up(names) {
  const wanted = names.length ? SERVICES.filter((s) => names.includes(s.name)) : SERVICES;
  for (const service of wanted) {
    const state = await probe(service);
    if (state.state === 'up') {
      console.log(`${service.name}: already up (pid ${state.pid})`);
      continue;
    }
    if (state.pid) {
      console.log(`${service.name}: listening but not ready (${state.detail}); leave it or run down first`);
      continue;
    }
    start(service);
    console.log(`${service.name}: started on ${service.port} (cold start may take up to 45s)`);
  }
  for (let i = 0; i < 60; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const rows = [];
    for (const service of wanted) rows.push(await probe(service));
    if (rows.every((row) => row.state === 'up')) break;
  }
  for (const service of wanted) await warmup(service);
  await status();
}

async function warmup(service) {
  if (service.name !== 'gsv') return;
  const started = Date.now();
  try {
    const response = await fetch(`http://127.0.0.1:${service.port}/v1/tts`, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({text: '嗯，我在这儿。', voice: 'momo', language: 'zh'}),
      signal: AbortSignal.timeout(120000),
    });
    console.log(response.ok ? `gsv: warmed up in ${Date.now() - started}ms` : `gsv: warmup HTTP ${response.status}`);
  } catch (error) {
    console.log(`gsv: warmup failed ${error.message}`);
  }
}

function down(names) {
  const wanted = names.length ? SERVICES.filter((s) => names.includes(s.name)) : SERVICES;
  for (const service of wanted) {
    const pid = listening(service.port);
    if (!pid) { console.log(`${service.name}: not running`); continue; }
    try { execFileSync('taskkill', ['/F', '/PID', String(pid)], {stdio: 'ignore'}); console.log(`${service.name}: stopped (${pid})`); }
    catch { console.log(`${service.name}: could not stop ${pid}`); }
  }
}

const [command = 'status', ...names] = process.argv.slice(2);
if (command === 'status') await status();
else if (command === 'up') await up(names);
else if (command === 'down') down(names);
else if (command === 'e2e') {
  if (!names[0]) { console.log('usage: node tools/stack.mjs e2e <url>'); process.exit(2); }
  const child = spawn('node', ['tools/e2e-check.mjs', names[0]], {cwd: pluginRoot, stdio: 'inherit'});
  child.on('exit', (code) => process.exit(code ?? 1));
} else if (command === 'check') {
  const child = spawn('node', ['tools/voice-check.mjs'], {cwd: pluginRoot, stdio: 'inherit'});
  child.on('exit', (code) => process.exit(code ?? 1));
} else {
  console.log('usage: node tools/stack.mjs [status|up|down|check] [service...]');
  process.exit(2);
}
