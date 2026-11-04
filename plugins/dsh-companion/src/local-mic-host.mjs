import {spawn} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const script = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'tools', 'mic-server.py');
let child = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function healthy(url) {
  try {
    const response = await fetch(`${url}/health`, {signal: AbortSignal.timeout(800)});
    if (!response.ok) return null;
    const data = await response.json();
    return data?.ready ? data : null;
  } catch {
    return null;
  }
}

/**
 * Make sure the local microphone service is listening. The renderer never
 * touches the device; this host-side helper starts the capture process once.
 */
export async function ensureMicServer({url, python = 'python'} = {}) {
  const base = String(url || 'http://127.0.0.1:50132').replace(/\/+$/, '');
  const port = Number(new URL(base).port || 50132);
  const before = await healthy(base);
  if (before) return {ok: true, ready: true, started: false, url: base, rate: before.rate};

  if (!child) {
    try {
      child = spawn(python, [script, '--port', String(port)], {
        stdio: 'ignore',
        windowsHide: true,
        detached: true,
      });
      child.unref();
      child.on('exit', () => { child = null; });
    } catch (error) {
      child = null;
      return {ok: false, ready: false, started: false, url: base, error: `启动失败：${error.message}`};
    }
  }
  for (let i = 0; i < 20; i += 1) {
    await sleep(300);
    const info = await healthy(base);
    if (info) return {ok: true, ready: true, started: true, url: base, rate: info.rate, device: info.device};
  }
  return {ok: false, ready: false, started: true, url: base, error: '服务没有在 6 秒内就绪，可能是没有输入设备'};
}
