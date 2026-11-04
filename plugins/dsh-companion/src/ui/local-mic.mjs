/**
 * Local microphone client: reads float32 PCM from tools/mic-server.py.
 *
 * Same surface as createMicStream (sampleRate / mute / stop) so the call
 * orchestration does not care where the audio came from.
 */

/** Split an arbitrary byte chunk into whole float32 samples plus the carry. */
export function drainFloat32(carry, chunk) {
  const merged = new Uint8Array(carry.length + chunk.length);
  merged.set(carry);
  merged.set(chunk, carry.length);
  const usable = merged.length - (merged.length % 4);
  const samples = new Float32Array(usable / 4);
  const view = new DataView(merged.buffer, 0, usable);
  for (let i = 0; i < samples.length; i += 1) samples[i] = view.getFloat32(i * 4, true);
  return {samples, rest: merged.slice(usable)};
}

function baseOf(url) {
  return String(url || 'http://127.0.0.1:50132').replace(/\/+$/, '');
}

export async function createLocalMicStream({url, signal, onFrame = () => {}} = {}) {
  const base = baseOf(url);
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) throw Error('recording cancelled');
  signal?.addEventListener('abort', abort, {once: true});
  let response;
  try {
    response = await fetch(`${base}/stream`, {signal: controller.signal});
  } catch (error) {
    signal?.removeEventListener('abort', abort);
    throw Error(`本地麦克风服务连不上：${error.message}`);
  }
  if (!response.ok || !response.body) {
    signal?.removeEventListener('abort', abort);
    throw Error(`本地麦克风服务返回 ${response.status}`);
  }
  const sampleRate = Number(response.headers.get('x-sample-rate') || 16000) || 16000;
  const reader = response.body.getReader();
  let carry = new Uint8Array(0);
  const pump = async () => {
    try {
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        if (!value?.length) continue;
        const {samples, rest} = drainFloat32(carry, value);
        carry = rest;
        if (samples.length) onFrame(samples);
      }
    } catch (error) {
      if (error.name !== 'AbortError') console.error('[companion] local mic stream failed', error);
    }
  };
  pump();
  return {
    sampleRate,
    mute(value) {
      fetch(`${base}/mute?on=${value ? 1 : 0}`, {method: 'POST'}).catch(() => {});
    },
    stop() {
      signal?.removeEventListener('abort', abort);
      controller.abort();
      reader.cancel().catch(() => {});
    },
  };
}

/** Ask the host to start the local mic service if it is not up yet. */
export async function ensureLocalMic(request, port) {
  return request('/mic/ensure', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({port}),
  });
}
